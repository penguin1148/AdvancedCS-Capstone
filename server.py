"""Tiny local web server that marries the world-map UI with two news
sources: the GDELT geotagger and FreeNewsApi.io.

Run ``python server.py`` and open http://localhost:8000/ in a browser.
The server does two things:

1. Serves the static assets (``overview.html``, ``script.js``).
2. Exposes ``GET /api/news`` which proxies to either source. Query params:

   - ``country``    - country display name (required)
   - ``country_code`` - ISO 3166 alpha-2 code (optional, used by FreeNewsApi)
   - ``source``    - ``gdelt`` (default) or ``freenewsapi``
   - ``timespan`` - lookback window (e.g. ``1h``, ``24h``, ``7d``)
   - ``max``      - max records to return

A small in-memory TTL cache sits in front of upstream calls so rapid
clicks on the same country don't hammer the providers (GDELT enforces a
~6s floor; FreeNewsApi has a 5,000/day budget).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import freenewsapi
from gdelt_geotagger import fetch_stories as gdelt_fetch_stories

# Web UX tolerates much less patience than the CLI. Fail fast so the browser
# can show an error instead of hanging on a GDELT retry loop.
WEB_TIMEOUT = 15.0          # per-request HTTP timeout
WEB_MAX_RETRIES = 2         # total attempts on 429 / network errors
WEB_INITIAL_BACKOFF = 4.0   # first retry delay; doubles up to MAX_BACKOFF

VALID_SOURCES = {"gdelt", "freenewsapi"}

# Cheap model for the country-comparison feature. Pulled from the env var
# ANTHROPIC_API_KEY at request time so the key is never committed to source.
COMPARE_MODEL = "claude-haiku-4-5"
COMPARE_MAX_STORIES = 50      # cap per country to bound prompt size
COMPARE_MAX_TOKENS = 1500

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_FILES = {
    "/": ("overview.html", "text/html; charset=utf-8"),
    "/overview.html": ("overview.html", "text/html; charset=utf-8"),
    "/script.js": ("script.js", "application/javascript; charset=utf-8"),
    "/trustedsources.js": ("trustedsources.js", "application/javascript; charset=utf-8"),
}

# Cache responses for a short window. Keyed by (source, country, code, timespan, max).
_CACHE_TTL = 120.0  # seconds
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
# Serialize GDELT requests so concurrent clicks don't trip the rate limiter.
# FreeNewsApi has no such floor, so it gets its own (uncontended) lock.
_source_locks: dict[str, threading.Lock] = {
    "gdelt": threading.Lock(),
    "freenewsapi": threading.Lock(),
}


def _build_gdelt_query(country: str, domains: list[str] | None = None) -> str:
    """Build a GDELT DOC query.

    When ``domains`` is provided, the query selects recent stories *from*
    those publishers (e.g. ``(domain:reuters.com OR domain:apnews.com)``),
    so the API itself returns trusted-source stories instead of us
    filtering after the fact. Falls back to a country-name keyword search
    when no domain list is given.
    """
    if domains:
        parts = " OR ".join(f"domain:{d}" for d in domains)
        return f"({parts})"
    country = country.strip()
    if " " in country or "'" in country:
        return f'"{country}"'
    return country


def _balance_by_domain(stories: list[dict], domains: list[str],
                       max_records: int) -> list[dict]:
    """Round-robin stories across the given trusted domains so the result is
    a balanced mix instead of being dominated by whichever publisher had the
    most recent activity. A story matches a domain by exact equality or
    subdomain (so ``www.reuters.com`` lands in ``reuters.com``).
    """
    buckets: dict[str, list[dict]] = {d: [] for d in domains}
    for story in stories:
        sd = (story.get("domain") or "").lower()
        for d in domains:
            if sd == d or sd.endswith("." + d):
                buckets[d].append(story)
                break

    out: list[dict] = []
    while len(out) < max_records:
        progress = False
        for d in domains:
            if buckets[d]:
                out.append(buckets[d].pop(0))
                progress = True
                if len(out) >= max_records:
                    break
        if not progress:
            break
    return out


def _fetch_gdelt(country: str, country_code: str, timespan: str,
                 max_records: int,
                 domains: list[str] | None = None) -> list[dict]:
    def _on_wait(msg: str) -> None:
        print(f"[gdelt:{country}] {msg}", file=sys.stderr, flush=True)

    # When restricting to a trusted-domain allowlist, pull a much larger
    # upstream pool (GDELT caps at 250) so each publisher has enough stories
    # to contribute when we round-robin them into a balanced result.
    upstream_max = 250 if domains else max_records
    print(f"[gdelt:{country}] fetching (timespan={timespan}, "
          f"upstream_max={upstream_max}, return_max={max_records}, "
          f"domains={domains or '-'})", file=sys.stderr, flush=True)
    stories = gdelt_fetch_stories(
        _build_gdelt_query(country, domains),
        max_records=upstream_max,
        timespan=timespan,
        timeout=WEB_TIMEOUT,
        max_retries=WEB_MAX_RETRIES,
        initial_backoff=WEB_INITIAL_BACKOFF,
        on_wait=_on_wait,
    )
    payload = [s.to_dict() for s in stories]
    if domains:
        payload = _balance_by_domain(payload, domains, max_records)
    return payload


def _fetch_freenewsapi(country: str, country_code: str, timespan: str,
                       max_records: int,
                       domains: list[str] | None = None) -> list[dict]:
    # FreeNewsApi has its own source filter; the trusted-domain list is
    # GDELT-only, so we ignore it here.
    del domains
    def _on_wait(msg: str) -> None:
        print(f"[freenewsapi:{country}] {msg}", file=sys.stderr, flush=True)

    print(f"[freenewsapi:{country}] fetching (code={country_code}, "
          f"timespan={timespan}, max={max_records})",
          file=sys.stderr, flush=True)
    stories = freenewsapi.fetch_stories(
        country_code=country_code,
        country_name=country,
        # If we don't have an ISO code, fall back to keyword search by name.
        search="" if country_code else country,
        timespan=timespan,
        max_records=max_records,
        timeout=WEB_TIMEOUT,
        on_wait=_on_wait,
    )
    return [s.to_dict() for s in stories]


_FETCHERS = {
    "gdelt": _fetch_gdelt,
    "freenewsapi": _fetch_freenewsapi,
}


def _stories_for_country(source: str, country: str, country_code: str,
                         timespan: str, max_records: int,
                         domains: tuple[str, ...] = ()) -> list[dict]:
    key = (source, country.lower(), country_code.lower(), timespan,
           max_records, domains)
    now = time.time()

    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    with _source_locks[source]:
        # Re-check cache under lock; another thread may have filled it.
        with _cache_lock:
            cached = _cache.get(key)
            if cached and time.time() - cached[0] < _CACHE_TTL:
                return cached[1]

        payload = _FETCHERS[source](country, country_code, timespan,
                                    max_records, domains=list(domains))
        print(f"[{source}:{country}] returned {len(payload)} stories",
              file=sys.stderr, flush=True)

        with _cache_lock:
            _cache[key] = (time.time(), payload)
        return payload


def _format_stories_for_prompt(stories: list[dict]) -> str:
    lines = []
    for s in stories[:COMPARE_MAX_STORIES]:
        title = (s.get("title") or "").strip() or "(untitled)"
        domain = s.get("domain") or "?"
        mentioned = ", ".join(s.get("mentioned_countries") or [])
        line = f"- [{domain}] {title}"
        if mentioned:
            line += f"  (mentions: {mentioned})"
        lines.append(line)
    return "\n".join(lines) if lines else "(no stories)"


def _compare_news(a_name: str, a_stories: list[dict],
                  b_name: str, b_stories: list[dict]) -> str:
    """Ask a cheap Claude model to compare two countries' news batches.

    Reads the API key from ANTHROPIC_API_KEY (never hard-coded). Raises
    RuntimeError with a clean message if the key or SDK is missing.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export it before starting the "
            "server, e.g. `export ANTHROPIC_API_KEY=sk-ant-...`."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "The 'anthropic' package is not installed. Run `pip install anthropic`."
        ) from exc

    client = anthropic.Anthropic()
    system = (
        "You are a media-bias analyst. You compare how two countries' trusted "
        "news outlets cover the world, focusing on where each country is "
        "mentioned, how framing differs between sources, and potential biases. "
        "Be concise and specific, cite outlet domains, and never invent stories "
        "that are not in the provided lists."
    )
    user = (
        f"Compare the recent news coverage from these two countries' trusted "
        f"sources.\n\n"
        f"## {a_name} sources\n{_format_stories_for_prompt(a_stories)}\n\n"
        f"## {b_name} sources\n{_format_stories_for_prompt(b_stories)}\n\n"
        f"Provide three short markdown sections:\n"
        f"1. **Overlap** — topics or events where BOTH {a_name} and {b_name} "
        f"appear, or that both sets of outlets cover.\n"
        f"2. **Framing** — how shared topics are presented differently across "
        f"the outlets.\n"
        f"3. **Potential biases** — slants, omissions, or loaded language, each "
        f"tied to an outlet domain.\n\n"
        f"Keep the whole response under ~400 words."
    )

    message = client.messages.create(
        model=COMPARE_MODEL,
        max_tokens=COMPARE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "GdeltMapServer/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib sig
        # Drop 200s but keep error lines so users can see what's happening.
        status = args[1] if len(args) >= 2 else ""
        if status.startswith("2"):
            return
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.address_string(), self.log_date_time_string(), format % args))

    def _send_json(self, status: int, body: dict | list) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = os.path.join(HERE, filename)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib spelling
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in STATIC_FILES:
            filename, ctype = STATIC_FILES[path]
            self._send_static(filename, ctype)
            return

        if path == "/api/news":
            self._handle_news(parsed.query)
            return

        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib spelling
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/compare":
            self._handle_compare()
            return
        self.send_error(404, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _handle_compare(self) -> None:
        body = self._read_json_body()
        a = body.get("a") or {}
        b = body.get("b") or {}
        a_name = (a.get("name") or "").strip()
        b_name = (b.get("name") or "").strip()
        a_stories = a.get("stories") or []
        b_stories = b.get("stories") or []

        if not a_name or not b_name:
            self._send_json(400, {"error": "two countries (a, b) are required"})
            return
        if a_name == b_name:
            self._send_json(400, {"error": "pick two different countries"})
            return
        if not a_stories or not b_stories:
            self._send_json(400, {
                "error": "both countries need fetched stories to compare",
            })
            return

        try:
            analysis = _compare_news(a_name, a_stories, b_name, b_stories)
        except Exception as exc:  # noqa: BLE001 - surface to client as JSON
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._send_json(502, {"error": str(exc)})
            return

        self._send_json(200, {
            "model": COMPARE_MODEL,
            "a": a_name,
            "b": b_name,
            "analysis": analysis,
        })

    def _handle_news(self, raw_query: str) -> None:
        params = urllib.parse.parse_qs(raw_query)
        country = (params.get("country") or [""])[0].strip()
        country_code = (params.get("country_code") or [""])[0].strip()
        timespan = (params.get("timespan") or ["24h"])[0].strip() or "24h"
        source = (params.get("source") or ["gdelt"])[0].strip().lower() or "gdelt"
        try:
            max_records = int((params.get("max") or ["50"])[0])
        except ValueError:
            max_records = 50
        max_records = max(1, min(max_records, 100))

        # Optional comma-separated allowlist of publisher domains. The client
        # passes this for GDELT so the upstream query targets trusted sources
        # directly instead of returning random stories that we'd then drop.
        raw_domains = (params.get("domains") or [""])[0]
        domains = tuple(
            d.strip().lower() for d in raw_domains.split(",") if d.strip()
        )

        if not country:
            self._send_json(400, {"error": "country parameter is required"})
            return
        if source not in VALID_SOURCES:
            self._send_json(400, {
                "error": f"unknown source {source!r}; "
                         f"use one of {sorted(VALID_SOURCES)}",
            })
            return

        try:
            stories = _stories_for_country(source, country, country_code,
                                           timespan, max_records, domains)
        except Exception as exc:  # noqa: BLE001 - surface to client as JSON
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._send_json(502, {
                "error": str(exc), "country": country, "source": source,
            })
            return

        self._send_json(200, {
            "source": source,
            "country": country,
            "country_code": country_code,
            "timespan": timespan,
            "count": len(stories),
            "stories": stories,
        })


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    main(args.host, args.port)
