"""Settings: the only reader of the environment.

Every field is read from `TOOLFORGE_<FIELD>` (or a `.env` file) and must appear in `.env.example`; `tests/test_docs_drift.py`
fails otherwise. Secret fields (`*_api_key`, `*_token`) are `SecretStr` and are masked wherever settings are printed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from toolforge.errors import ConfigError

ENV_PREFIX = "TOOLFORGE_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage and sandbox
    home: Path = Path("~/.toolforge")
    minilocker_path: Path | None = None
    runtime_image: str = "python:3.12-slim"
    build_allow: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["pypi.org", "files.pythonhosted.org"]
    )
    max_package_kb: int = Field(256, gt=0)
    default_timeout_s: int = Field(20, gt=0)
    default_memory_mb: int = Field(256, gt=0)
    max_concurrent_verifications: int = Field(2, gt=0)

    # Behaviour
    auto_promote: bool = True
    runtime_decoys: bool = True
    expose_direct: bool = False
    approval_timeout_s: int = Field(900, gt=0)
    log_level: str = "INFO"

    # Language models (builder and challenger); unset means "no model configured"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    challenger_base_url: str | None = None
    challenger_model: str | None = None
    challenger_api_key: SecretStr | None = None

    # Network listeners; a token is required on any non-loopback host (enforced in P07 and P17)
    server_host: str = "127.0.0.1"
    server_port: int = Field(8765, ge=1, le=65535)
    server_token: SecretStr | None = None
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(8787, ge=1, le=65535)
    dashboard_token: SecretStr | None = None

    @field_validator("build_allow", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("home", "minilocker_path", mode="after")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator("log_level", mode="after")
    @classmethod
    def _level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return level

    # The challenger falls back to the builder's configuration (docs/guide/04-optional-resources.md).
    @property
    def effective_challenger_base_url(self) -> str | None:
        return self.challenger_base_url or self.llm_base_url

    @property
    def effective_challenger_model(self) -> str | None:
        return self.challenger_model or self.llm_model

    @property
    def effective_challenger_api_key(self) -> SecretStr | None:
        return self.challenger_api_key or self.llm_api_key

    def masked(self) -> dict[str, object]:
        """Effective settings for display. Secrets show `***` when set and `None` when not; values are never revealed."""
        out: dict[str, object] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[name] = "***"
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        return out


def load_settings(**overrides: object) -> Settings:
    """Load settings, turning pydantic's validation error into a ConfigError an operator can act on."""
    from pydantic import ValidationError as PydanticValidationError

    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{ENV_PREFIX}{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(
            f"invalid settings: {problems}", hint="fix the value in the environment or .env"
        ) from exc
