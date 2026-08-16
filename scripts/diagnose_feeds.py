"""Phase 0 diagnostic — dump RAW Firestore feed docs, bypassing the library's strict models.

The library's list_feed_intervals() aborts on the first pydantic ValidationError and returns a
silently truncated list. This script reads the same data with no validation, so we can see
ground truth: what's actually stored, which entries the library chokes on, and why.

Run:  uv run python scripts/diagnose_feeds.py
      uv run python scripts/diagnose_feeds.py --days 3      # limit the report window
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from huckleberry_api import HuckleberryAPI
from huckleberry_api.firebase_types import FirebaseFeedIntervalData
from pydantic import TypeAdapter, ValidationError

ADAPTER = TypeAdapter(FirebaseFeedIntervalData)


def fmt(ts: float | None, tz: ZoneInfo) -> str:
    if ts is None:
        return "?"
    try:
        return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return f"invalid({ts})"


def describe(raw: dict) -> str:
    mode = raw.get("mode", "?")
    bits = [f"mode={mode}"]
    if mode == "bottle":
        bits.append(f"{raw.get('amount', 'MISSING')}{raw.get('units', '?')} {raw.get('bottleType', '?')}")
    elif mode == "breast":
        bits.append(f"side={raw.get('lastSide', 'MISSING')}")
    elif mode == "solids":
        bits.append(f"foods={len(raw.get('foods', []) or [])}")
    return "  ".join(bits)


def check(key: str, raw: dict) -> tuple[bool, str]:
    try:
        ADAPTER.validate_python(raw)
        return True, ""
    except ValidationError as exc:
        missing = sorted({
            str(e["loc"][-1])
            for e in exc.errors()
            if e["type"] == "missing"
        })
        bad = sorted({
            f"{e['loc'][-1]}={e.get('input')!r}"
            for e in exc.errors()
            if e["type"] == "literal_error"
        })
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if bad:
            parts.append("wrong " + ", ".join(bad))
        return False, "; ".join(parts) or "validation failed"


async def main(days: int) -> None:
    load_dotenv()
    email, password = os.getenv("HUCKLEBERRY_EMAIL"), os.getenv("HUCKLEBERRY_PASSWORD")
    tz_name = os.getenv("LOCAL_TZ", "Europe/London")
    if not email or not password:
        print("FAIL: set HUCKLEBERRY_EMAIL / HUCKLEBERRY_PASSWORD in .env")
        sys.exit(1)

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    window_start = (now - timedelta(days=days)).timestamp()

    async with aiohttp.ClientSession() as websession:
        api = HuckleberryAPI(email=email, password=password, timezone=tz_name, websession=websession)
        await api.authenticate()

        user_doc = await api.get_user()
        child_uid = os.getenv("HUCKLEBERRY_CHILD_UID") or user_doc.childList[0].cid
        print(f"child_uid = {child_uid}   tz = {tz_name}   now = {now:%Y-%m-%d %H:%M}\n")

        client = await api._get_firestore_client()  # noqa: SLF001 — no public raw accessor
        intervals = client.collection("feed").document(child_uid).collection("intervals")

        regular: list[tuple[float, str, dict, bool, str]] = []
        multi_entries: list[tuple[float, str, dict, bool, str]] = []
        n_docs = n_multi_docs = 0
        broken_all_time: list[tuple[str, str, str]] = []

        async for doc in intervals.stream():
            raw = doc.to_dict() or {}
            n_docs += 1

            if raw.get("multi"):
                n_multi_docs += 1
                data = raw.get("data") or {}
                print(f"[multi container] {doc.id}  entries={len(data)}  hasMoreRoom={raw.get('hasMoreRoom')}")
                for key, entry in data.items():
                    ok, why = check(key, entry)
                    if not ok:
                        broken_all_time.append((doc.id, key, why))
                    start = entry.get("start")
                    if start and float(start) >= window_start:
                        multi_entries.append((float(start), key, entry, ok, why))
            else:
                ok, why = check(doc.id, raw)
                if not ok:
                    broken_all_time.append(("(regular)", doc.id, why))
                start = raw.get("start")
                if start and float(start) >= window_start:
                    regular.append((float(start), doc.id, raw, ok, why))

        # ---- report -------------------------------------------------------
        print(f"\n{'=' * 78}")
        print(f"TOTAL docs in feed/intervals: {n_docs}   (multi containers: {n_multi_docs})")
        print(f"{'=' * 78}\n")

        rows = sorted(regular + multi_entries, key=lambda r: r[0])
        print(f"--- ALL feed entries in the last {days} day(s), TRUE chronological order "
              f"({len(rows)} found) ---\n")
        for start, key, raw, ok, why in rows:
            src = "multi " if (start, key, raw, ok, why) in multi_entries else "doc   "
            flag = "     " if ok else " BAD "
            print(f"{flag}{fmt(start, tz)}  {src} {describe(raw)}")
            if not ok:
                print(f"          ^ library CANNOT parse this: {why}")

        print(f"\n--- entries the library cannot parse, ANY date ({len(broken_all_time)}) ---")
        if not broken_all_time:
            print("    none — reads are currently safe")
        for container, key, why in broken_all_time:
            print(f"    {container} / {key}: {why}")

        # ---- what the library actually returns ----------------------------
        print(f"\n--- comparison: what list_feed_intervals() returns for the same window ---")
        lib = await api.list_feed_intervals(
            child_uid, start_time=now - timedelta(days=days), end_time=now
        )
        print(f"    raw scan found : {len(rows)} entries")
        print(f"    library returned: {len(lib)} entries")
        if len(lib) < len(rows):
            print(f"\n    >>> {len(rows) - len(lib)} ENTRIES SILENTLY LOST by the library <<<")
        elif len(lib) == len(rows):
            print("    match — no loss for this window")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=2)
    asyncio.run(main(p.parse_args().days))
