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


def _fetch_gdelt(country: str, country_code: str, timespan: str,
                 max_records: int,
                 domains: list[str] | None = None) -> list[dict]:
    def _on_wait(msg: str) -> None:
        print(f"[gdelt:{country}] {msg}", file=sys.stderr, flush=True)

    print(f"[gdelt:{country}] fetching (timespan={timespan}, max={max_records}, "
          f"domains={domains or '-'})", file=sys.stderr, flush=True)
    stories = gdelt_fetch_stories(
        _build_gdelt_query(country, domains),
        max_records=max_records,
        timespan=timespan,
        timeout=WEB_TIMEOUT,
        max_retries=WEB_MAX_RETRIES,
        initial_backoff=WEB_INITIAL_BACKOFF,
        on_wait=_on_wait,
    )
    return [s.to_dict() for s in stories]


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

    def _handle_news(self, raw_query: str) -> None:
        params = urllib.parse.parse_qs(raw_query)
        country = (params.get("country") or [""])[0].strip()
        country_code = (params.get("country_code") or [""])[0].strip()
        timespan = (params.get("timespan") or ["24h"])[0].strip() or "24h"
        source = (params.get("source") or ["gdelt"])[0].strip().lower() or "gdelt"
        try:
            max_records = int((params.get("max") or ["25"])[0])
        except ValueError:
            max_records = 25
        max_records = max(1, min(max_records, 75))

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
