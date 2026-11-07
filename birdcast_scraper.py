#!/usr/bin/env python3
"""Fetch one BirdCast state snapshot and render it for a local BioGlow install."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://dashboard.birdcast.org/region/{}"
DEFAULT_CONFIG = "config.json"
DEFAULT_TIMEOUT = 20

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
STATE_CODES_BY_NAME = {name.casefold(): code for code, name in STATE_NAMES.items()}


class ScraperError(Exception):
    """An expected configuration, network, or source parsing failure."""


class PageTextParser(HTMLParser):
    """Convert rendered HTML into readable text while retaining block boundaries."""

    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "p", "section",
        "table", "title", "tr", "ul", "ol",
    }
    IGNORED_TAGS = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", p).strip() for p in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    parser = PageTextParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def normalize_region(value: str) -> tuple[str, str]:
    """Return (canonical code, display name) for a US state input."""
    raw = value.strip()
    upper = raw.upper()
    if upper.startswith("US-"):
        code = upper[3:]
    else:
        code = upper
    if code not in STATE_NAMES:
        code = STATE_CODES_BY_NAME.get(raw.casefold(), "")
    if code not in STATE_NAMES:
        raise ScraperError(
            f"unsupported region {value!r}; use a US state code such as US-CA or a state name"
        )
    return f"US-{code}", STATE_NAMES[code]


def _number(pattern: str, text: str) -> int | float | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    value = match.group(1).replace(",", "")
    return float(value) if "." in value else int(value)


def _line_after(label: str, text: str) -> str | None:
    for line in text.splitlines():
        if line.casefold().startswith(label.casefold()):
            value = line[len(label):].strip(" :")
            return value or None
    return None


def recorded_time_utc(value):
    from datetime import datetime, timezone, timedelta
    if not isinstance(value, str):
        return None
    try:
        date, abbreviation = value.rsplit(" ", 1)
        offsets = {"UTC": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
                   "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}
        offset = offsets[abbreviation]
        stamp = datetime.strptime(date, "%a, %b %d, %Y, %I:%M %p")
        return stamp.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(
            timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, KeyError):
        return None


def parse_snapshot(html: str, region_code: str, source_url: str, retrieved_at: str) -> dict[str, Any]:
    """Parse one rendered BirdCast page into the documented JSON shape."""
    region_code, fallback_name = normalize_region(region_code)
    text = html_to_text(html)
    compact = re.sub(r"\s+", " ", text)
    warnings: list[str] = []

    title_match = re.search(r"([^\n]+?)\s+-\s+Migration Dashboard\s+-\s+BirdCast", text)
    region_name = title_match.group(1).strip() if title_match else fallback_name
    unavailable = re.search(
        r"Migration data are not available for this region on this night", compact, re.IGNORECASE
    )

    birds_in_flight = _number(r"([\d,]+(?:\.\d+)?)\s*Birds\s+(?:now\s+)?in flight\s*\(est\.", compact)
    total_birds = _number(
        r"([\d,]+(?:\.\d+)?)\s*Birds\s+(?:have\s+)?crossed\b[^.\n]*?\(est\.", compact
    )
    speed = _number(r"Speed:\s*([\d,]+(?:\.\d+)?)\s*mph", compact)
    altitude = _number(r"Altitude:\s*([\d,]+(?:\.\d+)?)\s*ft", compact)

    direction: str | None = None
    direction_match = re.search(
        r"Direction:\s*(?:North|South|East|West|Northeast|Northwest|Southeast|Southwest)?\s*\(?\s*([NSEW]{1,3})\b",
        compact,
        re.IGNORECASE,
    )
    if direction_match:
        direction = direction_match.group(1).upper()

    recorded_match = re.search(
        r"Recorded:\s*(.+?)(?=\s+Not all birds|\s+Direction:|\s+Speed:|\s+Altitude:|$)",
        compact,
        re.IGNORECASE,
    )
    recorded_at = recorded_match.group(1).strip() if recorded_match else None
    updated_match = re.search(r"Last updated\s+(.+?)(?=\s+Live|\s+Total birds|$)", compact, re.IGNORECASE)
    updated_at = updated_match.group(1).strip() if updated_match else None

    selected_match = re.search(
        r"(?:Tonight|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) night),?\s+([A-Z][a-z]{2}\s+\d{1,2})",
        compact,
    )
    selected_night = selected_match.group(1) if selected_match else None

    if "Not all birds may fully cross a region" in compact:
        warnings.append("Birds in flight may exceed total birds crossed because not all birds finish crossing.")
    if "cannot be reliably determined when the number of birds in flight is low" in compact:
        warnings.append("Direction, speed, and altitude may be unreliable at low migration volume.")
    if unavailable:
        warnings.append("BirdCast reports that migration data are unavailable for this region on this night.")
    if birds_in_flight is None:
        warnings.append("Birds-in-flight metric is unavailable.")
    if total_birds is None:
        warnings.append("Total-birds-crossed metric is unavailable.")
    for label, value in (("direction", direction), ("speed", speed), ("altitude", altitude)):
        if value is None:
            warnings.append(f"{label.capitalize()} metric is unavailable.")

    status = "unavailable" if unavailable else "ok"
    if not unavailable and birds_in_flight is None and total_birds is None:
        raise ScraperError("BirdCast response did not contain a usable live migration metric")

    traffic = re.search(r"(?:Live migration traffic|Peak migration traffic)(.*?)(?=Direction:|Recorded:|$)",
                        compact, re.IGNORECASE)
    high_badge = bool(traffic and re.search(r"\bHigh\b", traffic.group(1)))

    return {
        "status": status,
        "region": {"code": region_code, "name": region_name},
        "source": {"url": source_url},
        "retrieved_at": retrieved_at,
        "selected_night": selected_night,
        "live_feed": bool(re.search(r"LIVE DATA FEED\s+Last updated", compact, re.IGNORECASE)),
        # BirdCast documents a High badge based on historical comparisons.
        # The absence of this badge is NOT a documented Low/Medium category.
        "migration_level": "high" if high_badge else "unknown",
        "metrics": {
            "birds_in_flight": birds_in_flight,
            "total_birds_crossed": total_birds,
            "direction": direction,
            "speed_mph": speed,
            "altitude_ft": altitude,
            "recorded_at": recorded_at,
            "recorded_at_utc": recorded_time_utc(recorded_at),
            "updated_at": updated_at,
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def fetch_html(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "BioGlow-BirdCast-Scraper/1.0 (+local Raspberry Pi project)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise ScraperError(f"BirdCast returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ScraperError(f"could not reach BirdCast: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ScraperError("BirdCast request timed out") from exc


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScraperError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScraperError(f"invalid JSON in config file {path}: {exc.msg}") from exc
    if not isinstance(config, dict):
        raise ScraperError("config must be a JSON object")
    if not isinstance(config.get("region"), str) or not config["region"].strip():
        raise ScraperError("config must contain a non-empty string field named 'region'")
    return config


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def print_summary(snapshot: dict[str, Any], output_path: Path) -> None:
    metrics = snapshot["metrics"]
    print(f"BirdCast — {snapshot['region']['name']} ({snapshot['region']['code']})")
    print(f"Birds in flight: {metrics['birds_in_flight'] if metrics['birds_in_flight'] is not None else 'unavailable'}")
    if metrics["total_birds_crossed"] is not None:
        print(f"Total birds crossed: {metrics['total_birds_crossed']}")
    details = []
    if metrics["direction"] is not None:
        details.append(f"direction {metrics['direction']}")
    if metrics["speed_mph"] is not None:
        details.append(f"speed {metrics['speed_mph']} mph")
    if metrics["altitude_ft"] is not None:
        details.append(f"altitude {metrics['altitude_ft']} ft")
    if details:
        print("Live traffic: " + ", ".join(details))
    if metrics["recorded_at"]:
        print(f"Recorded: {metrics['recorded_at']}")
    if snapshot["warnings"]:
        print("Warnings:")
        for warning in snapshot["warnings"]:
            print(f"  - {warning}")
    print(f"JSON: {output_path}")


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    configured_region = args.region or config["region"]
    region_code, _ = normalize_region(configured_region)
    source_url = BASE_URL.format(region_code)
    output_value = config.get("output_file", "birdcast_snapshot.json")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ScraperError("config field 'output_file' must be a non-empty string")
    output_path = Path(output_value).expanduser()
    if not output_path.is_absolute():
        output_path = config_path.parent / output_path

    from datetime import datetime, timezone

    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    html = fetch_html(source_url, timeout=args.timeout)
    snapshot = parse_snapshot(html, region_code, source_url, retrieved_at)
    atomic_write_json(output_path, snapshot)
    try:
        written_snapshot = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScraperError(f"could not read back output JSON: {exc}") from exc
    print_summary(written_snapshot, output_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch one live BirdCast migration snapshot.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="JSON config path (default: config.json)")
    parser.add_argument("--region", help="Optional US region override, e.g. US-CA")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Network timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        return run(args)
    except ScraperError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
