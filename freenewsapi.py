"""News fetcher backed by FreeNewsApi.io.

Implements the same shape as :mod:`gdelt_geotagger` so :mod:`server` can
swap between sources at request time. ``fetch_stories`` returns a list of
:class:`gdelt_geotagger.Story` records to keep the JSON envelope identical
on the wire.

Auth: the API key is read from the ``FREENEWSAPI_KEY`` environment
variable when set; otherwise we fall back to the project-default key
embedded below. Keep this default scoped to the capstone — rotate it for
anything public-facing.

The FreeNewsApi.io schema is parsed permissively: we accept ``data``,
``articles`` or ``results`` arrays, and for each article we try several
common field aliases (``published_at`` / ``publishedAt`` / ``published``,
``source`` / ``source_id`` / ``source_name``, ...). That keeps us
resilient to small response-shape differences.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from gdelt_geotagger import Story

# Project default. Override locally with `export FREENEWSAPI_KEY=...`.
_DEFAULT_API_KEY = "bcc59dea3871dffa62e0b128aa4dfd9860d5b6e35e2376a6529fce222ce67e2c"
FREENEWSAPI_BASE = "https://api.freenewsapi.io/v1/news"


# Map a 7d/24h/1h GDELT-style timespan to an ISO timestamp suitable for
# the ``published_after`` parameter. We accept the same vocabulary as the
# GDELT side so the UI selector keeps working unchanged.
_TIMESPAN_SECONDS = {
    "1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400,
    "1d": 86400, "3d": 259200, "7d": 604800, "1w": 604800,
}


def _published_after(timespan: str) -> str | None:
    """Convert e.g. ``"24h"`` to an ISO-8601 UTC string for ``published_after``.

    Returns ``None`` for unknown/empty values so the API picks a default.
    """
    secs = _TIMESPAN_SECONDS.get((timespan or "").strip().lower())
    if not secs:
        return None
    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=secs)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


def _api_key() -> str:
    return os.environ.get("FREENEWSAPI_KEY", "").strip() or _DEFAULT_API_KEY


# We don't have authoritative docs for the exact auth scheme, so we try
# every common name on the same request. Unknown query params and headers
# are ignored by reasonable APIs, and whichever name the upstream actually
# expects will be honored. If a 401 still comes back, we then iterate
# `_FALLBACK_QUERY_NAMES` one at a time as a last resort.
_QUERY_KEY_NAMES = ("api_token", "apikey", "api_key", "apiKey", "key", "token", "access_key")
_FALLBACK_QUERY_NAMES = ("apikey", "api_key", "apiKey", "key", "token", "access_key")


def _build_url(country_code: str, search: str, timespan: str,
               max_records: int, language: str,
               key_name: str | None = None) -> str:
    params: dict[str, str] = {
        "limit": str(max(1, min(max_records, 100))),
        "language": language,
    }
    key = _api_key()
    if key_name is None:
        # First try: send the key under every common name at once.
        for name in _QUERY_KEY_NAMES:
            params[name] = key
    else:
        params[key_name] = key
    code = (country_code or "").strip().lower()
    if code:
        # Different deployments call this `country` or `locale`; send both
        # so we don't depend on the exact spelling.
        params["country"] = code
        params["locale"] = code
    if search:
        params["search"] = search
    cutoff = _published_after(timespan)
    if cutoff:
        params["published_after"] = cutoff
    return f"{FREENEWSAPI_BASE}?{urllib.parse.urlencode(params)}"


def _extract_articles(payload: dict) -> list[dict]:
    """Return the article list from a FreeNewsApi response, regardless of
    which wrapper key it uses."""
    for key in ("data", "articles", "results", "news"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def _first(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v).strip()
    return default


def _normalize_published(raw: str) -> str:
    """Reuse GDELT's ``YYYYMMDDTHHMMSSZ`` format so the existing JS
    formatter renders dates consistently across sources."""
    if not raw:
        return ""
    # FreeNewsApi typically returns ISO-8601, e.g. "2026-04-28T11:23:45.000000Z".
    import datetime as _dt
    s = raw.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return raw  # let the JS layer fall through to displaying raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    dt = dt.astimezone(_dt.timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _to_story(article: dict, country_name: str) -> Story:
    title = _first(article, "title", "headline")
    url = _first(article, "url", "link")
    published = _normalize_published(_first(article, "published_at",
                                            "publishedAt", "published",
                                            "date", "pubDate"))
    domain = _first(article, "source", "source_name", "source_id",
                    "publisher", "domain")
    language = _first(article, "language", "lang", default="")
    locale = _first(article, "locale", "country", default="").upper()

    return Story(
        title=title,
        url=url,
        seendate=published,
        domain=domain,
        language=language,
        # We don't have a FIPS code from FreeNewsApi; keep the field
        # populated with the locale (often ISO) so the UI has something.
        source_country_code=locale,
        source_country=country_name or locale,
        mentioned_countries=[country_name] if country_name else [],
        primary_country=country_name or locale or "Unknown",
    )


def _auth_headers() -> dict[str, str]:
    """Send the key under every header convention we've seen in the wild
    so the upstream picks up whichever one it expects."""
    key = _api_key()
    return {
        "User-Agent": "gdelt-map/1.0",
        "Accept": "application/json",
        "X-Api-Key": key,
        "X-API-Key": key,
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _do_request(url: str, timeout: float) -> bytes:
    """Open ``url`` and return the body, mapping HTTP/URL errors to
    RuntimeError with the upstream response body included when available."""
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            detail = ""
        # Re-raise as a typed wrapper that carries the status code so the
        # caller can decide whether to fall back to a different auth name.
        err = RuntimeError(
            f"FreeNewsApi returned HTTP {exc.code}: {detail or exc.reason}"
        )
        err.status = exc.code  # type: ignore[attr-defined]
        err.body = detail       # type: ignore[attr-defined]
        raise err from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"FreeNewsApi network error: {exc.reason}") from exc


def fetch_stories(
    country_code: str = "",
    country_name: str = "",
    search: str = "",
    timespan: str = "24h",
    max_records: int = 25,
    language: str = "en",
    timeout: float = 15.0,
    on_wait: Callable[[str], None] | None = None,
) -> list[Story]:
    """Fetch articles for ``country_code`` (ISO-3166 alpha-2) from FreeNewsApi.

    Either ``country_code`` or ``search`` should be set; we'll use both if
    both are provided. ``country_name`` is only used to populate the
    ``primary_country`` field so the UI labels stories consistently.
    """
    if on_wait is not None:
        on_wait(f"FreeNewsApi: requesting {country_code or search}…")

    # First attempt: shotgun every known query-param name + every header.
    url = _build_url(country_code, search, timespan, max_records, language)
    attempts: list[tuple[str, str]] = [("shotgun", url)]
    # Fallbacks: one query-param name at a time, in case the upstream
    # rejects requests carrying *unknown* parameters (some API gateways do).
    for name in _FALLBACK_QUERY_NAMES:
        attempts.append((name, _build_url(
            country_code, search, timespan, max_records, language,
            key_name=name,
        )))

    last_err: Exception | None = None
    for label, attempt_url in attempts:
        try:
            raw = _do_request(attempt_url, timeout)
            break
        except RuntimeError as exc:
            last_err = exc
            status = getattr(exc, "status", None)
            body = (getattr(exc, "body", "") or "").lower()
            # Only retry on auth failures; bail on anything else immediately.
            if status not in (400, 401, 403) or "key" not in body:
                raise
            if on_wait is not None:
                on_wait(f"FreeNewsApi: auth via {label} rejected, trying next…")
    else:
        # Every fallback failed; surface the most recent error.
        assert last_err is not None
        raise last_err

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        snippet = raw[:200].decode("utf-8", errors="replace")
        raise RuntimeError(
            f"FreeNewsApi returned non-JSON: {snippet!r}"
        ) from exc

    if isinstance(payload, dict) and payload.get("error"):
        # Some APIs put errors in the body even on a 200 status.
        raise RuntimeError(f"FreeNewsApi error: {payload.get('error')}")

    articles = _extract_articles(payload) if isinstance(payload, dict) else []
    return [_to_story(a, country_name) for a in articles if isinstance(a, dict)]
