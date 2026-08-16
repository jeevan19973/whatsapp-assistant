"""Webhook logic tests — no Meta, no network.

These cover the security-critical paths (signature, allowlist, dedupe) that are easy to
break silently later. Run: uv run pytest
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

os.environ.update(
    {
        "WA_APP_SECRET": "test-secret",
        "WA_VERIFY_TOKEN": "test-verify",
        "ALLOWED_WA_IDS": "447700900123",
        "WA_PHONE_NUMBER_ID": "000",
        "WA_ACCESS_TOKEN": "fake",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from scripts import spike_whatsapp as s  # noqa: E402

ME = "447700900123"


@pytest.fixture
def sent(monkeypatch):
    captured: list[tuple[str, str]] = []

    async def fake_send(to: str, text: str) -> None:
        captured.append((to, text))

    monkeypatch.setattr(s, "send_text", fake_send)
    s._seen_message_ids.clear()
    return captured


@pytest.fixture
def client():
    return TestClient(s.app)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def _post(client, payload: dict, signature: str | None = None):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": signature or _sign(body),
            "Content-Type": "application/json",
        },
    )


def _text_msg(text: str, sender: str = ME, mid: str = "wamid.1") -> dict:
    return {
        "entry": [
            {"changes": [{"value": {"messages": [
                {"id": mid, "from": sender, "type": "text", "text": {"body": text}}
            ]}}]}
        ]
    }


def test_handshake_returns_challenge(client):
    r = client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "test-verify", "hub.challenge": "abc123"},
    )
    assert r.status_code == 200
    assert r.text == "abc123"


def test_handshake_rejects_bad_token(client):
    r = client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    assert r.status_code == 403


def test_bad_signature_rejected(client, sent):
    r = _post(client, _text_msg("hi"), signature="sha256=deadbeef")
    assert r.status_code == 403
    assert sent == []


def test_valid_message_is_echoed(client, sent):
    r = _post(client, _text_msg("11am 90ml breast milk"))
    assert r.status_code == 200
    assert sent == [(ME, "echo: 11am 90ml breast milk")]


def test_duplicate_message_id_ignored(client, sent):
    """Meta retries on slow/failed responses. Without dedupe every retry double-logs."""
    _post(client, _text_msg("90ml", mid="wamid.dup"))
    _post(client, _text_msg("90ml", mid="wamid.dup"))
    assert len(sent) == 1


def test_sender_not_in_allowlist_ignored(client, sent):
    r = _post(client, _text_msg("hello", sender="19999999999", mid="wamid.2"))
    assert r.status_code == 200
    assert sent == []


def test_non_text_message_handled(client, sent):
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid.3", "from": ME, "type": "image"}
        ]}}]}]
    }
    r = _post(client, payload)
    assert r.status_code == 200
    assert len(sent) == 1


def test_status_only_payload_does_not_crash(client, sent):
    """Meta sends sent/delivered/read status webhooks constantly; they have no 'messages' key."""
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.9", "status": "read"}
    ]}}]}]}
    r = _post(client, payload)
    assert r.status_code == 200
    assert sent == []
