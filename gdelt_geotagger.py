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
import sys
import textwrap
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Callable

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

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


def fetch_stories(
    query: str,
    max_records: int = 75,
    timespan: str = "1h",
    mode: str = "ArtList",
    timeout: float = 30.0,
) -> list[Story]:
    """Query the GDELT DOC API and return geotagged ``Story`` records.

    ``query`` follows GDELT's query syntax (e.g. ``"climate change"`` or
    ``"election sourcelang:eng"``). ``timespan`` accepts values like ``15min``,
    ``1h``, ``24h``, ``1d``. We default to a tight one-hour window plus
    ``sort=datedesc`` so the freshest stories come back first.
    ``max_records`` is capped at 250 by the upstream API.
    """
    url = _build_query_url(query, max_records, timespan, mode)
    req = urllib.request.Request(url, headers={"User-Agent": "gdelt-geotagger/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    # GDELT occasionally returns an HTML error page on bad queries; guard for it.
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GDELT returned non-JSON response: {raw[:200]}") from exc

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
           refresh: Callable[[], list[Story]],
           query: str, timespan: str) -> None:
    """Launch the curses browser. ``refresh`` re-runs the GDELT fetch."""

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
                    _flash(stdscr, "Refreshing from GDELT\u2026")
                    try:
                        stories = refresh()
                    except Exception as exc:  # noqa: BLE001 - surface in UI
                        _flash(stdscr, f"Refresh failed: {exc}")
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
    p.add_argument("query", nargs="?", default="*",
                   help='GDELT query string. Defaults to "*" (latest worldwide).')
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

    def _fetch() -> list[Story]:
        return fetch_stories(args.query, args.max_records, args.timespan)

    try:
        stories = _fetch()
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
