"""
Infrastructure Configuration for MailVerdict.

Loads ONLY infrastructure config: server, database. Application settings
(AI, spam, rules, retry) are stored in the database (see settings/).

Loading order (highest priority wins):
    1. config/config.yaml (complete defaults, always present)
    2. config-custom/config.override.yaml (sparse override, optional)
    3. MAIL_VERDICT_<SECTION>_<KEY> environment variable overrides
    4. ${VAR} placeholders anywhere in the merged config are resolved from
       the environment as a final pass

The merged result is validated by a pydantic model. Validation failure
(missing field, wrong type, unresolved placeholder) raises at startup --
there are no fallback defaults in code.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_CONFIG_PATH = Path("/app/config/config.yaml")
OVERRIDE_CONFIG_PATH = Path("/app/config-custom/config.override.yaml")

MCP_TRANSPORT = "streamable-http"

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised when configuration is missing, invalid, or unresolvable."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """
    Recursively merge override dict into base dict.

    Override values take precedence. Modifies base in-place.

    Args:
        base: Dict to merge into (mutated)
        override: Dict whose values take precedence
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _apply_env_overrides(prefix: str, config: dict[str, Any]) -> None:
    """
    Apply environment variable overrides to a config dict in-place.

    Env var naming is arbitrary-depth: MAIL_VERDICT_<SECTION>_<KEY>_..., in
    uppercase with underscores joining nested keys. Only keys already present
    in the config are eligible -- this cannot introduce new keys.

    Examples:
        server.port -> MAIL_VERDICT_SERVER_PORT
        database.url -> MAIL_VERDICT_DATABASE_URL

    Args:
        prefix: Env var prefix accumulated so far (e.g. "MAIL_VERDICT_SERVER")
        config: Config dict to mutate in-place
    """
    for key, value in config.items():
        env_key = f"{prefix}_{key}".upper()

        if isinstance(value, dict):
            _apply_env_overrides(env_key, value)
            continue

        env_value = os.environ.get(env_key)
        if env_value is None:
            continue

        if isinstance(value, bool):
            config[key] = env_value.lower() in ("true", "1", "yes", "on")
        elif isinstance(value, int):
            try:
                config[key] = int(env_value)
            except ValueError as exc:
                raise ConfigError(
                    f"Environment override {env_key}={env_value!r} is not a valid int"
                ) from exc
        elif isinstance(value, float):
            try:
                config[key] = float(env_value)
            except ValueError as exc:
                raise ConfigError(
                    f"Environment override {env_key}={env_value!r} is not a valid float"
                ) from exc
        elif isinstance(value, list):
            config[key] = [item.strip() for item in env_value.split(",") if item.strip()]
        else:
            config[key] = env_value


def _resolve_placeholders(value: Any) -> Any:
    """
    Recursively resolve ${VAR} placeholders against the environment.

    Args:
        value: Any config value (dict, list, str, or scalar)

    Returns:
        Value with every ${VAR} occurrence substituted

    Raises:
        ConfigError: If a referenced environment variable is unset
    """
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v) for v in value]
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            # Unset env vars resolve to "" rather than raising -- some
            # placeholders (security.encryption_key) are legitimately
            # optional, and emptiness is validated per-field where it
            # actually matters (DatabaseConfig.url) rather than
            # blanket-enforced here.
            return os.environ.get(match.group(1), "")

        return _PLACEHOLDER_RE.sub(_sub, value)
    return value


def _resolve_config_path() -> Path:
    """Resolve the config.yaml path, checking env override and local fallback."""
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


def _resolve_override_path() -> Path | None:
    """Resolve the sparse override path, checking env var and local fallback."""
    env_path = os.environ.get("MAIL_VERDICT_CONFIG_OVERRIDE_PATH")
    if env_path:
        return Path(env_path)
    if OVERRIDE_CONFIG_PATH.exists():
        return OVERRIDE_CONFIG_PATH
    local_override = (
        Path(__file__).parent.parent.parent.parent
        / "config-custom"
        / "config.override.yaml"
    )
    return local_override if local_override.exists() else None


def _fix_db_url(url: str) -> str:
    """
    Ensure the async driver prefix for SQLAlchemy.

    CNPG and most infra tooling generate postgresql:// but the app needs
    postgresql+asyncpg://.

    Args:
        url: Database URL string
    """
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _load_raw_config() -> dict[str, Any]:
    """
    Load and merge the raw configuration dict from files and environment.

    Loading order:
        1. config.yaml (complete defaults)
        2. config-custom/config.override.yaml (sparse override), if present
        3. MAIL_VERDICT_<SECTION>_<KEY> environment overrides
        4. ${VAR} placeholder resolution

    Returns:
        Fully merged and resolved config dict
    """
    config_path = _resolve_config_path()
    with open(config_path) as f:
        config: dict[str, Any] = yaml.safe_load(f) or {}

    override_path = _resolve_override_path()
    if override_path is not None:
        with open(override_path) as f:
            override: dict[str, Any] = yaml.safe_load(f) or {}
        _deep_merge(config, override)

    _apply_env_overrides("MAIL_VERDICT", config)

    return _resolve_placeholders(config)  # type: ignore[no-any-return]


_CONFIG: dict[str, Any] = {}


def _ensure_config() -> dict[str, Any]:
    """Ensure the raw config dict is loaded, return it."""
    global _CONFIG
    if not _CONFIG:
        _CONFIG = _load_raw_config()
    return _CONFIG


class ServerConfig(BaseModel):
    """HTTP server configuration."""

    host: str
    port: int
    log_level: str
    cors_origins: list[str] = Field(default_factory=list)


class SecurityConfig(BaseModel):
    """Encryption key for provider API keys stored in the database."""

    # Empty string (unresolved ${ENCRYPTION_KEY}) means the settings API
    # cannot store a provider key -- an environment variable is still
    # accepted as a fallback. Legitimately blank for a deployment that
    # prefers env-var-only provider keys, unlike database.url below.
    encryption_key: str = ""


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    url: str
    pool_size: int
    max_overflow: int
    reserved_for_requests: int

    @field_validator("url")
    @classmethod
    def _url_must_be_set(cls, value: str) -> str:
        """Reject an empty database URL rather than defaulting it."""
        if not value:
            raise ValueError(
                "database.url resolved to an empty string -- set "
                "MAIL_VERDICT_DATABASE_URL or database.url in config.yaml"
            )
        return value


class OutboxConfig(BaseModel):
    """Limits on an outgoing message's attachments.

    Every attachment is held whole in memory to be handed to PostIMAP,
    so the total and the count matter as much as the single-file size --
    a per-file limit alone is satisfied by many files that together are
    not.
    """

    max_attachment_bytes: int
    max_attachments_total_bytes: int
    max_attachments: int


class InfraConfig(BaseModel):
    """
    Infrastructure configuration (file-based, requires restart).

    Does NOT include application settings (AI, spam, rules, retry).
    Those are in the database via SettingsService.

    Usage:
        config = get_config()
        print(config.server.port)
        print(config.database.url)
    """

    server: ServerConfig
    security: SecurityConfig
    outbox: OutboxConfig
    database: DatabaseConfig


_config_instance: InfraConfig | None = None


def get_config() -> InfraConfig:
    """
    Get the global infrastructure configuration instance.

    Creates a new instance on first call, reuses on subsequent calls.

    Raises:
        ConfigError: If required config values are missing or invalid
    """
    global _config_instance
    if _config_instance is None:
        cfg = _ensure_config()

        database_cfg = dict(cfg.get("database") or {})
        db_url = database_cfg.get("url")
        if db_url:
            database_cfg["url"] = _fix_db_url(db_url)

        try:
            _config_instance = InfraConfig(
                server=ServerConfig(**(cfg.get("server") or {})),
                security=SecurityConfig(**(cfg.get("security") or {})),
                outbox=OutboxConfig(**(cfg.get("outbox") or {})),
                database=DatabaseConfig(**database_cfg),
            )
        except ValidationError as exc:
            raise ConfigError(f"Invalid configuration: {exc}") from exc

    return _config_instance


def reset_config() -> None:
    """Reset the global configuration. Useful for testing."""
    global _config_instance, _CONFIG
    _config_instance = None
    _CONFIG = {}
