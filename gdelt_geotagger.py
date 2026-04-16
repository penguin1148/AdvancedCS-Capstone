"""Fetch news stories from the GDELT DOC 2.0 API and geotag each by country.

GDELT exposes a free, public REST endpoint at
https://api.gdeltproject.org/api/v2/doc/doc that returns article metadata
in JSON. Each record includes a ``sourcecountry`` (FIPS 10-4 code for the
publisher's country) and a list of geographic ``locations`` mentioned in the
article body. We use both to assign a country tag to every story.
"""

from __future__ import annotations

import argparse
import csv
import curses
import json
import os
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Callable

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT asks callers to stay under roughly one request every ~5 seconds; exceed
# that and the edge starts returning 429 Too Many Requests, sometimes for
# minutes at a time. We enforce the floor locally (no matter how often
# ``fetch_stories`` is called, or how often the script is re-invoked) and,
# when a 429 slips through anyway, back off exponentially before retrying.
MIN_REQUEST_INTERVAL = 6.0      # seconds between successive requests
MAX_RETRIES = 6                 # total attempts on 429 / transient errors
INITIAL_BACKOFF = 15.0          # first sleep on 429; doubles each retry
MAX_BACKOFF = 120.0             # cap any single backoff at 2 minutes

# Persist the last-request timestamp so consecutive invocations of the script
# still honor the rate limit. Stored as seconds-since-epoch in a small file
# under the user's temp directory.
_STATE_PATH = os.path.join(tempfile.gettempdir(), "gdelt_geotagger.state")


def _load_last_request_time() -> float:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return 0.0


def _save_last_request_time(ts: float) -> None:
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(ts))
    except OSError:
        pass  # best-effort; a failure here just means no cross-run throttling

# FIPS 10-4 country codes used by GDELT -> human-readable name.
# Covers the codes most commonly seen in GDELT output. Anything missing
# falls back to the raw code so the caller still has something to work with.
FIPS_COUNTRY = {
    "AE": "United Arab Emirates", "AF": "Afghanistan", "AG": "Algeria",
    "AR": "Argentina", "AS": "Australia", "AU": "Austria", "BE": "Belgium",
    "BL": "Bolivia", "BM": "Burma", "BO": "Belarus", "BR": "Brazil",
    "BU": "Bulgaria", "CA": "Canada", "CH": "China", "CI": "Chile",
    "CO": "Colombia", "CS": "Costa Rica", "CU": "Cuba", "CY": "Cyprus",
    "DA": "Denmark", "EG": "Egypt", "EI": "Ireland", "ER": "Eritrea",
    "ES": "El Salvador", "ET": "Ethiopia", "EZ": "Czech Republic",
    "FI": "Finland", "FR": "France", "GG": "Georgia", "GM": "Germany",
    "GR": "Greece", "HO": "Honduras", "HU": "Hungary", "IC": "Iceland",
    "ID": "Indonesia", "IN": "India", "IR": "Iran", "IS": "Israel",
    "IT": "Italy", "IZ": "Iraq", "JA": "Japan", "JO": "Jordan",
    "KE": "Kenya", "KN": "North Korea", "KS": "South Korea", "KU": "Kuwait",
    "LE": "Lebanon", "LH": "Lithuania", "LO": "Slovakia", "LU": "Luxembourg",
    "MA": "Madagascar", "MO": "Morocco", "MX": "Mexico", "MY": "Malaysia",
    "NI": "Nigeria", "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand",
    "PE": "Peru", "PK": "Pakistan", "PL": "Poland", "PO": "Portugal",
    "QA": "Qatar", "RO": "Romania", "RP": "Philippines", "RS": "Russia",
    "SA": "Saudi Arabia", "SF": "South Africa", "SG": "Senegal",
    "SN": "Singapore", "SP": "Spain", "SU": "Sudan", "SW": "Sweden",
    "SY": "Syria", "SZ": "Switzerland", "TH": "Thailand", "TS": "Tunisia",
    "TU": "Turkey", "TW": "Taiwan", "UG": "Uganda", "UK": "United Kingdom",
    "UP": "Ukraine", "US": "United States", "UV": "Burkina Faso",
    "UY": "Uruguay", "VE": "Venezuela", "VM": "Vietnam", "WA": "Namibia",
    "YM": "Yemen", "ZA": "Zambia", "ZI": "Zimbabwe",
}


@dataclass
class Story:
    title: str
    url: str
    seendate: str
    domain: str
    language: str
    source_country_code: str
    source_country: str
    mentioned_countries: list[str] = field(default_factory=list)
    primary_country: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _country_name(code: str) -> str:
    if not code:
        return ""
    return FIPS_COUNTRY.get(code.upper(), code.upper())


def _mentioned_countries(article: dict) -> list[str]:
    """Pull country names out of the article's ``locations`` array.

    GDELT location entries look like:
        {"countrycode": "US", "name": "Washington, District Of Columbia, United States", ...}
    We dedupe while preserving order so the first mention wins.
    """
    seen: list[str] = []
    for loc in article.get("locations") or []:
        code = (loc.get("countrycode") or "").strip()
        if not code:
            continue
        name = _country_name(code)
        if name not in seen:
            seen.append(name)
    return seen


def _build_query_url(query: str, max_records: int, timespan: str, mode: str) -> str:
    params = {
        "query": query,
        "mode": mode,
        "format": "json",
        "maxrecords": str(max_records),
        "timespan": timespan,
        "sort": "datedesc",
    }
    return f"{GDELT_DOC_URL}?{urllib.parse.urlencode(params)}"


def _sleep_with_progress(seconds: float,
                         on_wait: Callable[[str], None] | None) -> None:
    """Sleep in one-second increments so callers can report progress."""
    if seconds <= 0:
        return
    remaining = seconds
    while remaining > 0:
        if on_wait is not None:
            on_wait(f"Rate limit: waiting {remaining:.0f}s\u2026")
        step = 1.0 if remaining > 1.0 else remaining
        time.sleep(step)
        remaining -= step


def _parse_retry_after(header: str | None) -> float:
    """Convert a ``Retry-After`` header to a wait duration in seconds.

    The spec allows either a delta-seconds integer or an HTTP-date. We only
    try the integer form; anything else falls through to our own backoff.
    """
    if not header:
        return 0.0
    try:
        return max(0.0, float(header.strip()))
    except ValueError:
        return 0.0


def _throttled_get(url: str, timeout: float,
                   on_wait: Callable[[str], None] | None,
                   max_retries: int = MAX_RETRIES,
                   initial_backoff: float = INITIAL_BACKOFF) -> bytes:
    """Fetch ``url`` while honoring the cross-run request interval and
    retrying on 429 / transient 5xx responses with exponential backoff."""

    req = urllib.request.Request(
        url, headers={"User-Agent": "gdelt-geotagger/1.0"}
    )
    backoff = initial_backoff
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        # Client-side floor: keep at least MIN_REQUEST_INTERVAL between calls
        # even across separate script invocations (state is persisted to disk).
        last_ts = _load_last_request_time()
        wait = MIN_REQUEST_INTERVAL - (time.time() - last_ts)
        if wait > 0 and on_wait is not None:
            on_wait(f"Throttling: waiting {wait:.0f}s before first request\u2026")
        _sleep_with_progress(wait, on_wait)

        try:
            _save_last_request_time(time.time())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # 429 = rate limited; 5xx = transient upstream issue. Back off.
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt == max_retries:
                    break
                # Prefer the server's Retry-After guidance if provided.
                retry_after = _parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
                delay = min(MAX_BACKOFF, max(backoff, retry_after))
                if on_wait is not None:
                    on_wait(f"HTTP {exc.code} Too Many Requests; "
                            f"waiting {delay:.0f}s "
                            f"(attempt {attempt}/{max_retries})\u2026")
                _sleep_with_progress(delay, on_wait)
                # Push the next floor-check forward so the post-backoff call
                # is separated from the failure by the full delay.
                _save_last_request_time(time.time())
                backoff = min(MAX_BACKOFF, backoff * 2)
                continue
            raise  # non-retryable HTTP error
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            if on_wait is not None:
                on_wait(f"Network error; retrying in {backoff:.0f}s\u2026")
            _sleep_with_progress(backoff, on_wait)
            backoff = min(MAX_BACKOFF, backoff * 2)

    raise RuntimeError(
        f"GDELT request failed after {max_retries} attempts: {last_exc}. "
        f"GDELT's rate limit may still be active \u2014 wait a few minutes "
        f"and try again."
    )


def fetch_stories(
    query: str,
    max_records: int = 75,
    timespan: str = "1h",
    mode: str = "ArtList",
    timeout: float = 30.0,
    on_wait: Callable[[str], None] | None = None,
    max_retries: int = MAX_RETRIES,
    initial_backoff: float = INITIAL_BACKOFF,
) -> list[Story]:
    """Query the GDELT DOC API and return geotagged ``Story`` records.

    ``query`` follows GDELT's query syntax (e.g. ``"climate change"`` or
    ``"election sourcelang:eng"``). ``timespan`` accepts values like ``15min``,
    ``1h``, ``24h``, ``1d``. We default to a tight one-hour window plus
    ``sort=datedesc`` so the freshest stories come back first.
    ``max_records`` is capped at 250 by the upstream API.

    Requests are throttled to one per ``MIN_REQUEST_INTERVAL`` seconds to stay
    under GDELT's rate limit, and 429 responses trigger exponential backoff.
    Pass ``on_wait`` to receive status strings while the client is sleeping.
    """
    url = _build_query_url(query, max_records, timespan, mode)
    raw_bytes = _throttled_get(url, timeout, on_wait,
                               max_retries=max_retries,
                               initial_backoff=initial_backoff)
    raw = raw_bytes.decode("utf-8", errors="replace")

    # GDELT returns a plain-text error message (not JSON) when the query is
    # rejected, e.g. "Your query was too short or too long." Surface those as
    # a clean error with a usage hint rather than a cryptic decode failure.
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = raw.strip()[:200] or "empty response"
        raise RuntimeError(
            f"GDELT rejected the query: {msg}\n"
            f"Try a real keyword, e.g. "
            f"'python gdelt_geotagger.py \"world news\"'."
        ) from exc

    stories: list[Story] = []
    for art in payload.get("articles", []):
        source_code = (art.get("sourcecountry") or "").strip()
        mentioned = _mentioned_countries(art)
        # Prefer the publisher country; fall back to the first geographic mention.
        primary = _country_name(source_code) or (mentioned[0] if mentioned else "Unknown")
        stories.append(
            Story(
                title=art.get("title", "").strip(),
                url=art.get("url", ""),
                seendate=art.get("seendate", ""),
                domain=art.get("domain", ""),
                language=art.get("language", ""),
                source_country_code=source_code,
                source_country=_country_name(source_code),
                mentioned_countries=mentioned,
                primary_country=primary,
            )
        )
    return stories


def write_csv(stories: list[Story], path: str) -> None:
    fieldnames = [
        "primary_country", "source_country", "source_country_code",
        "mentioned_countries", "seendate", "domain", "language", "title", "url",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for s in stories:
            row = s.to_dict()
            row["mentioned_countries"] = "|".join(row["mentioned_countries"])
            writer.writerow(row)


def write_json(stories: list[Story], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([s.to_dict() for s in stories], fh, indent=2, ensure_ascii=False)


def print_summary(stories: list[Story]) -> None:
    if not stories:
        print("No stories returned.")
        return
    counts = Counter(s.primary_country for s in stories)
    width = max(len(c) for c in counts)
    print(f"\nFetched {len(stories)} stories. Top countries:")
    for country, n in counts.most_common(15):
        print(f"  {country:<{width}}  {n}")
    print("\nSample:")
    for s in stories[:5]:
        print(f"  [{s.primary_country}] {s.title[:90]}  ({s.domain})")


# --- Curses TUI ------------------------------------------------------------

def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "\u2026"


def _safe_addstr(stdscr, row: int, col: int, text: str, attr: int = 0) -> None:
    """``addstr`` raises if you write the bottom-right cell. Just swallow it."""
    try:
        stdscr.addstr(row, col, text, attr)
    except curses.error:
        pass


def _draw_list(stdscr, stories, selected, scroll, query, timespan, h, w):
    title = " GDELT News Geotagger "
    _safe_addstr(stdscr, 0, 0, title.center(w, "="),
                 curses.A_BOLD | curses.color_pair(1))
    info = f" Query: {query}   Timespan: {timespan}   Stories: {len(stories)} "
    _safe_addstr(stdscr, 1, 0, _truncate(info, w), curses.color_pair(1))
    _safe_addstr(stdscr, 2, 0, "-" * w)

    list_top = 3
    list_height = max(1, h - list_top - 2)

    if not stories:
        _safe_addstr(stdscr, list_top + 1, 2,
                     "No stories returned. Press 'r' to refresh, 'q' to quit.")
    else:
        end = min(scroll + list_height, len(stories))
        for idx in range(scroll, end):
            s = stories[idx]
            row = list_top + (idx - scroll)
            tag = f"[{(s.primary_country or 'Unknown')[:18]:<18}]"
            domain = f"({s.domain})" if s.domain else ""
            avail = w - len(tag) - len(domain) - 4
            title_text = _truncate(s.title or "(no title)", max(0, avail))
            line = f" {tag} {title_text} {domain}"
            line = _truncate(line, w).ljust(w)
            if idx == selected:
                attr = curses.color_pair(2) | curses.A_REVERSE | curses.A_BOLD
            else:
                attr = curses.color_pair(4)
            _safe_addstr(stdscr, row, 0, line, attr)

    footer = " \u2191/\u2193 navigate   PgUp/PgDn jump   Enter details   r refresh   q quit "
    _safe_addstr(stdscr, h - 1, 0, _truncate(footer, w).ljust(w),
                 curses.color_pair(1) | curses.A_REVERSE)


def _draw_detail(stdscr, story, h, w):
    title = " Story Details "
    _safe_addstr(stdscr, 0, 0, title.center(w, "="),
                 curses.A_BOLD | curses.color_pair(1))

    src = (f"{story.source_country} ({story.source_country_code})"
           if story.source_country_code else "(unknown)")
    fields = [
        ("Country",   story.primary_country or "Unknown"),
        ("Publisher", src),
        ("Mentioned", ", ".join(story.mentioned_countries) or "(none)"),
        ("Domain",    story.domain),
        ("Language",  story.language),
        ("Seen",      story.seendate),
        ("URL",       story.url),
    ]

    row = 2
    label_w = max(len(k) for k, _ in fields) + 2
    for label, value in fields:
        if row >= h - 2:
            break
        _safe_addstr(stdscr, row, 2, f"{label}:".ljust(label_w),
                     curses.A_BOLD | curses.color_pair(3))
        _safe_addstr(stdscr, row, 2 + label_w,
                     _truncate(str(value), w - 4 - label_w))
        row += 1

    if row < h - 3:
        row += 1
        _safe_addstr(stdscr, row, 2, "Title:", curses.A_BOLD | curses.color_pair(3))
        row += 1
        for line in textwrap.wrap(story.title or "(no title)", max(10, w - 6)):
            if row >= h - 2:
                break
            _safe_addstr(stdscr, row, 4, line)
            row += 1

    footer = " b/\u2190/Esc back   o open in browser   q quit "
    _safe_addstr(stdscr, h - 1, 0, _truncate(footer, w).ljust(w),
                 curses.color_pair(1) | curses.A_REVERSE)


def _flash(stdscr, msg: str) -> None:
    h, w = stdscr.getmaxyx()
    bar = f" {msg} ".center(min(len(msg) + 4, w))
    _safe_addstr(stdscr, h // 2, max(0, (w - len(bar)) // 2), bar,
                 curses.A_BOLD | curses.color_pair(2) | curses.A_REVERSE)
    stdscr.refresh()


def run_ui(initial_stories: list[Story],
           refresh: Callable[[Callable[[str], None]], list[Story]],
           query: str, timespan: str) -> None:
    """Launch the curses browser. ``refresh`` re-runs the GDELT fetch and
    accepts an ``on_wait`` callback so throttle/backoff status can be shown.
    """

    def _loop(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_CYAN, bg)
            curses.init_pair(2, curses.COLOR_YELLOW, bg)
            curses.init_pair(3, curses.COLOR_GREEN, bg)
            curses.init_pair(4, curses.COLOR_WHITE, bg)

        stories = list(initial_stories)
        selected = 0
        scroll = 0
        mode = "list"

        while True:
            h, w = stdscr.getmaxyx()
            list_height = max(1, h - 5)
            if stories:
                selected = max(0, min(selected, len(stories) - 1))
                if selected < scroll:
                    scroll = selected
                elif selected >= scroll + list_height:
                    scroll = selected - list_height + 1
            else:
                selected = 0
                scroll = 0

            stdscr.erase()
            if mode == "list":
                _draw_list(stdscr, stories, selected, scroll, query, timespan, h, w)
            else:
                _draw_detail(stdscr, stories[selected], h, w)
            stdscr.refresh()

            key = stdscr.getch()
            if key == ord('q'):
                return
            if mode == "list":
                if key in (curses.KEY_DOWN, ord('j')):
                    selected += 1
                elif key in (curses.KEY_UP, ord('k')):
                    selected -= 1
                elif key == curses.KEY_NPAGE:
                    selected += list_height
                elif key == curses.KEY_PPAGE:
                    selected -= list_height
                elif key == curses.KEY_HOME:
                    selected = 0
                elif key == curses.KEY_END and stories:
                    selected = len(stories) - 1
                elif key in (curses.KEY_ENTER, 10, 13) and stories:
                    mode = "detail"
                elif key == ord('r'):
                    def _status(msg: str) -> None:
                        stdscr.erase()
                        _draw_list(stdscr, stories, selected, scroll,
                                   query, timespan, h, w)
                        _flash(stdscr, msg)
                    _status("Refreshing from GDELT\u2026")
                    try:
                        stories = refresh(_status)
                    except Exception as exc:  # noqa: BLE001 - surface in UI
                        _flash(stdscr, f"Refresh failed: {exc} (press a key)")
                        stdscr.getch()
            else:  # detail mode
                if key in (curses.KEY_LEFT, 27, ord('b'), ord('h')):
                    mode = "list"
                elif key == ord('o') and stories[selected].url:
                    webbrowser.open(stories[selected].url)
                    _flash(stdscr, "Opened in browser")

    curses.wrapper(_loop)


# --- CLI -------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("query", nargs="?", default="world news",
                   help='GDELT query string. Must be a real keyword or phrase '
                        '(GDELT rejects "*"). Defaults to "world news".')
    p.add_argument("--max", type=int, default=75, dest="max_records",
                   help="Max records to return (GDELT caps at 250).")
    p.add_argument("--timespan", default="1h",
                   help="Lookback window: 15min, 1h (default), 24h, 1d, 7d.")
    p.add_argument("--no-ui", action="store_true",
                   help="Skip the interactive browser; print a text summary.")
    p.add_argument("--csv", help="Also write results to this CSV path.")
    p.add_argument("--json", dest="json_path",
                   help="Also write results to this JSON path.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    def _fetch(on_wait: Callable[[str], None] | None = None) -> list[Story]:
        return fetch_stories(args.query, args.max_records, args.timespan,
                             on_wait=on_wait)

    def _cli_status(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    try:
        stories = _fetch(_cli_status)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"Error fetching GDELT data: {exc}", file=sys.stderr)
        return 1

    if args.csv:
        write_csv(stories, args.csv)
        print(f"Wrote CSV: {args.csv}")
    if args.json_path:
        write_json(stories, args.json_path)
        print(f"Wrote JSON: {args.json_path}")

    if args.no_ui:
        print_summary(stories)
        return 0

    try:
        run_ui(stories, _fetch, args.query, args.timespan)
    except curses.error as exc:
        print(f"UI failed to start ({exc}); falling back to text summary.",
              file=sys.stderr)
        print_summary(stories)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
