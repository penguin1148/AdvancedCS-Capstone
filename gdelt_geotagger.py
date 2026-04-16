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
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field

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
    max_records: int = 50,
    timespan: str = "1d",
    mode: str = "ArtList",
    timeout: float = 30.0,
) -> list[Story]:
    """Query the GDELT DOC API and return geotagged ``Story`` records.

    ``query`` follows GDELT's query syntax (e.g. ``"climate change"`` or
    ``"election sourcelang:eng"``). ``timespan`` accepts values like ``24h``,
    ``1d``, ``7d``. ``max_records`` is capped at 250 by the upstream API.
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("query", help='GDELT query string, e.g. "climate change"')
    p.add_argument("--max", type=int, default=50, dest="max_records",
                   help="Max records to return (GDELT caps at 250).")
    p.add_argument("--timespan", default="1d",
                   help="Lookback window, e.g. 24h, 1d, 7d, 1w.")
    p.add_argument("--csv", help="Write results to this CSV path.")
    p.add_argument("--json", dest="json_path", help="Write results to this JSON path.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        stories = fetch_stories(args.query, args.max_records, args.timespan)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"Error fetching GDELT data: {exc}", file=sys.stderr)
        return 1

    print_summary(stories)
    if args.csv:
        write_csv(stories, args.csv)
        print(f"\nWrote CSV: {args.csv}")
    if args.json_path:
        write_json(stories, args.json_path)
        print(f"Wrote JSON: {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
