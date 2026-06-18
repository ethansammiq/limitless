#!/usr/bin/env python3
"""
PREFLIGHT — Startup credential and environment validation.

Call preflight_check() at the start of any trading-critical module to catch
misconfiguration before real money is at risk. Validates:
  1. KALSHI_API_KEY_ID is set and non-empty
  2. KALSHI_PRIVATE_KEY_PATH points to a readable RSA key file
  3. DISCORD_WEBHOOK_URL is set (warning-only — not blocking)
  4. positions.json is writable
  5. .env file exists

Usage:
    from preflight import preflight_check
    preflight_check()  # Raises SystemExit on critical failure

    # Or non-fatal:
    ok, issues = preflight_check(fatal=False)
"""

import os
from pathlib import Path

from log_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent


def preflight_check(fatal: bool = True) -> tuple:
    """Validate all credentials and environment before trading.

    Args:
        fatal: If True (default), raises SystemExit on critical failure.
               If False, returns (ok: bool, issues: list[str]).

    Returns:
        (True, []) if all checks pass.
        (False, [list of issue strings]) if any check fails.
    """
    issues = []
    warnings = []

    # ── 1. .env file exists ──
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        issues.append(f".env file not found at {env_path}")

    # ── 2. Kalshi API key ──
    api_key = os.getenv("KALSHI_API_KEY_ID", "")
    if not api_key:
        issues.append("KALSHI_API_KEY_ID not set in environment")
    elif len(api_key) < 10:
        issues.append(f"KALSHI_API_KEY_ID looks too short ({len(api_key)} chars)")

    # ── 3. Kalshi private key file ──
    pk_path_str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not pk_path_str:
        issues.append("KALSHI_PRIVATE_KEY_PATH not set in environment")
    else:
        pk_path = Path(pk_path_str)
        if not pk_path.is_absolute():
            pk_path = PROJECT_ROOT / pk_path
        if not pk_path.exists():
            issues.append(f"Private key file not found: {pk_path}")
        elif not os.access(pk_path, os.R_OK):
            issues.append(f"Private key file not readable: {pk_path}")
        else:
            # Quick sanity check — RSA key files start with specific headers
            try:
                header = pk_path.read_text()[:50]
                if "PRIVATE KEY" not in header and "RSA" not in header:
                    warnings.append(f"Private key file may not be RSA format: {pk_path.name}")
            except Exception:
                warnings.append(f"Could not read private key header: {pk_path}")

    # ── 4. Discord webhook (warning only) ──
    webhook = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK") or ""
    if not webhook:
        warnings.append("DISCORD_WEBHOOK_URL not set — notifications will use JSONL fallback only")
    elif not webhook.startswith("https://discord.com/api/webhooks/"):
        warnings.append("DISCORD_WEBHOOK_URL doesn't look like a Discord webhook URL")

    # ── 5. Positions file writable ──
    positions_file = PROJECT_ROOT / "positions.json"
    positions_dir = positions_file.parent
    if not os.access(positions_dir, os.W_OK):
        issues.append(f"Cannot write to positions directory: {positions_dir}")
    elif positions_file.exists() and not os.access(positions_file, os.W_OK):
        issues.append("positions.json exists but is not writable")

    # ── Report ──
    if warnings:
        for w in warnings:
            logger.warning(f"PREFLIGHT WARNING: {w}")

    if issues:
        for issue in issues:
            logger.error(f"PREFLIGHT FAILED: {issue}")
        if fatal:
            print(f"\n  ✗ PREFLIGHT CHECK FAILED ({len(issues)} issue(s)):")
            for issue in issues:
                print(f"    • {issue}")
            for w in warnings:
                print(f"    ⚠ {w}")
            raise SystemExit(1)
        return False, issues + [f"WARNING: {w}" for w in warnings]

    logger.info("Preflight check passed (%d warnings)", len(warnings))
    return True, [f"WARNING: {w}" for w in warnings]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    ok, issues = preflight_check(fatal=False)
    print(f"\n  PREFLIGHT CHECK {'PASSED ✓' if ok else 'FAILED ✗'}")
    if issues:
        for i in issues:
            print(f"    • {i}")
    else:
        print("    All checks passed")
