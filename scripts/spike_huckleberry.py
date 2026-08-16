"""Phase 0, Spike A — prove the reverse-engineered Huckleberry client works.

Run:  uv run python scripts/spike_huckleberry.py            # read-only, safe
      uv run python scripts/spike_huckleberry.py --write     # logs ONE real bottle entry

Reads HUCKLEBERRY_EMAIL / HUCKLEBERRY_PASSWORD / LOCAL_TZ from .env

The --write step logs a real 90ml breast milk entry at 11:00 today (or yesterday if
11:00 hasn't happened yet), then reads the feed intervals back to confirm it landed.
If it lands, delete it in the Huckleberry app afterwards — the library has no delete.
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


def fail(msg: str) -> None:
    print(f"\n  FAIL: {msg}")
    sys.exit(1)


async def main(do_write: bool) -> None:
    load_dotenv()
    email = os.getenv("HUCKLEBERRY_EMAIL")
    password = os.getenv("HUCKLEBERRY_PASSWORD")
    tz_name = os.getenv("LOCAL_TZ", "Europe/London")

    if not email or not password:
        fail("set HUCKLEBERRY_EMAIL and HUCKLEBERRY_PASSWORD in .env (copy .env.example)")

    tz = ZoneInfo(tz_name)
    print(f"timezone: {tz_name}")
    print(f"account:  {email}")

    async with aiohttp.ClientSession() as websession:
        api = HuckleberryAPI(
            email=email,
            password=password,
            timezone=tz_name,
            websession=websession,
        )

        # --- 1. auth -------------------------------------------------------
        print("\n[1] authenticate() ...", end=" ", flush=True)
        try:
            await api.authenticate()
        except Exception as exc:
            fail(f"authentication failed: {type(exc).__name__}: {exc}")
        print("ok")

        # --- 2. discover children -----------------------------------------
        print("[2] get_user() ...", end=" ", flush=True)
        user_doc = await api.get_user()
        if user_doc is None:
            fail("get_user() returned None")
        print("ok")

        children = list(user_doc.childList or [])
        if not children:
            fail("no children on this account")

        print(f"\n    {len(children)} child(ren) found:")
        for i, child in enumerate(children):
            name = getattr(child, "name", None) or getattr(child, "firstName", None) or "(unnamed)"
            print(f"      [{i}] cid={child.cid}  name={name}")

        child_uid = children[0].cid
        print(f"\n    using child_uid = {child_uid}")

        # --- 3. read back recent feeds (proves read path + gives a baseline)
        now = datetime.now(tz)
        window_start = now - timedelta(days=2)
        print(f"\n[3] list_feed_intervals(last 48h) ...", end=" ", flush=True)
        try:
            feeds_before = await api.list_feed_intervals(
                child_uid, start_time=window_start, end_time=now
            )
        except Exception as exc:
            fail(f"read failed: {type(exc).__name__}: {exc}")
        print(f"ok — {len(feeds_before)} entries")
        for f in feeds_before[-5:]:
            print(f"      {f}")

        if not do_write:
            print("\n  READ PATH OK. Re-run with --write to test logging a real entry.")
            return

        # --- 4. write one real entry --------------------------------------
        target = now.replace(hour=11, minute=0, second=0, microsecond=0)
        if target > now:
            target -= timedelta(days=1)

        print(f"\n[4] log_bottle(90ml Breast Milk @ {target.isoformat()}) ...", end=" ", flush=True)
        try:
            await api.log_bottle(
                child_uid,
                start_time=target,
                amount=90,
                bottle_type="Breast Milk",
                units="ml",
            )
        except Exception as exc:
            fail(f"write failed: {type(exc).__name__}: {exc}")
        print("ok (no exception)")

        # --- 5. verify it actually landed ---------------------------------
        # log_bottle returns None, so a read-back is the only way to confirm.
        #
        # Do NOT verify by list length: list_feed_intervals() aborts on the first
        # ValidationError and returns a truncated list, so the count is unreliable.
        # Do NOT use feeds[-1]: the list is regular-docs-sorted-by-start followed by
        # multi-container entries in arbitrary order, so it is not globally sorted.
        # Instead, match on the exact (start, amount) we just wrote.
        await asyncio.sleep(2)
        print("[5] read back to verify ...", end=" ", flush=True)
        feeds_after = await api.list_feed_intervals(
            child_uid, start_time=window_start, end_time=datetime.now(tz)
        )
        print(f"ok — {len(feeds_after)} entries (was {len(feeds_before)})")

        target_ts = target.timestamp()
        match = next(
            (
                f
                for f in feeds_after
                if getattr(f, "mode", None) == "bottle"
                and abs(float(f.start) - target_ts) < 60
                and float(getattr(f, "amount", -1)) == 90
            ),
            None,
        )

        if match is not None:
            print("\n  SPIKE A PASSED — write confirmed by matching read-back.")
            print(f"     {match}")
            print("\n     Now delete it in the Huckleberry app (no delete method in the library).")
        else:
            print("\n  WRITE UNCONFIRMED — the entry we wrote was not found on read-back.")
            print("     This does NOT necessarily mean the write failed: if the read logged a")
            print("     'validation error', the list is truncated and our entry may be hidden.")
            print("     Run  uv run python scripts/diagnose_feeds.py  to see raw stored data.")

        # --- 6. independent verification via prefs.lastBottle --------------
        # log_bottle also updates feed/{child}.prefs.lastBottle. Reading that is immune
        # to the intervals-parsing bug, so it's a second, more reliable signal.
        print("\n[6] cross-check via prefs.lastBottle ...", end=" ", flush=True)
        try:
            fs = await api._get_firestore_client()  # noqa: SLF001
            snap = await fs.collection("feed").document(child_uid).get()
            last = ((snap.to_dict() or {}).get("prefs") or {}).get("lastBottle") or {}
            print("ok")
            print(f"     lastBottle = {last}")
            if last.get("start") and abs(float(last["start"]) - target_ts) < 60:
                print("     ^ matches what we just wrote — write DEFINITELY landed.")
        except Exception as exc:
            print(f"skipped ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="log a real test entry")
    args = parser.parse_args()
    asyncio.run(main(args.write))
