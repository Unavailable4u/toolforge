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
    runtime_image: str = "toolforge-runtime:py312"
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
    # Requests per minute the client allows itself (0 = no limit), and how long one request may take. The challenger's own limit
    # falls back to the builder's only while the challenger shares the builder's endpoint (P10, docs/guide/04-optional-resources.md).
    llm_max_rpm: int = Field(0, ge=0)
    challenger_max_rpm: int = Field(0, ge=0)
    llm_timeout_s: int = Field(60, ge=1, le=600)
    # The server-side builder (P11): rounds it may ask the model for a package, and the model tokens one build may spend in all.
    builder_max_rounds: int = Field(4, ge=1, le=10)
    builder_token_budget: int = Field(60000, ge=1000)
    # Repair (P12): how many times `forge_repair` may start on one tool in an hour, and whether the raw arguments of a failed call are kept
    # as regression cases (off by default: they are caller data; see docs/repair.md).
    repair_max_per_hour: int = Field(3, ge=1, le=100)
    store_failed_inputs: bool = False
    # Sessions (P13): a session tool keeps one sandbox alive across calls. It closes after `session_ttl_s` seconds without a call, or
    # `session_max_s` seconds after it opened whatever happens; at most `max_sessions` are open at once.
    session_ttl_s: int = Field(900, ge=1)
    session_max_s: int = Field(3600, ge=1)
    max_sessions: int = Field(4, ge=1, le=64)

    # Network listeners; a token is required on any non-loopback host (enforced in P07 and P17)
    server_host: str = "127.0.0.1"
    server_port: int = Field(8765, ge=1, le=65535)
    server_token: SecretStr | None = None
    # Extra `Origin` and `Host` values the HTTP listener accepts beyond loopback and its own bound address (P07, shared with P17).
    server_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    server_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(8787, ge=1, le=65535)
    dashboard_token: SecretStr | None = None

    @field_validator("build_allow", "server_allowed_origins", "server_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("home", "minilocker_path", mode="after")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser()

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

    @property
    def effective_challenger_max_rpm(self) -> int:
        """The challenger's own limit; when it has no endpoint of its own (it uses the builder's), the builder's limit applies."""
        if self.challenger_max_rpm:
            return self.challenger_max_rpm
        return self.llm_max_rpm if not self.challenger_base_url else 0

    @property
    def challenger_shares_builder_model(self) -> bool:
        """True when the challenger would run the same model id as the builder, which makes G5 weaker evidence."""
        challenger = (self.effective_challenger_model or "").strip().lower()
        builder = (self.llm_model or "").strip().lower()
        return bool(challenger) and challenger == builder

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