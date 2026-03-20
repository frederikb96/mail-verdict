"""
Infrastructure Configuration for MailVerdict.

Loads ONLY infrastructure config from YAML files with environment variable overrides.
Application settings (AI, spam, sync, retry) are stored in the database.

Loading order: env vars > mail-verdict.yaml (XDG override) > config.yaml (defaults)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("/app/config/config.yaml")
OVERRIDE_CONFIG_PATH = Path.home() / ".config" / "mail-verdict" / "mail-verdict.yaml"

MCP_TRANSPORT = "streamable-http"


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
        database.url -> MAIL_VERDICT_DATABASE_URL
    """
    for key, value in config.items():
        env_key = f"{prefix}_{key}".upper()

        if isinstance(value, dict):
            _get_env_override(env_key, value)
        elif isinstance(value, list):
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


def _fix_db_url(url: str) -> str:
    """
    Ensure async driver prefix for SQLAlchemy.

    CNPG generates postgresql:// but the app needs postgresql+asyncpg://.

    Args:
        url: Database URL string
    """
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _load_config() -> dict[str, Any]:
    """
    Load and merge infrastructure configuration from files and environment.

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


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    host: str
    port: int
    log_level: str
    cors_origins: list[str]


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
class InfraConfig:
    """
    Infrastructure configuration (file-based, requires restart).

    Does NOT include application settings (AI, spam, sync, retry).
    Those are in the database via SettingsService.

    Usage:
        config = get_config()
        print(config.server.port)
        print(config.database.url)
    """

    server: ServerConfig
    database: DatabaseConfig
    qdrant: QdrantConfig


_config_instance: InfraConfig | None = None


def get_config() -> InfraConfig:
    """
    Get the global infrastructure configuration instance.

    Creates a new instance on first call, reuses on subsequent calls.

    Raises:
        ConfigError: If required config values are missing
    """
    global _config_instance
    if _config_instance is None:
        cfg = _ensure_config()

        server_cfg = _require(cfg, "server", "server")
        database_cfg = _require(cfg, "database", "database")
        qdrant_cfg = _require(cfg, "qdrant", "qdrant")

        cors_origins_raw = server_cfg.get("cors_origins", ["http://localhost:5173"])
        cors_origins = (
            [str(o) for o in cors_origins_raw]
            if isinstance(cors_origins_raw, list)
            else ["http://localhost:5173"]
        )

        db_url = _require(database_cfg, "url", "database.url")
        db_url = _fix_db_url(db_url) if db_url else db_url

        _config_instance = InfraConfig(
            server=ServerConfig(
                host=_require(server_cfg, "host", "server.host"),
                port=_require(server_cfg, "port", "server.port"),
                log_level=_require(server_cfg, "log_level", "server.log_level"),
                cors_origins=cors_origins,
            ),
            database=DatabaseConfig(
                url=db_url,
                pool_size=_require(database_cfg, "pool_size", "database.pool_size"),
                max_overflow=_require(database_cfg, "max_overflow", "database.max_overflow"),
            ),
            qdrant=QdrantConfig(
                host=_require(qdrant_cfg, "host", "qdrant.host"),
                port=_require(qdrant_cfg, "port", "qdrant.port"),
                collection_name=_require(qdrant_cfg, "collection_name", "qdrant.collection_name"),
            ),
        )
    return _config_instance


def reset_config() -> None:
    """Reset the global configuration. Useful for testing."""
    global _config_instance, _CONFIG
    _config_instance = None
    _CONFIG = {}
