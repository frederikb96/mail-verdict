"""Tests for config loading, env overrides, singleton reset."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

import mail_verdict.config.loader as loader
from mail_verdict.config.loader import (
    ConfigError,
    MailVerdictConfig,
    _deep_merge,
    _get_env_override,
    _require,
    get_config,
    reset_config,
)
from tests.helpers.config_factory import make_config


class TestRequire:
    """Tests for _require helper."""

    def test_returns_value_when_present(self) -> None:
        """Existing key returns its value."""
        assert _require({"port": 8080}, "port", "server.port") == 8080

    def test_raises_on_missing_key(self) -> None:
        """Missing key raises ConfigError with path."""
        with pytest.raises(ConfigError, match="server.port"):
            _require({}, "port", "server.port")


class TestDeepMerge:
    """Tests for _deep_merge."""

    def test_shallow_override(self) -> None:
        """Top-level keys are overridden."""
        base: dict[str, Any] = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 3})
        assert base == {"a": 1, "b": 3}

    def test_nested_merge(self) -> None:
        """Nested dicts are merged recursively."""
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
    """Tests for environment variable overrides."""

    def test_string_override(self) -> None:
        """String values are overridden from env."""
        config: dict[str, Any] = {"host": "0.0.0.0"}
        with patch.dict(os.environ, {"TEST_HOST": "127.0.0.1"}):
            _get_env_override("TEST", config)
        assert config["host"] == "127.0.0.1"

    def test_int_override(self) -> None:
        """Integer values are cast from env string."""
        config: dict[str, Any] = {"port": 8080}
        with patch.dict(os.environ, {"TEST_PORT": "9090"}):
            _get_env_override("TEST", config)
        assert config["port"] == 9090

    def test_bool_override(self) -> None:
        """Boolean values are parsed from env string."""
        config: dict[str, Any] = {"enabled": False}
        with patch.dict(os.environ, {"TEST_ENABLED": "true"}):
            _get_env_override("TEST", config)
        assert config["enabled"] is True

    def test_float_override(self) -> None:
        """Float values are cast from env string."""
        config: dict[str, Any] = {"delay": 1.0}
        with patch.dict(os.environ, {"TEST_DELAY": "2.5"}):
            _get_env_override("TEST", config)
        assert config["delay"] == 2.5

    def test_nested_override(self) -> None:
        """Nested dict keys are prefixed correctly."""
        config: dict[str, Any] = {"server": {"port": 8080}}
        with patch.dict(os.environ, {"TEST_SERVER_PORT": "9090"}):
            _get_env_override("TEST", config)
        assert config["server"]["port"] == 9090

    def test_list_skipped(self) -> None:
        """List values cannot be overridden via env vars."""
        config: dict[str, Any] = {"items": [1, 2, 3]}
        with patch.dict(os.environ, {"TEST_ITEMS": "4,5,6"}):
            _get_env_override("TEST", config)
        assert config["items"] == [1, 2, 3]

    def test_invalid_int_ignored(self) -> None:
        """Invalid int env value leaves original unchanged."""
        config: dict[str, Any] = {"port": 8080}
        with patch.dict(os.environ, {"TEST_PORT": "not_a_number"}):
            _get_env_override("TEST", config)
        assert config["port"] == 8080


class TestGetConfig:
    """Tests for singleton config loading."""

    def test_loads_from_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loads from real config.yaml with test overrides."""
        cfg_dict = make_config()
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        config = get_config()
        assert isinstance(config, MailVerdictConfig)
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
        """Missing top-level section raises ConfigError."""
        cfg_dict = make_config()
        del cfg_dict["server"]
        monkeypatch.setattr(loader, "_CONFIG", cfg_dict)
        with pytest.raises(ConfigError, match="server"):
            get_config()


class TestRetryConfig:
    """Tests for RetryConfig.get_delay."""

    def test_exponential_backoff(self, test_config: MailVerdictConfig) -> None:
        """Delay increases exponentially."""
        retry = test_config.retry
        d0 = retry.get_delay(0)
        d1 = retry.get_delay(1)
        assert d1 > d0

    def test_capped_at_max(self, test_config: MailVerdictConfig) -> None:
        """Delay is capped at max_delay_seconds."""
        retry = test_config.retry
        d = retry.get_delay(100)
        assert d == retry.max_delay_seconds
