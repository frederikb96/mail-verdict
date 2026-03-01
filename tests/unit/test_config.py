"""
Unit tests for config loading, env var overrides, and validation errors.

Row 109 (o=10.41).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from mail_verdict.config import (
    ConfigError,
    MailVerdictConfig,
    get_config,
    reset_config,
)
from mail_verdict.config.loader import (
    _deep_merge,
    _get_env_override,
    _require,
)

pytestmark = pytest.mark.unit


class TestRequire:
    """Tests for _require helper."""

    def test_returns_value_when_present(self) -> None:
        """_require returns the value when key exists."""
        assert _require({"foo": 42}, "foo", "test.foo") == 42

    def test_raises_config_error_when_missing(self) -> None:
        """_require raises ConfigError with clear message for missing key."""
        with pytest.raises(ConfigError, match="Missing required config: 'server.host'"):
            _require({}, "host", "server.host")


class TestDeepMerge:
    """Tests for _deep_merge utility."""

    def test_flat_override(self) -> None:
        """Override values replace base values."""
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 99})
        assert base == {"a": 1, "b": 99}

    def test_nested_merge(self) -> None:
        """Nested dicts are merged recursively."""
        base = {"server": {"host": "0.0.0.0", "port": 8080}}
        _deep_merge(base, {"server": {"port": 9090}})
        assert base == {"server": {"host": "0.0.0.0", "port": 9090}}

    def test_new_keys_added(self) -> None:
        """New keys in override are added to base."""
        base = {"a": 1}
        _deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}


class TestEnvOverride:
    """Tests for environment variable overrides."""

    def test_string_override(self) -> None:
        """String config values are overridden from env."""
        config = {"host": "0.0.0.0"}
        os.environ["TEST_HOST"] = "127.0.0.1"
        try:
            _get_env_override("TEST", config)
            assert config["host"] == "127.0.0.1"
        finally:
            del os.environ["TEST_HOST"]

    def test_int_override(self) -> None:
        """Integer config values are parsed from env."""
        config = {"port": 8080}
        os.environ["TEST_PORT"] = "9090"
        try:
            _get_env_override("TEST", config)
            assert config["port"] == 9090
        finally:
            del os.environ["TEST_PORT"]

    def test_bool_override(self) -> None:
        """Boolean config values are parsed from env."""
        config = {"enabled": False}
        os.environ["TEST_ENABLED"] = "true"
        try:
            _get_env_override("TEST", config)
            assert config["enabled"] is True
        finally:
            del os.environ["TEST_ENABLED"]

    def test_float_override(self) -> None:
        """Float config values are parsed from env."""
        config = {"delay": 1.0}
        os.environ["TEST_DELAY"] = "2.5"
        try:
            _get_env_override("TEST", config)
            assert config["delay"] == 2.5
        finally:
            del os.environ["TEST_DELAY"]

    def test_invalid_int_ignored(self) -> None:
        """Invalid int env values leave original value unchanged."""
        config = {"port": 8080}
        os.environ["TEST_PORT"] = "not_a_number"
        try:
            _get_env_override("TEST", config)
            assert config["port"] == 8080
        finally:
            del os.environ["TEST_PORT"]

    def test_nested_env_override(self) -> None:
        """Nested config keys use underscore-joined env names."""
        config = {"server": {"port": 8080}}
        os.environ["TEST_SERVER_PORT"] = "9090"
        try:
            _get_env_override("TEST", config)
            assert config["server"]["port"] == 9090
        finally:
            del os.environ["TEST_SERVER_PORT"]

    def test_list_values_not_overridden(self) -> None:
        """List values cannot be overridden via env vars."""
        config = {"folders": ["INBOX"]}
        os.environ["TEST_FOLDERS"] = "Sent,Trash"
        try:
            _get_env_override("TEST", config)
            assert config["folders"] == ["INBOX"]
        finally:
            del os.environ["TEST_FOLDERS"]


class TestConfigLoading:
    """Tests for full config loading pipeline."""

    def test_loads_from_config_path_env(self, config_path: Path) -> None:
        """Config loads successfully from MAIL_VERDICT_CONFIG_PATH."""
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_path)
        reset_config()
        config = get_config()
        assert isinstance(config, MailVerdictConfig)
        assert config.server.port == 8080

    def test_missing_config_file_raises(self, tmp_path: Path) -> None:
        """Missing config file raises FileNotFoundError."""
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(tmp_path / "nonexistent.yaml")
        reset_config()
        with pytest.raises(FileNotFoundError):
            get_config()

    def test_missing_required_section_raises(self, tmp_path: Path) -> None:
        """Config missing required sections raises ConfigError."""
        minimal = tmp_path / "minimal.yaml"
        minimal.write_text(yaml.dump({"server": {"host": "localhost"}}))
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(minimal)
        reset_config()
        with pytest.raises(ConfigError, match="Missing required config"):
            get_config()

    def test_env_override_applies(self, config_path: Path) -> None:
        """Environment variables override config file values."""
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_path)
        os.environ["MAIL_VERDICT_SERVER_PORT"] = "9999"
        try:
            reset_config()
            config = get_config()
            assert config.server.port == 9999
        finally:
            del os.environ["MAIL_VERDICT_SERVER_PORT"]

    def test_config_singleton(self, config_path: Path) -> None:
        """get_config returns the same instance on repeated calls."""
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_path)
        reset_config()
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2

    def test_reset_clears_singleton(self, config_path: Path) -> None:
        """reset_config clears the cached instance."""
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_path)
        reset_config()
        c1 = get_config()
        reset_config()
        c2 = get_config()
        assert c1 is not c2

    def test_override_file_merges(self, config_path: Path, tmp_path: Path) -> None:
        """Override config file is merged on top of base config."""
        from mail_verdict.config import loader

        override = tmp_path / "mail-verdict.yaml"
        override.write_text(yaml.dump({"server": {"port": 7777}}))
        loader.OVERRIDE_CONFIG_PATH = override

        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_path)
        reset_config()
        config = get_config()
        assert config.server.port == 7777

    def test_accounts_parsing(self, tmp_path: Path) -> None:
        """Account config is correctly parsed from YAML."""
        cfg = yaml.safe_load(
            (Path(__file__).parent.parent.parent / "config" / "config.yaml").read_text()
        )
        cfg["accounts"] = [
            {
                "name": "personal",
                "host": "imap.example.com",
                "port": 993,
                "username": "user@example.com",
                "password": "secret",
                "folders": ["INBOX", "Archive"],
            }
        ]
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(cfg))
        os.environ["MAIL_VERDICT_CONFIG_PATH"] = str(config_file)
        reset_config()

        config = get_config()
        assert len(config.accounts) == 1
        assert config.accounts[0].name == "personal"
        assert config.accounts[0].folders == ["INBOX", "Archive"]

    def test_retry_config_delay_calculation(self) -> None:
        """RetryConfig.get_delay computes exponential backoff correctly."""
        from mail_verdict.config import RetryConfig

        retry = RetryConfig(
            max_retries=3,
            base_delay_seconds=1.0,
            max_delay_seconds=8.0,
            exponential_base=2.0,
        )
        assert retry.get_delay(0) == 1.0
        assert retry.get_delay(1) == 2.0
        assert retry.get_delay(2) == 4.0
        assert retry.get_delay(3) == 8.0
        assert retry.get_delay(10) == 8.0  # capped
