"""Read-only probe of a real GeoPulse server against docs/DEVELOPMENT.md's GeoPulse API notes.

Issues GET requests only. Output is redacted: JSON is printed as a type
skeleton (no coordinates, names, emails or ids); only enum-like fields and
timestamp *formats* are shown verbatim.

    docker/probe.sh            # reads .env.local (GEOPULSE_URL, GEOPULSE_READ_TOKEN)
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.geopulse.api import GeoPulseClient  # noqa: E402
from custom_components.geopulse.const import (  # noqa: E402
    API_PATH_FRIENDS,
    API_PATH_GPS_LAST_KNOWN_POSITION,
    API_PATH_GPS_POINTS,
    API_PATH_GPS_SOURCE,
    SOURCE_TYPE_HOME_ASSISTANT,
)

# Values safe to print: enums, flags and envelope status.
VERBATIM_KEYS = {
    "type", "sourceType", "status", "connectionType", "active",
    "friendSharesLiveLocation", "friendSharesTimeline", "hasPayloadEncryptionSecret",
    "filterInaccurateData", "enableDuplicateDetection", "latestActivityType",
}
TIMESTAMP_KEYS = {"timestamp", "lastSeen", "createdAt", "updatedAt"}
findings: list[str] = []


def skeleton(value: Any, key: str | None = None, depth: int = 0) -> str:
    pad = "  " * depth
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = [f"{pad}  {k}: {skeleton(v, k, depth + 1).lstrip()}" for k, v in value.items()]
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        return f"[{len(value)} items] first = {skeleton(value[0], key, depth)}"
    if value is None:
        return "null"
    if key in VERBATIM_KEYS:
        return repr(value)
    if key in TIMESTAMP_KEYS and isinstance(value, str):
        return f"<timestamp format {re.sub(r'[0-9]', '9', value)!r}>"
    if isinstance(value, str):
        return f"<str len {len(value)}>"
    return f"<{type(value).__name__}>"


def note(ok: bool, message: str) -> None:
    findings.append(f"{'OK  ' if ok else 'FAIL'} {message}")


async def raw_get(session: aiohttp.ClientSession, base: str, token: str, path: str, **params: Any) -> Any:
    async with session.get(
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, params=params or None
    ) as resp:
        body = await resp.json(content_type=None)
        print(f"\n### GET {path} {params or ''} -> {resp.status} ({resp.content_type})")
        print(skeleton(body))
        return body


def age(ts: datetime) -> str:
    minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    return f"{minutes:.0f} min ago" if minutes < 120 else f"{minutes / 60:.1f} h ago"


async def main() -> None:
    base = os.environ["GEOPULSE_URL"].rstrip("/")
    token = os.environ["GEOPULSE_READ_TOKEN"]

    async with aiohttp.ClientSession() as session:
        # --- Raw shapes -----------------------------------------------------
        sources = await raw_get(session, base, token, API_PATH_GPS_SOURCE)
        note(isinstance(sources, list), "/api/gps/source/ returns a raw list (no envelope)")
        await raw_get(session, base, token, API_PATH_GPS_LAST_KNOWN_POSITION)
        await raw_get(session, base, token, API_PATH_FRIENDS)

        types = sorted({s["type"] for s in sources}) if isinstance(sources, list) else []
        for source_type in types:
            page = await raw_get(
                session, base, token, API_PATH_GPS_POINTS,
                sourceTypes=source_type, limit=2, sortBy="timestamp", sortOrder="desc",
            )
            points = (page.get("data") or {}).get("data") if isinstance(page, dict) else None
            if not isinstance(points, list):
                note(False, f"points[{source_type}]: expected data.data list (page envelope)")
                continue
            note(len(points) <= 2, f"points[{source_type}]: limit honored ({len(points)} returned)")
            got = {p.get("sourceType") for p in points}
            note(got <= {source_type}, f"points[{source_type}]: sourceTypes filter honored ({got or 'no points'})")
            if len(points) == 2:
                note(points[0]["timestamp"] >= points[1]["timestamp"], f"points[{source_type}]: sorted newest first")

        # --- Our client's parsing ------------------------------------------
        client = GeoPulseClient(session, base, token)
        configs = await client.async_get_source_configs()
        user_ids = {c.user_id for c in configs}
        note(bool(configs) and None not in user_ids, f"every source config carries userId ({len(configs)} configs)")
        note(len(user_ids - {None}) <= 1, "source configs agree on a single userId")

        last = await client.async_get_last_known_position()
        if last:
            note(last.timestamp.tzinfo is not None, f"last-known position parses, tz-aware, {age(last.timestamp)} ({last.source_type})")
        else:
            note(False, "last-known position is null")

        for source_type in types:
            point = await client.async_get_latest_point_for_source_type(source_type)
            tag = " (excluded from import)" if source_type == SOURCE_TYPE_HOME_ASSISTANT else ""
            # An empty HOME_ASSISTANT source just means nothing exported yet.
            note(point is not None or bool(tag), f"latest {source_type}{tag}: " + (age(point.timestamp) if point else "no point"))

        friends = await client.async_get_friends()
        sharing = [f for f in friends if f.shares_live_location]
        friend_uids = {f.user_id for f in friends} - {None}
        note(True, f"friends parse: {len(friends)} total, {len(sharing)} sharing live location")
        if friends:
            note(friend_uids <= user_ids, "friends' userId matches the source configs' userId (token owner)")

    print("\n### Findings")
    print("\n".join(findings))


if __name__ == "__main__":
    asyncio.run(main())
