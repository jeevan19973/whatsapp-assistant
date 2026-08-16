"""Typed settings from environment / .env."""

from __future__ import annotations

from functools import cached_property
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- channel ---
    channel: str = "whatsapp"
    wa_phone_number_id: str = ""
    wa_access_token: str = ""
    wa_app_secret: str = ""
    wa_verify_token: str = ""
    allowed_wa_ids: str = ""

    # --- huckleberry ---
    huckleberry_email: str = ""
    huckleberry_password: str = ""
    huckleberry_child_uid: str = ""

    # --- llm (phase 3) ---
    llm_provider: str = "nvidia"
    nvidia_api_key: str = ""
    nvidia_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    # Nemotron models reason by default. For a deterministic parser we want that OFF
    # (faster, far fewer credits) — the provider sends `/no_think`. Flip to True only if
    # extraction accuracy on messy phrasings needs the chain-of-thought.
    nvidia_reasoning: bool = False
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    # --- general ---
    local_tz: str = "Europe/London"
    db_path: str = "./journal.db"

    @cached_property
    def allowed(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.allowed_wa_ids.split(",") if n.strip())

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.local_tz)


settings = Settings()
