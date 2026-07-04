"""Tests for auto_trader's scan-only-by-default mode resolution."""
class TestAutoTraderScanOnlyDefault:
    """Execution is opt-in since 2026-07 (KDE loop measured -EV)."""

    def test_default_is_scan_only(self):
        from auto_trader import resolve_scan_only
        assert resolve_scan_only(False, False, None)
        assert resolve_scan_only(False, False, "")

    def test_execute_flag_enables(self):
        from auto_trader import resolve_scan_only
        assert not resolve_scan_only(True, False, None)

    def test_env_enables(self):
        from auto_trader import resolve_scan_only
        assert not resolve_scan_only(False, False, "true")
        assert not resolve_scan_only(False, False, "1")
        assert resolve_scan_only(False, False, "false")

    def test_explicit_scan_only_beats_env(self):
        from auto_trader import resolve_scan_only
        assert resolve_scan_only(False, True, "true")
