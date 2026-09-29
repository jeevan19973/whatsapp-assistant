"""The webhook must fail closed: no secret or no allowlist means no bot."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.channels.whatsapp import WhatsAppCloudChannel
from app.config import Settings


def _settings(**overrides: str) -> Settings:
    values = {"wa_app_secret": "s3cret", "allowed_wa_ids": "447700900123"} | overrides
    return Settings(_env_file=None, **values)


def test_complete_settings_have_no_missing_security_values():
    assert _settings().missing_security() == []


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"wa_app_secret": ""}, ["WA_APP_SECRET"]),
        ({"allowed_wa_ids": ""}, ["ALLOWED_WA_IDS"]),
        ({"allowed_wa_ids": " , "}, ["ALLOWED_WA_IDS"]),
        ({"wa_app_secret": "", "allowed_wa_ids": ""}, ["WA_APP_SECRET", "ALLOWED_WA_IDS"]),
    ],
)
def test_missing_security_values_are_named(overrides: dict[str, str], missing: list[str]):
    assert _settings(**overrides).missing_security() == missing


def _channel(secret: str) -> WhatsAppCloudChannel:
    return WhatsAppCloudChannel(phone_number_id="1", access_token="t", app_secret=secret)


def test_signature_check_rejects_everything_without_a_secret():
    assert _channel("").verify_signature(b"{}", "sha256=anything") is False


def test_signature_check_accepts_a_valid_signature():
    body = b'{"entry": []}'
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    channel = _channel("s3cret")
    assert channel.verify_signature(body, f"sha256={sig}") is True
    assert channel.verify_signature(body, "sha256=" + "0" * 64) is False
