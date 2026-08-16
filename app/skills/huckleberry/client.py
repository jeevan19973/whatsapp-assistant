"""Long-lived Huckleberry session.

Authentication is email/password against Firebase and takes 1-3s, so it is done once and kept
warm — this is the main reason the plan picked an always-on host over scale-to-zero.

Also filters one known-benign log line from the library. `list_feed_intervals` raises a
ValidationError internally on a single malformed historical entry (PLAN.md §9) and logs it on
every read. It loses no data for recent windows, but left alone it trains you to ignore that
logger, which would hide a real failure later.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from huckleberry_api import HuckleberryAPI

log = logging.getLogger(__name__)

_KNOWN_BENIGN = "FirebaseBottleFeedIntervalData.amount"


class _DropKnownBenign(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "Error fetching feed intervals" in msg and _KNOWN_BENIGN in msg:
            return False  # known upstream bug, zero measured data loss
        return True


def install_log_filter() -> None:
    logging.getLogger("huckleberry_api.api").addFilter(_DropKnownBenign())
    log.info(
        "filtering one known-benign huckleberry_api log line "
        "(malformed historical bottle entry; see PLAN.md section 9)"
    )


class HuckleberryClient:
    def __init__(self, email: str, password: str, timezone: str, child_uid: str = "") -> None:
        self._email = email
        self._password = password
        self._timezone = timezone
        self._configured_child_uid = child_uid
        self._api: HuckleberryAPI | None = None
        self._session: aiohttp.ClientSession | None = None
        self._child_uid: str | None = child_uid or None
        self._lock = asyncio.Lock()

    async def api(self) -> HuckleberryAPI:
        async with self._lock:
            if self._api is None:
                if not self._email or not self._password:
                    raise RuntimeError("HUCKLEBERRY_EMAIL / HUCKLEBERRY_PASSWORD not configured")
                self._session = aiohttp.ClientSession()
                self._api = HuckleberryAPI(
                    email=self._email,
                    password=self._password,
                    timezone=self._timezone,
                    websession=self._session,
                )
                await self._api.authenticate()
                log.info("huckleberry authenticated")
            else:
                # Refreshes the token if it is close to expiry; cheap when it isn't.
                await self._api.ensure_session()
            return self._api

    async def child_uid(self) -> str:
        if self._child_uid:
            return self._child_uid
        api = await self.api()
        user = await api.get_user()
        if user is None or not user.childList:
            raise RuntimeError("no children found on the Huckleberry account")
        if len(user.childList) > 1:
            log.warning(
                "%d children on the account; defaulting to the first. "
                "Set HUCKLEBERRY_CHILD_UID to pin one.",
                len(user.childList),
            )
        self._child_uid = user.childList[0].cid
        log.info("resolved child_uid=%s", self._child_uid)
        return self._child_uid

    async def reset(self) -> None:
        """Force re-authentication on the next call."""
        async with self._lock:
            self._api = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            self._child_uid = self._configured_child_uid or None

    async def close(self) -> None:
        if self._api is not None:
            try:
                await self._api.stop_all_listeners()
            except Exception:
                log.debug("stop_all_listeners failed on shutdown", exc_info=True)
        if self._session and not self._session.closed:
            await self._session.close()
