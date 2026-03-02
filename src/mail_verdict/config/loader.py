"""
Centralized Configuration for MailVerdict.

Loads configuration from YAML files with environment variable overrides.
Loading order: env vars > mail-verdict.yaml (XDG override) > config.yaml (defaults)

All values MUST be defined in config.yaml - no hardcoded defaults here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

DEFAULT_CONFIG_PATH = Path("/app/config/config.yaml")
OVERRIDE_CONFIG_PATH = Path.home() / ".config" / "mail-verdict" / "mail-verdict.yaml"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _require(cfg: dict[str, Any], key: str, path: str) -> Any:
    """
    Get a required config value, raising clear error if missing.

    Args:
        cfg: Config dict to read from
        key: Key to look up
        path: Full path for error message (e.g., "server.port")

    Raises:
        ConfigError: If key is missing
    """
    if key not in cfg:
        raise ConfigError(
            f"Missing required config: '{path}'. Ensure config.yaml contains this value."
        )
    return cfg[key]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """
    Recursively merge override dict into base dict.

    Override values take precedence. Modifies base in-place.
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _get_env_override(prefix: str, config: dict[str, Any]) -> None:
    """
    Apply environment variable overrides to config dict.

    Env var naming: MAIL_VERDICT_<SECTION>_<KEY> in uppercase with underscores.
    Examples:
        server.port -> MAIL_VERDICT_SERVER_PORT
        retry.max_retries -> MAIL_VERDICT_RETRY_MAX_RETRIES
        ai.model -> MAIL_VERDICT_AI_MODEL
    """
    for key, value in config.items():
        env_key = f"{prefix}_{key}".upper()

        if isinstance(value, dict):
            _get_env_override(env_key, value)
        elif isinstance(value, list):
            # Lists cannot be overridden via env vars
            continue
        else:
            env_value = os.environ.get(env_key)
            if env_value is not None:
                if isinstance(value, bool):
                    config[key] = env_value.lower() in ("true", "1", "yes", "on")
                elif isinstance(value, int):
                    try:
                        config[key] = int(env_value)
                    except ValueError:
                        pass
                elif isinstance(value, float):
                    try:
                        config[key] = float(env_value)
                    except ValueError:
                        pass
                else:
                    config[key] = env_value


def _resolve_config_path() -> Path:
    """Resolve the default config path, checking env override and local fallback."""
    env_path = os.environ.get("MAIL_VERDICT_CONFIG_PATH")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return path
        raise FileNotFoundError(f"Config not found at MAIL_VERDICT_CONFIG_PATH={env_path}")

    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH

    local_config = Path(__file__).parent.parent.parent.parent / "config" / "config.yaml"
    if local_config.exists():
        return local_config

    raise FileNotFoundError(f"Config not found at {DEFAULT_CONFIG_PATH} or {local_config}")


def _load_config() -> dict[str, Any]:
    """
    Load and merge configuration from files and environment.

    Loading order:
        1. Load default config (config.yaml)
        2. Merge override config (mail-verdict.yaml) if exists
        3. Apply environment variable overrides
    """
    config_path = _resolve_config_path()
    with open(config_path) as f:
        config: dict[str, Any] = yaml.safe_load(f) or {}

    if OVERRIDE_CONFIG_PATH.is_file():
        with open(OVERRIDE_CONFIG_PATH) as f:
            override: dict[str, Any] = yaml.safe_load(f) or {}
        _deep_merge(config, override)

    _get_env_override("MAIL_VERDICT", config)

    return config


_CONFIG: dict[str, Any] = {}


def _ensure_config() -> dict[str, Any]:
    """Ensure config is loaded, return the config dict."""
    global _CONFIG
    if not _CONFIG:
        _CONFIG = _load_config()
    return _CONFIG


TransportType = Literal["stdio", "http", "sse", "streamable-http"]


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    host: str
    port: int
    log_level: str
    cors_origins: list[str]


@dataclass
class AccountConfig:
    """IMAP/SMTP account configuration."""

    name: str
    host: str
    port: int
    username: str
    password: str
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    idle_folders: list[str] = field(default_factory=lambda: ["INBOX"])
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: str | None = None
    ssl_verify: bool = True


@dataclass
class DatabaseConfig:
    """Database connection configuration."""

    url: str
    pool_size: int
    max_overflow: int


@dataclass
class QdrantConfig:
    """Qdrant vector store configuration."""

    host: str
    port: int
    collection_name: str


@dataclass
class AIConfig:
    """AI provider configuration."""

    provider: str
    model: str
    embedding_model: str
    embedding_dimensions: int


@dataclass
class SpamConfig:
    """Spam detection configuration."""

    enabled: bool
    excerpt_length: int
    neighbor_count: int
    auto_mark_read: bool
    system_prompt: str


@dataclass
class SyncConfig:
    """Email sync configuration."""

    poll_interval_seconds: int
    idle_enabled: bool
    idle_restart_seconds: int
    lookback_days: int
    auto_detect_folders: bool


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    exponential_base: float

    def get_delay(self, attempt: int) -> float:
        """
        Calculate exponential backoff delay for a retry attempt.

        Args:
            attempt: Zero-indexed attempt number (0 = first retry)
        """
        delay = self.base_delay_seconds * (self.exponential_base**attempt)
        return min(delay, self.max_delay_seconds)


@dataclass
class MCPConfig:
    """MCP server configuration."""

    enabled: bool
    port: int
    transport: TransportType


@dataclass
class MailVerdictConfig:
    """
    Main configuration container.

    Usage:
        config = get_config()
        print(config.server.port)
        print(config.database.url)
    """

    server: ServerConfig
    accounts: list[AccountConfig]
    database: DatabaseConfig
    qdrant: QdrantConfig
    ai: AIConfig
    spam: SpamConfig
    sync: SyncConfig
    retry: RetryConfig
    mcp: MCPConfig
    rules: list[dict[str, Any]]


_config_instance: MailVerdictConfig | None = None


def _parse_account(raw: dict[str, Any], index: int) -> AccountConfig:
    """
    Parse a single account config dict into AccountConfig.

    Args:
        raw: Raw account config dict
        index: Account index for error messages
    """
    prefix = f"accounts[{index}]"
    folders_raw = raw.get("folders", ["INBOX"])
    folders = [str(f) for f in folders_raw] if isinstance(folders_raw, list) else ["INBOX"]
    idle_raw = raw.get("idle_folders", ["INBOX"])
    idle_folders = [str(f) for f in idle_raw] if isinstance(idle_raw, list) else ["INBOX"]
    return AccountConfig(
        name=_require(raw, "name", f"{prefix}.name"),
        host=_require(raw, "host", f"{prefix}.host"),
        port=raw.get("port", 993),
        username=_require(raw, "username", f"{prefix}.username"),
        password=_require(raw, "password", f"{prefix}.password"),
        folders=folders,
        idle_folders=idle_folders,
        smtp_host=raw.get("smtp_host"),
        smtp_port=raw.get("smtp_port", 465),
        smtp_user=raw.get("smtp_user"),
        smtp_password=raw.get("smtp_password"),
        ssl_verify=raw.get("ssl_verify", True),
    )


def get_config() -> MailVerdictConfig:
    """
    Get the global configuration instance.

    Creates a new instance on first call, reuses on subsequent calls.
    All config values must be present in config.yaml - no hardcoded defaults.

    Raises:
        ConfigError: If required config values are missing
    """
    global _config_instance
    if _config_instance is None:
        cfg = _ensure_config()

        server_cfg = _require(cfg, "server", "server")
        accounts_raw = cfg.get("accounts", [])
        database_cfg = _require(cfg, "database", "database")
        qdrant_cfg = _require(cfg, "qdrant", "qdrant")
        ai_cfg = _require(cfg, "ai", "ai")
        spam_cfg = _require(cfg, "spam", "spam")
        sync_cfg = _require(cfg, "sync", "sync")
        retry_cfg = _require(cfg, "retry", "retry")
        mcp_cfg = _require(cfg, "mcp", "mcp")
        rules_raw = cfg.get("rules", [])

        accounts = (
            [_parse_account(acc, i) for i, acc in enumerate(accounts_raw)] if accounts_raw else []
        )

        cors_origins_raw = server_cfg.get("cors_origins", ["http://localhost:5173"])
        cors_origins = (
            [str(o) for o in cors_origins_raw]
            if isinstance(cors_origins_raw, list)
            else ["http://localhost:5173"]
        )

        _config_instance = MailVerdictConfig(
            server=ServerConfig(
                host=_require(server_cfg, "host", "server.host"),
                port=_require(server_cfg, "port", "server.port"),
                log_level=_require(server_cfg, "log_level", "server.log_level"),
                cors_origins=cors_origins,
            ),
            accounts=accounts,
            database=DatabaseConfig(
                url=_require(database_cfg, "url", "database.url"),
                pool_size=_require(database_cfg, "pool_size", "database.pool_size"),
                max_overflow=_require(database_cfg, "max_overflow", "database.max_overflow"),
            ),
            qdrant=QdrantConfig(
                host=_require(qdrant_cfg, "host", "qdrant.host"),
                port=_require(qdrant_cfg, "port", "qdrant.port"),
                collection_name=_require(qdrant_cfg, "collection_name", "qdrant.collection_name"),
            ),
            ai=AIConfig(
                provider=_require(ai_cfg, "provider", "ai.provider"),
                model=_require(ai_cfg, "model", "ai.model"),
                embedding_model=_require(ai_cfg, "embedding_model", "ai.embedding_model"),
                embedding_dimensions=_require(
                    ai_cfg, "embedding_dimensions", "ai.embedding_dimensions"
                ),
            ),
            spam=SpamConfig(
                enabled=_require(spam_cfg, "enabled", "spam.enabled"),
                excerpt_length=_require(spam_cfg, "excerpt_length", "spam.excerpt_length"),
                neighbor_count=_require(spam_cfg, "neighbor_count", "spam.neighbor_count"),
                auto_mark_read=_require(spam_cfg, "auto_mark_read", "spam.auto_mark_read"),
                system_prompt=_require(spam_cfg, "system_prompt", "spam.system_prompt"),
            ),
            sync=SyncConfig(
                poll_interval_seconds=_require(
                    sync_cfg, "poll_interval_seconds", "sync.poll_interval_seconds"
                ),
                idle_enabled=_require(sync_cfg, "idle_enabled", "sync.idle_enabled"),
                idle_restart_seconds=_require(
                    sync_cfg, "idle_restart_seconds", "sync.idle_restart_seconds"
                ),
                lookback_days=_require(sync_cfg, "lookback_days", "sync.lookback_days"),
                auto_detect_folders=_require(
                    sync_cfg, "auto_detect_folders", "sync.auto_detect_folders"
                ),
            ),
            retry=RetryConfig(
                max_retries=_require(retry_cfg, "max_retries", "retry.max_retries"),
                base_delay_seconds=_require(
                    retry_cfg, "base_delay_seconds", "retry.base_delay_seconds"
                ),
                max_delay_seconds=_require(
                    retry_cfg, "max_delay_seconds", "retry.max_delay_seconds"
                ),
                exponential_base=_require(retry_cfg, "exponential_base", "retry.exponential_base"),
            ),
            mcp=MCPConfig(
                enabled=_require(mcp_cfg, "enabled", "mcp.enabled"),
                port=_require(mcp_cfg, "port", "mcp.port"),
                transport=_require(mcp_cfg, "transport", "mcp.transport"),
            ),
            rules=rules_raw if isinstance(rules_raw, list) else [],
        )
    return _config_instance


def reset_config() -> None:
    """Reset the global configuration. Useful for testing."""
    global _config_instance, _CONFIG
    _config_instance = None
    _CONFIG = {}
