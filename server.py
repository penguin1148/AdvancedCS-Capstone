"""Tiny local web server that marries the world-map UI with the GDELT
geotagger.

Run ``python server.py`` and open http://localhost:8000/ in a browser. The
server does two things:

1. Serves the static assets (``overview.html``, ``script.js``).
2. Exposes ``GET /api/news?country=<name>&timespan=<win>&max=<n>`` which
   reuses :func:`gdelt_geotagger.fetch_stories` to pull news mentioning the
   clicked country and returns them as JSON.

A small in-memory TTL cache sits in front of the GDELT call so rapid clicks
on the same country don't spam the upstream API (which enforces a ~6s floor
between requests).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gdelt_geotagger import fetch_stories

# Web UX tolerates much less patience than the CLI. Fail fast so the browser
# can show an error instead of hanging on a GDELT retry loop.
WEB_TIMEOUT = 15.0          # per-request HTTP timeout
WEB_MAX_RETRIES = 2         # total attempts on 429 / network errors
WEB_INITIAL_BACKOFF = 4.0   # first retry delay; doubles up to MAX_BACKOFF

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_FILES = {
    "/": ("overview.html", "text/html; charset=utf-8"),
    "/overview.html": ("overview.html", "text/html; charset=utf-8"),
    "/script.js": ("script.js", "application/javascript; charset=utf-8"),
}

# Cache GDELT responses for a short window. Keyed by (country, timespan, max).
_CACHE_TTL = 120.0  # seconds
_cache: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
# Serialize GDELT requests so concurrent clicks don't trip the rate limiter.
_gdelt_lock = threading.Lock()


def _build_query(country: str) -> str:
    """GDELT accepts a phrase; wrap multi-word country names in quotes so
    the API treats them as a single token."""
    country = country.strip()
    if " " in country or "'" in country:
        return f'"{country}"'
    return country


def _stories_for_country(country: str, timespan: str, max_records: int) -> list[dict]:
    key = (country.lower(), timespan, max_records)
    now = time.time()

    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    with _gdelt_lock:
        # Re-check cache under lock; another thread may have filled it.
        with _cache_lock:
            cached = _cache.get(key)
            if cached and time.time() - cached[0] < _CACHE_TTL:
                return cached[1]

        def _on_wait(msg: str) -> None:
            print(f"[gdelt:{country}] {msg}", file=sys.stderr, flush=True)

        print(f"[gdelt:{country}] fetching (timespan={timespan}, max={max_records})",
              file=sys.stderr, flush=True)
        stories = fetch_stories(
            _build_query(country),
            max_records=max_records,
            timespan=timespan,
            timeout=WEB_TIMEOUT,
            max_retries=WEB_MAX_RETRIES,
            initial_backoff=WEB_INITIAL_BACKOFF,
            on_wait=_on_wait,
        )
        payload = [s.to_dict() for s in stories]
        print(f"[gdelt:{country}] returned {len(payload)} stories",
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
        timespan = (params.get("timespan") or ["24h"])[0].strip() or "24h"
        try:
            max_records = int((params.get("max") or ["25"])[0])
        except ValueError:
            max_records = 25
        max_records = max(1, min(max_records, 75))

        if not country:
            self._send_json(400, {"error": "country parameter is required"})
            return

        try:
            stories = _stories_for_country(country, timespan, max_records)
        except Exception as exc:  # noqa: BLE001 - surface to client as JSON
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._send_json(502, {"error": str(exc), "country": country})
            return

        self._send_json(200, {
            "country": country,
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
