"""Channel seam — everything above this layer is transport-agnostic.

Adding Telegram later means implementing this Protocol, not touching the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A normalized inbound message, stripped of channel specifics."""

    id: str          # globally unique per channel; the dedupe key
    sender: str      # channel-native sender id
    kind: str        # "text" | "image" | "audio" | ...
    text: str        # empty unless kind == "text"


@runtime_checkable
class Channel(Protocol):
    name: str

    def verify_signature(self, body: bytes, header: str | None) -> bool:
        """Reject payloads that did not come from the platform."""
        ...

    def parse(self, payload: dict) -> list[InboundMessage]:
        """Extract messages from a webhook body. Non-message events yield []."""
        ...

    async def send_text(self, to: str, text: str) -> None: ...

    async def close(self) -> None: ...
