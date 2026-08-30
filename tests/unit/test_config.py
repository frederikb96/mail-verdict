"""Tests for config loading: deep merge, env overrides, placeholders, fail-fast."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

import mail_verdict.config.loader as loader
from mail_verdict.config.loader import (
    ConfigError,
    InfraConfig,
    _apply_env_overrides,
    _deep_merge,
    _resolve_placeholders,
    get_config,
    reset_config,
)
from tests.helpers.config_factory import make_config


class TestDeepMerge:
    """Tests for _deep_merge."""

    def test_shallow_override(self) -> None:
        """Top-level keys are overridden."""
        base: dict[str, Any] = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 3})
        assert base == {"a": 1, "b": 3}

    def test_nested_merge(self) -> None:
        """Nested dicts are merged recursively, sibling keys survive."""
        base: dict[str, Any] = {"server": {"host": "0.0.0.0", "port": 8080}}
        _deep_merge(base, {"server": {"port": 9090}})
        assert base["server"]["host"] == "0.0.0.0"
        assert base["server"]["port"] == 9090

    def test_new_key_added(self) -> None:
        """Keys not in base are added."""
        base: dict[str, Any] = {"a": 1}
        _deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}


class TestEnvOverride:
    """Tests for MAIL_VERDICT_<SECTION>_<KEY> environment overrides."""

    def test_string_override(self) -> None:
        """String values are overridden from env."""
        config: dict[str, Any] = {"host": "0.0.0.0"}
        with patch.dict(os.environ, {"TEST_HOST": "127.0.0.1"}):
            _apply_env_overrides("TEST", config)
        assert config["host"] == "127.0.0.1"

    def test_int_override(self) -> None:
        """Integer values are cast from env string."""
        config: dict[str, Any] = {"port": 8080}
        with patch.dict(os.environ, {"TEST_PORT": "9090"}):
            _apply_env_overrides("TEST", config)
        assert config["port"] == 9090

    def test_bool_override(self) -> None:
        """Boolean values are parsed from env string."""
        config: dict[str, Any] = {"enabled": False}
        with patch.dict(os.environ, {"TEST_ENABLED": "true"}):
            _apply_env_overrides("TEST", config)
        assert config["enabled"] is True

    def test_arbitrary_depth_override(self) -> None:
        """Env override reaches keys nested more than one level deep."""
        config: dict[str, Any] = {"server": {"database": {"pool": {"size": 5}}}}
        with patch.dict(os.environ, {"TEST_SERVER_DATABASE_POOL_SIZE": "20"}):
            _apply_env_overrides("TEST", config)
        assert config["server"]["database"]["pool"]["size"] == 20

    def test_list_override_is_comma_split(self) -> None:
        """A list value (e.g. cors_origins) is overridden as a comma-split string."""
        config: dict[str, Any] = {"items": ["a", "b"]}
        with patch.dict(os.environ, {"TEST_ITEMS": "x, y, z"}):
            _apply_env_overrides("TEST", config)
        assert config["items"] == ["x", "y", "z"]

    def test_invalid_int_raises(self) -> None:
        """An unparseable int override raises rather than silently ignoring it."""
        config: dict[str, Any] = {"port": 8080}
        with (
            patch.dict(os.environ, {"TEST_PORT": "not_a_number"}),
            pytest.raises(ConfigError, match="TEST_PORT"),
        ):
            _apply_env_overrides("TEST", config)


class TestResolvePlaceholders:
    """Tests for ${VAR} placeholder resolution."""

    def test_resolves_from_environment(self) -> None:
        """A ${VAR} placeholder is substituted with the env value."""
        with patch.dict(os.environ, {"MY_SECRET": "hunter2"}):
            result = _resolve_placeholders({"password": "${MY_SECRET}"})
        assert result == {"password": "hunter2"}

    def test_unset_var_resolves_to_empty_string(self) -> None:
        """An unset env var resolves to '' rather than raising.

        security.encryption_key relies on exactly this: an unconfigured
        ENCRYPTION_KEY disables storing provider keys via the settings API,
        not a startup crash.
        """
        os.environ.pop("MV_TEST_DEFINITELY_UNSET", None)
        result = _resolve_placeholders({"encryption_key": "${MV_TEST_DEFINITELY_UNSET}"})
        assert result == {"encryption_key": ""}

    def test_recurses_into_nested_structures(self) -> None:
        """Placeholders are resolved inside nested dicts and lists."""
        with patch.dict(os.environ, {"HOST": "db.internal"}):
            result = _resolve_placeholders(
                {"database": {"url": "postgresql://${HOST}/mv"}, "list": ["${HOST}"]}
            )
        assert result == {
            "database": {"url": "postgresql://db.internal/mv"},
            "list": ["db.internal"],
        }


class TestGetConfig:
    """Tests for singleton config loading and validation."""

    def test_loads_from_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loads from real config.yaml with test overrides."""
        cfg_dict = make_config()
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        config = get_config()
        assert isinstance(config, InfraConfig)
        assert config.server.port == 18080

    def test_singleton_reuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Second call returns same instance."""
        cfg_dict = make_config()
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2

    def test_reset_clears_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """reset_config clears global state."""
        cfg_dict = make_config()
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        get_config()
        reset_config()
        assert loader._config_instance is None
        assert loader._CONFIG == {}

    def test_missing_required_section_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing top-level section raises ConfigError, not a default fallback."""
        cfg_dict = make_config()
        del cfg_dict["server"]
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        with pytest.raises(ConfigError):
            get_config()

    def test_empty_database_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty database.url fails fast rather than connecting to nothing."""
        cfg_dict = make_config(database={"url": "", "pool_size": 1, "max_overflow": 0})
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        with pytest.raises(ConfigError, match="database.url"):
            get_config()

    def test_empty_encryption_key_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty encryption_key is accepted -- stored provider keys are just unavailable."""
        cfg_dict = make_config(security={"encryption_key": ""})
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        config = get_config()
        assert config.security.encryption_key == ""
