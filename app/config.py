from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # Off: never call the LLM, even with a key set; the persona endpoint returns the rule-based persona.
    llm_enabled: bool = True
    llm_provider: Literal["openai", "anthropic"] = "openai"
    # Empty: the provider's low-cost default (gpt-4.1-mini / claude-haiku-5-5), enough for this structured extraction task.
    llm_model: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # Needed only for an API key that is not scoped to a workspace (sent as the anthropic-workspace-id header).
    anthropic_workspace_id: str | None = None
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'persona.db').as_posix()}"
    supplier_data_dir: Path = PROJECT_ROOT / "data"
    conversation_data_dir: Path = PROJECT_ROOT / "data" / "conversations"

    # Optional tokens. When unset, admin routes only accept direct local requests,
    # and Sarvam/persona routes refuse requests that arrive through a proxy/tunnel.
    admin_token: str | None = None
    api_token: str | None = None

    evidence_cache_path: Path = PROJECT_ROOT / "evidence_cache.json"

    default_history_limit: int = 10
    max_history_limit: int = 50
    ingest_batch_size: int = 5000

    @property
    def llm_api_key(self) -> str | None:
        return self.openai_api_key if self.llm_provider == "openai" else self.anthropic_api_key

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_enabled and self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
