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

# GDELT is the user's preferred source, so be patient with it: a single 429
# should not knock us straight into the FreeNewsApi fallback. We give it a
# reasonable timeout and multiple retries with progressive backoff so
# transient rate-limit windows ride themselves out.
WEB_TIMEOUT = 18.0          # per-request HTTP timeout
WEB_MAX_RETRIES = 3         # total attempts on 429 / network errors
WEB_INITIAL_BACKOFF = 3.0   # first retry delay; doubles up to WEB_MAX_BACKOFF
WEB_MAX_BACKOFF = 12.0      # cap any single backoff at this many seconds

VALID_SOURCES = {"gdelt", "freenewsapi"}

# When an upstream returns 429 / errors out *after* exhausting retries, mark
# it unhealthy for this long so the next request goes to the fallback (or
# stale cache) instead of repeating the same slow failure. Short enough that
# we recover within a click or two once the rate-limit window expires.
_UNHEALTHY_COOLDOWN = 15.0
_unhealthy_until: dict[str, float] = {"gdelt": 0.0, "freenewsapi": 0.0}

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_FILES = {
    "/": ("better_overview.html", "text/html; charset=utf-8"),
    "/better_overview.html": ("better_overview.html", "text/html; charset=utf-8"),
    "/overview.html": ("overview.html", "text/html; charset=utf-8"),
    "/script.js": ("script.js", "application/javascript; charset=utf-8"),
    "/trustedsources.js": ("trustedsources.js", "application/javascript; charset=utf-8"),
}

# Cache responses. The fresh window is what we serve without re-querying;
# the stale window is a fallback we serve when the upstream errors out so
# the user always sees stories instead of a red error message. The user has
# explicitly opted in to "not the very latest news" in exchange for speed,
# so these are sized aggressively.
_CACHE_TTL = 1800.0   # 30 min: fresh enough, fast enough
_STALE_TTL = 21600.0  # 6 hours: served only when a refetch fails
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


def _balance_generically(stories: list[dict], max_records: int) -> list[dict]:
    """Round-robin across whichever domains showed up in the response.

    GDELT keyword searches frequently return ~20 near-duplicates of the same
    wire story from a single publisher. Bucketing by domain and rotating
    through them gives the user a more diverse mix without any allowlist
    configured.
    """
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []  # preserve first-seen order so results stay stable
    for story in stories:
        d = (story.get("domain") or "_").lower()
        if d not in buckets:
            buckets[d] = []
            order.append(d)
        buckets[d].append(story)

    out: list[dict] = []
    while len(out) < max_records:
        progress = False
        for d in order:
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

    # Always pull a generous upstream pool (GDELT caps at 250) so we can
    # round-robin publishers into a diverse result, even when the user has
    # not configured a trusted-domains allowlist.
    upstream_max = 250
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
        max_backoff=WEB_MAX_BACKOFF,
        on_wait=_on_wait,
    )
    payload = [s.to_dict() for s in stories]
    if domains:
        return _balance_by_domain(payload, domains, max_records)
    return _balance_generically(payload, max_records)


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

# Per-source fallback order. If the user-selected source is unhealthy or
# the live fetch fails, we try the next entry before giving up. This is
# what keeps the UI usable when GDELT is in a multi-minute 429 window.
_FALLBACK_CHAIN = {
    "gdelt": ["gdelt", "freenewsapi"],
    "freenewsapi": ["freenewsapi", "gdelt"],
}


def _try_fetch_one(source: str, country: str, country_code: str,
                   timespan: str, max_records: int,
                   domains: tuple[str, ...]) -> list[dict] | None:
    """Attempt one source. Returns the payload on success, the most recent
    cached payload (fresh or stale) on failure, or ``None`` when neither is
    available (so the caller can try the next source in the chain).

    Mutates ``_cache`` and ``_unhealthy_until`` as side effects.
    """
    # GDELT-only flags don't apply to FreeNewsApi, so don't let them split
    # the cache key for FreeNewsApi.
    effective_domains = domains if source == "gdelt" else ()
    key = (source, country.lower(), country_code.lower(), timespan,
           max_records, effective_domains)
    now = time.time()

    with _cache_lock:
        cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    # In cooldown — don't bother hitting the upstream; degrade to stale.
    if now < _unhealthy_until.get(source, 0.0):
        if cached and now - cached[0] < _STALE_TTL:
            print(f"[{source}:{country}] cooldown active; serving stale "
                  f"({len(cached[1])} stories)", file=sys.stderr, flush=True)
            return cached[1]
        return None

    with _source_locks[source]:
        with _cache_lock:
            cached = _cache.get(key)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1]

        try:
            payload = _FETCHERS[source](country, country_code, timespan,
                                        max_records,
                                        domains=list(effective_domains))
        except Exception as exc:  # noqa: BLE001 - try the fallback
            _unhealthy_until[source] = time.time() + _UNHEALTHY_COOLDOWN
            print(f"[{source}:{country}] fetch failed ({exc})",
                  file=sys.stderr, flush=True)
            if cached and time.time() - cached[0] < _STALE_TTL:
                print(f"[{source}:{country}] serving stale "
                      f"({len(cached[1])} stories)",
                      file=sys.stderr, flush=True)
                return cached[1]
            return None

        print(f"[{source}:{country}] returned {len(payload)} stories",
              file=sys.stderr, flush=True)
        with _cache_lock:
            _cache[key] = (time.time(), payload)
        return payload


def _stories_for_country(source: str, country: str, country_code: str,
                         timespan: str, max_records: int,
                         domains: tuple[str, ...] = ()) -> list[dict]:
    chain = _FALLBACK_CHAIN.get(source, [source])
    for candidate in chain:
        result = _try_fetch_one(candidate, country, country_code, timespan,
                                max_records, domains)
        if result is not None:
            if candidate != source:
                print(f"[{source}:{country}] fell back to {candidate}",
                      file=sys.stderr, flush=True)
            return result
    raise RuntimeError(
        "All news sources are unavailable right now; try again shortly."
    )


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


# Countries most users click first. The background prefetcher quietly keeps
# their GDELT cache entries warm so the click path almost always hits the
# 30-min in-memory cache instead of waiting on a live GDELT request.
POPULAR_COUNTRIES: tuple[tuple[str, str], ...] = (
    ("United States", "US"), ("United Kingdom", "GB"), ("China", "CN"),
    ("Russia", "RU"), ("France", "FR"), ("Germany", "DE"),
    ("India", "IN"), ("Brazil", "BR"), ("Japan", "JP"),
    ("Canada", "CA"), ("Australia", "AU"), ("Mexico", "MX"),
    ("Italy", "IT"), ("Spain", "ES"), ("Republic of Korea", "KR"),
    ("Indonesia", "ID"), ("Turkey", "TR"), ("Saudi Arabia", "SA"),
    ("Egypt", "EG"), ("South Africa", "ZA"), ("Nigeria", "NG"),
    ("Argentina", "AR"), ("Iran", "IR"), ("Israel", "IL"),
    ("Ukraine", "UA"), ("Poland", "PL"), ("Netherlands", "NL"),
    ("Sweden", "SE"), ("Pakistan", "PK"), ("Vietnam", "VN"),
)


# The user click sends max=75 (see script.js). The prefetcher uses the same
# value so its cache entries are keyed identically and the click lands on a
# direct cache hit.
_PREFETCH_MAX = 75
_PREFETCH_TIMESPAN = "24h"


def _prefetch_one(name: str, code: str) -> None:
    """Refresh the GDELT cache entry for one country without touching the
    cooldown machinery. A prefetch 429 must never poison a user click — if
    GDELT pushes back, we just skip this country and try the next one.
    """
    key = ("gdelt", name.lower(), code.lower(), _PREFETCH_TIMESPAN,
           _PREFETCH_MAX, ())
    with _cache_lock:
        cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return  # still fresh; nothing to do

    with _source_locks["gdelt"]:
        # Re-check under lock; a concurrent user click may have just filled it.
        with _cache_lock:
            cached = _cache.get(key)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return

        try:
            payload = _fetch_gdelt(name, code, _PREFETCH_TIMESPAN,
                                   _PREFETCH_MAX, domains=[])
        except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
            print(f"[prefetch:{name}] {exc}", file=sys.stderr, flush=True)
            return

        with _cache_lock:
            _cache[key] = (time.time(), payload)
        print(f"[prefetch:{name}] warmed cache ({len(payload)} stories)",
              file=sys.stderr, flush=True)


def _prefetch_loop() -> None:
    """Rotate through POPULAR_COUNTRIES forever, refreshing entries that have
    expired. Sleeps generously between countries so the prefetcher doesn't
    contribute to GDELT's perception of us as a hot caller — we'd rather take
    half an hour to fully warm the cache than get the IP rate-limited.
    """
    import itertools
    time.sleep(10.0)  # let the HTTP server settle first
    for name, code in itertools.cycle(POPULAR_COUNTRIES):
        _prefetch_one(name, code)
        # ~10s between iterations keeps us well under GDELT's per-IP load
        # threshold, and yields the lock so user clicks always win the race.
        time.sleep(10.0)


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    prefetch_thread = threading.Thread(
        target=_prefetch_loop, name="gdelt-prefetch", daemon=True,
    )
    prefetch_thread.start()
    print(f"Serving on http://{host}:{port}/  (Ctrl-C to stop)")
    print(f"Prefetching GDELT cache for {len(POPULAR_COUNTRIES)} popular "
          f"countries in the background.")
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
