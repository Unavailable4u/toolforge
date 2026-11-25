1|"""Settings: the only reader of the environment.
2|
3|Every field is read from `TOOLFORGE_<FIELD>` (or a `.env` file) and must appear in `.env.example`; `tests/test_docs_drift.py`
4|fails otherwise. Secret fields (`*_api_key`, `*_token`) are `SecretStr` and are masked wherever settings are printed.
5|"""
6|
7|from __future__ import annotations
8|
9|from pathlib import Path
10|from typing import Annotated
11|
12|from pydantic import Field, SecretStr, field_validator
13|from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
14|
15|from toolforge.errors import ConfigError
16|
17|ENV_PREFIX = "TOOLFORGE_"
18|
19|
20|class Settings(BaseSettings):
21|    model_config = SettingsConfigDict(
22|        env_prefix=ENV_PREFIX,
23|        env_file=".env",
24|        env_file_encoding="utf-8",
25|        extra="ignore",
26|    )
27|
28|    # Storage and sandbox
29|    home: Path = Path("~/.toolforge")
30|    images_dir: Path = Path("~/.toolforge/images")
31|    minilocker_path: Path | None = None
32|    runtime_image: str = "toolforge-runtime:py312"
33|    build_allow: Annotated[list[str], NoDecode] = Field(
34|        default_factory=lambda: ["pypi.org", "files.pythonhosted.org"]
35|    )
36|    max_package_kb: int = Field(256, gt=0)
37|    default_timeout_s: int = Field(20, gt=0)
38|    default_memory_mb: int = Field(256, gt=0)
39|    max_concurrent_verifications: int = Field(2, gt=0)
40|
41|    # Behaviour
42|    auto_promote: bool = True
43|    runtime_decoys: bool = True
44|    expose_direct: bool = False
45|    approval_timeout_s: int = Field(900, gt=0)
46|    log_level: str = "INFO"
47|
48|    # Language models (builder and challenger); unset means "no model configured"
49|    llm_base_url: str | None = None
50|    llm_model: str | None = None
51|    llm_api_key: SecretStr | None = None
52|    challenger_base_url: str | None = None
53|    challenger_model: str | None = None
54|    challenger_api_key: SecretStr | None = None
55|    # Requests per minute the client allows itself (0 = no limit), and how long one request may take. The challenger's own limit
56|    # falls back to the builder's only while the challenger shares the builder's endpoint (P10, docs/guide/04-optional-resources.md).
57|    llm_max_rpm: int = Field(0, ge=0)
58|    challenger_max_rpm: int = Field(0, ge=0)
59|    llm_timeout_s: int = Field(60, ge=1, le=600)
60|    # The server-side builder (P11): rounds it may ask the model for a package, and the model tokens one build may spend in all.
61|    builder_max_rounds: int = Field(4, ge=1, le=10)
62|    builder_token_budget: int = Field(60000, ge=1000)
63|    # Repair (P12): how many times `forge_repair` may start on one tool in an hour, and whether the raw arguments of a failed call are kept
64|    # as regression cases (off by default: they are caller data; see docs/repair.md).
65|    repair_max_per_hour: int = Field(3, ge=1, le=100)
66|    store_failed_inputs: bool = False
67|    # Sessions (P13): a session tool keeps one sandbox alive across calls. It closes after `session_ttl_s` seconds without a call, or
68|    # `session_max_s` seconds after it opened whatever happens; at most `max_sessions` are open at once.
69|    session_ttl_s: int = Field(900, ge=1)
70|    session_max_s: int = Field(3600, ge=1)
71|    max_sessions: int = Field(4, ge=1, le=64)
72|
73|    # Network listeners; a token is required on any non-loopback host (enforced in P07 and P17)
74|    server_host: str = "127.0.0.1"
75|    server_port: int = Field(8765, ge=1, le=65535)
76|    server_token: SecretStr | None = None
77|    # Extra `Origin` and `Host` values the HTTP listener accepts beyond loopback and its own bound address (P07, shared with P17).
78|    server_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
79|    server_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
80|    dashboard_host: str = "127.0.0.1"
81|    dashboard_port: int = Field(8787, ge=1, le=65535)
82|    dashboard_token: SecretStr | None = None
83|
84|    @field_validator("build_allow", "server_allowed_origins", "server_allowed_hosts", mode="before")
85|    @classmethod
86|    def _split_hosts(cls, value: object) -> object:
87|        if isinstance(value, str):
88|            return [part.strip().lower() for part in value.split(",") if part.strip()]
89|        return value
90|
91|    @field_validator("home", "images_dir", "minilocker_path", mode="after")
92|    @classmethod
93|    def _expand(cls, value: Path | None) -> Path | None:
94|        if value is None:
95|            return None
96|        return value.expanduser()
97|
98|    @field_validator("log_level", mode="after")
99|    @classmethod
100|    def _level(cls, value: str) -> str:
101|        level = value.upper()
102|        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
103|            raise ValueError("must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
104|        return level
105|
106|    # The challenger falls back to the builder's configuration (docs/guide/04-optional-resources.md).
107|    @property
108|    def effective_challenger_base_url(self) -> str | None:
109|        return self.challenger_base_url or self.llm_base_url
110|
111|    @property
112|    def effective_challenger_model(self) -> str | None:
113|        return self.challenger_model or self.llm_model
114|
115|    @property
116|    def effective_challenger_api_key(self) -> SecretStr | None:
117|        return self.challenger_api_key or self.llm_api_key
118|
119|    @property
120|    def effective_challenger_max_rpm(self) -> int:
121|        """The challenger's own limit; when it has no endpoint of its own (it uses the builder's), the builder's limit applies."""
122|        if self.challenger_max_rpm:
123|            return self.challenger_max_rpm
124|        return self.llm_max_rpm if not self.challenger_base_url else 0
125|
126|    @property
127|    def challenger_shares_builder_model(self) -> bool:
128|        """True when the challenger would run the same model id as the builder, which makes G5 weaker evidence."""
129|        challenger = (self.effective_challenger_model or "").strip().lower()
130|        builder = (self.llm_model or "").strip().lower()
131|        return bool(challenger) and challenger == builder
132|
133|    def masked(self) -> dict[str, object]:
134|        """Effective settings for display. Secrets show `***` when set and `None` when not; values are never revealed."""
135|        out: dict[str, object] = {}
136|        for name in type(self).model_fields:
137|            value = getattr(self, name)
138|            if isinstance(value, SecretStr):
139|                out[name] = "***"
140|            elif isinstance(value, Path):
141|                out[name] = str(value)
142|            else:
143|                out[name] = value
144|        return out
145|
146|
147|def load_settings(**overrides: object) -> Settings:
148|    """Load settings, turning pydantic's validation error into a ConfigError an operator can act on."""
149|    from pydantic import ValidationError as PydanticValidationError
150|
151|    try:
152|        return Settings(**overrides)  # type: ignore[arg-type]
153|    except PydanticValidationError as exc:
154|        problems = "; ".join(
155|            f"{ENV_PREFIX}{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}"
156|            for err in exc.errors()
157|        )
158|        raise ConfigError(
159|            f"invalid settings: {problems}", hint="fix the value in the environment or .env"
160|        ) from exc