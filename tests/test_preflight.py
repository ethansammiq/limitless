#!/usr/bin/env python3
"""Tests for preflight.py — startup credential validation."""


import pytest


@pytest.fixture
def temp_project(tmp_path):
    """Create a temp project directory with .env and key file."""
    env_file = tmp_path / ".env"
    env_file.write_text("KALSHI_API_KEY_ID=test_key_12345\nKALSHI_PRIVATE_KEY_PATH=test_key.pem\n")

    key_file = tmp_path / "test_key.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake_key_content\n-----END RSA PRIVATE KEY-----\n")

    positions_file = tmp_path / "positions.json"
    positions_file.write_text("[]")

    return tmp_path


class TestPreflightCheck:
    """preflight_check() validation tests."""

    def test_all_checks_pass(self, temp_project, monkeypatch):
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "test_key_12345678")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(temp_project / "test_key.pem"))
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is True
        assert not any(i for i in issues if not i.startswith("WARNING:"))

    def test_missing_env_file(self, temp_project, monkeypatch):
        import preflight
        # Point to dir without .env
        empty_dir = temp_project / "subdir"
        empty_dir.mkdir()
        monkeypatch.setattr(preflight, "PROJECT_ROOT", empty_dir)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "test_key_12345678")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(temp_project / "test_key.pem"))

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is False
        assert any(".env" in i for i in issues)

    def test_missing_api_key(self, temp_project, monkeypatch):
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(temp_project / "test_key.pem"))

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is False
        assert any("KALSHI_API_KEY_ID" in i for i in issues)

    def test_short_api_key(self, temp_project, monkeypatch):
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "short")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(temp_project / "test_key.pem"))

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is False
        assert any("too short" in i for i in issues)

    def test_missing_private_key_path(self, temp_project, monkeypatch):
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "test_key_12345678")
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is False
        assert any("KALSHI_PRIVATE_KEY_PATH" in i for i in issues)

    def test_private_key_file_not_found(self, temp_project, monkeypatch):
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "test_key_12345678")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/nonexistent/path/key.pem")

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is False
        assert any("not found" in i for i in issues)

    def test_missing_discord_webhook_warning_only(self, temp_project, monkeypatch):
        """Missing Discord webhook is a warning, not a blocker."""
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.setenv("KALSHI_API_KEY_ID", "test_key_12345678")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(temp_project / "test_key.pem"))
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)

        ok, issues = preflight.preflight_check(fatal=False)
        assert ok is True  # Should still pass — webhook is warning-only
        assert any("DISCORD_WEBHOOK" in i for i in issues)

    def test_fatal_mode_exits(self, temp_project, monkeypatch):
        """fatal=True raises SystemExit on critical failure."""
        import preflight
        monkeypatch.setattr(preflight, "PROJECT_ROOT", temp_project)
        monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(temp_project / "test_key.pem"))

        with pytest.raises(SystemExit):
            preflight.preflight_check(fatal=True)
