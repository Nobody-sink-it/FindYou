from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINDYOU_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = ""
    sqlite_journal_mode: str = "WAL"
    api_token: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    owner_id: str = "local-owner"
    owner_name: str = "Owner"
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"
    llm_base_url: str = ""
    llm_api_key: SecretStr | None = None
    llm_timeout: float = 60.0
    llm_max_tokens: int = 2048
    bili_python: str = ""
    bili_home: Path | None = None
    bili_timeout: float = 45.0
    bili_version: str = "1.2.1"
    worker_poll_seconds: float = 1.0
    job_lease_seconds: int = 240
    max_import_chars: int = 200000
    agent_context_tokens: int = 16000
    agent_daily_model_calls: int = 20

    @field_validator("sqlite_journal_mode")
    @classmethod
    def journal(cls, value: str) -> str:
        if value.upper() not in {"WAL", "DELETE"}:
            raise ValueError("sqlite_journal_mode must be WAL or DELETE")
        return value.upper()

    @field_validator("llm_provider")
    @classmethod
    def provider(cls, value: str) -> str:
        if value not in {"deepseek", "glm", "fake"}:
            raise ValueError("llm_provider must be deepseek, glm or fake")
        return value

    @property
    def effective_database_url(self) -> str:
        return self.database_url or "sqlite:///" + (self.data_dir.resolve() / "findyou.db").as_posix()

    @property
    def effective_bili_home(self) -> Path:
        return self.bili_home or self.data_dir / "credentials" / "bilibili"
