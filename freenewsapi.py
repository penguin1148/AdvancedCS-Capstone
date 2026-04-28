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


def _build_url(country_code: str, search: str, timespan: str,
               max_records: int, language: str) -> str:
    params: dict[str, str] = {
        "api_token": _api_key(),
        "limit": str(max(1, min(max_records, 100))),
        "language": language,
    }
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
    url = _build_url(country_code, search, timespan, max_records, language)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "gdelt-map/1.0",
            "Accept": "application/json",
            # Some deployments accept Bearer auth too; sending both is
            # harmless if only one is honored.
            "Authorization": f"Bearer {_api_key()}",
        },
    )
    if on_wait is not None:
        on_wait(f"FreeNewsApi: requesting {country_code or search}…")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # Surface the body when present — the API typically returns JSON
        # with a useful error message on 4xx.
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(
            f"FreeNewsApi returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"FreeNewsApi network error: {exc.reason}") from exc

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
