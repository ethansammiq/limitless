#!/usr/bin/env python3
"""
NOTIFICATIONS — Reliable Discord webhook delivery with retry and fallback.

Shared module used by every alerting job (cli_sniper, dead_bracket_sweeper,
live_watch, watchdog, weekly_digest, …). If Discord is unreachable, alerts
are saved to a local JSONL fallback file so no opportunity is silently lost.

Features:
  - Exponential backoff for connection errors (5 attempts, 2s→60s, ~2 min total)
  - Fast-fail on 4xx client errors (except 429, which honors retry_after)
  - Discord embed chunking (respects 10 embed / 6000 char limits)
  - JSONL fallback file when Discord is persistently down
  - Fallback replay: pending alerts (<48h old) resend on next successful delivery
  - Dry-run mode for testing
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from log_setup import get_logger

logger = get_logger(__name__)

__all__ = ["send_discord_alert", "send_discord_embeds"]

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
FALLBACK_FILE = PROJECT_ROOT / "alerts_fallback.jsonl"


def _get_discord_webhook() -> str:
    """Lazy lookup of Discord webhook URL.

    Reading at call time (not import time) ensures dotenv has been loaded
    by the calling module, fixing watchdog.py which imports notifications
    before loading .env.
    """
    return os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK") or ""


def _mention_fields() -> tuple[str, dict] | None:
    """(content, allowed_mentions) for actionable alerts, or None.

    Discord mobile only pushes reliably on @mentions (server channels
    default to "Only @mentions"), so embeds-only money alerts never reach
    a phone. DISCORD_MENTION_USER_IDS (comma-separated) opts users in;
    allowed_mentions is explicit so nothing else in the text can ping."""
    ids = [u.strip() for u in
           os.getenv("DISCORD_MENTION_USER_IDS", "").split(",") if u.strip()]
    if not ids:
        return None
    return " ".join(f"<@{u}>" for u in ids), {"parse": [], "users": ids}

# Retry config — backoff must outlast the transient DNS failures the Mac
# hits right after wake ("Cannot connect to host discord.com:443 nodename
# nor servname"), which can persist for a minute or more.
MAX_RETRIES = 5
BACKOFF_BASE = 2.0        # first retry delay (seconds)
BACKOFF_MULTIPLIER = 4.0
BACKOFF_CAP = 60.0        # max delay between attempts

# Fallback replay config
FALLBACK_MAX_AGE_HOURS = 48
REPLAY_MARKER = "⏪ delayed"


def _backoff_delay(attempt: int) -> float:
    """Delay after 0-indexed attempt N: 2s, 8s, 32s, then capped at 60s."""
    return min(BACKOFF_CAP, BACKOFF_BASE * BACKOFF_MULTIPLIER ** attempt)


async def _post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
) -> bool:
    """POST to Discord with exponential backoff. Returns True on success.

    Connection-class errors (DNS failures after wake, timeouts, resets) get
    the full backoff schedule (~2 min total). 4xx client errors fail fast —
    retrying a rejected payload can't succeed. 429 honors Discord's retry_after.
    """
    for attempt in range(MAX_RETRIES):
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 204):
                    return True
                elif resp.status == 429:
                    # Rate limited — use Discord's retry_after if available
                    try:
                        data = await resp.json()
                        wait = data.get("retry_after", _backoff_delay(attempt))
                    except Exception:
                        wait = _backoff_delay(attempt)
                    logger.warning(f"Discord rate limited, waiting {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(wait)
                elif 400 <= resp.status < 500:
                    body = await resp.text()
                    logger.error(f"Discord returned {resp.status} — not retrying: {body[:200]}")
                    return False
                else:
                    body = await resp.text()
                    logger.warning(f"Discord returned {resp.status}: {body[:200]} (attempt {attempt + 1}/{MAX_RETRIES})")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(_backoff_delay(attempt))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"Discord request failed: {e} (attempt {attempt + 1}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(_backoff_delay(attempt))

    return False


def _save_to_fallback(embeds: list[dict], context: str = ""):
    """Append alert to JSONL fallback file when Discord is unreachable."""
    try:
        record = {
            "timestamp": datetime.now(ET).isoformat(),
            "context": context,
            "embeds": embeds,
        }
        with open(FALLBACK_FILE, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        logger.warning(f"Discord unreachable — alert saved to {FALLBACK_FILE}")
    except Exception as e:
        logger.error(f"Failed to write fallback alert: {e}")


def _mark_replayed(embeds: list[dict]) -> list[dict]:
    """Return copies of embeds with titles prefixed so delayed delivery is visible."""
    marked = []
    for embed in embeds:
        copy = dict(embed)
        title = copy.get("title", "")
        if not title.startswith(REPLAY_MARKER):
            copy["title"] = f"{REPLAY_MARKER} {title}".strip()
        marked.append(copy)
    return marked


def _claim_fallback_records() -> list[dict]:
    """Atomically claim pending fallback records for replay.

    Renames the file before reading so concurrent senders can't replay the
    same entries twice (at-most-once delivery), then deletes the claimed
    copy. Records older than FALLBACK_MAX_AGE_HOURS and corrupt lines are
    dropped.
    """
    if not FALLBACK_FILE.exists():
        return []

    claimed_path = FALLBACK_FILE.with_suffix(".replaying")
    try:
        os.rename(FALLBACK_FILE, claimed_path)
    except FileNotFoundError:
        return []  # another process claimed it first
    except OSError as e:
        logger.error(f"Could not rotate fallback file for replay: {e}")
        return []

    now = datetime.now(ET)
    records: list[dict] = []
    try:
        for line in claimed_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                saved_at = datetime.fromisoformat(record["timestamp"])
                if saved_at.tzinfo is None:
                    saved_at = saved_at.replace(tzinfo=ET)
                age_hours = (now - saved_at).total_seconds() / 3600
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if age_hours <= FALLBACK_MAX_AGE_HOURS and record.get("embeds"):
                records.append(record)
    except OSError as e:
        logger.error(f"Could not read claimed fallback file: {e}")
    finally:
        claimed_path.unlink(missing_ok=True)
    return records


async def _replay_fallback(session: aiohttp.ClientSession, webhook_url: str):
    """Resend stranded fallback alerts once a delivery proved Discord reachable."""
    records = _claim_fallback_records()
    if not records:
        return

    pending: list[dict] = []
    for record in records:
        pending.extend(_mark_replayed(record["embeds"]))

    logger.info(f"Replaying {len(pending)} fallback embed(s) from {len(records)} saved alert(s)")
    failed: list[dict] = []
    for chunk in _chunk_embeds(pending):
        await asyncio.sleep(1)  # rate-limit spacing after the main send
        if await _post_with_retry(session, webhook_url, {"embeds": chunk}):
            logger.debug(f"Replayed {len(chunk)} fallback embed(s)")
        else:
            failed.extend(chunk)

    if failed:
        _save_to_fallback(failed, context="replay_failed")


def _chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Split embeds into Discord-safe chunks (max 10 embeds, ~5500 chars per message)."""
    chunks = []
    current_chunk = []
    current_chars = 0

    for embed in embeds:
        desc_len = len(embed.get("description", "")) + len(embed.get("title", ""))
        if current_chars + desc_len > 5500 or len(current_chunk) >= 9:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0
        current_chunk.append(embed)
        current_chars += desc_len

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


UTC = ZoneInfo("UTC")

# Ledger tags — a phone-glance must tell simulation from real money
# (2026-07-05: a PAPER position warning read exactly like a live loss).
# Explicit `ledger` wins; otherwise the sending context implies it.
LEDGER_TAGS = {"paper": "🧪 SIM", "live": "💰 REAL"}
_CONTEXT_LEDGER = {
    # Real-money surfaces: the live-account watcher, and the opportunity
    # alerts whose embedded take.py commands place real orders.
    "live_watch": "live",
    "cli_sniper": "live",
    "dead_bracket_sweeper": "live",
}


def tag_title(title: str, context: str = "", ledger: str | None = None) -> str:
    """Prefix the embed title with the ledger tag; idempotent, system
    alerts (watchdog/digest/audit) stay untagged."""
    tag = LEDGER_TAGS.get(ledger or _CONTEXT_LEDGER.get(context, ""))
    if not tag or title.startswith(tag):
        return title
    return f"{tag} · {title}"


async def send_discord_alert(
    title: str,
    description: str,
    color: int = 0xFF6600,
    context: str = "",
    ledger: str | None = None,
    mention: bool = False,
):
    """
    Send a single Discord embed alert with retry and fallback.

    `ledger` ("paper"|"live") tags the title so simulation noise can never
    be mistaken for real money; contexts with an unambiguous ledger are
    tagged automatically. `mention` pings DISCORD_MENTION_USER_IDS — reserve
    it for actionable money alerts, or phone pushes become noise.
    """
    embed = {
        "title": tag_title(title, context, ledger),
        "description": description,
        "color": color,
        # Discord expects UTC ISO 8601 timestamps for embed timestamps.
        # Using ET here caused Discord to display incorrect times.
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    await send_discord_embeds([embed], context=context, mention=mention)


async def send_discord_embeds(
    embeds: list[dict],
    dry_run: bool = False,
    context: str = "",
    mention: bool = False,
):
    """
    Send multiple Discord embeds with chunking, retry, and fallback.

    Falls back to JSONL file if Discord is persistently unreachable.
    """
    webhook_url = _get_discord_webhook()

    if not webhook_url:
        logger.warning("No DISCORD_WEBHOOK set — skipping notification")
        _save_to_fallback(embeds, context=context or "no_webhook")
        return

    if not embeds:
        return

    chunks = _chunk_embeds(embeds)

    if dry_run:
        print("\n[DRY RUN] Would send to Discord:")
        for i, chunk in enumerate(chunks):
            print(f"\n--- Message {i+1} ({len(chunk)} embeds) ---")
            for embed in chunk:
                print(f"  Title: {embed.get('title', 'N/A')}")
                print(f"  Desc:  {embed.get('description', '')[:200]}...")
        return

    failed_embeds = []
    any_success = False

    async with aiohttp.ClientSession() as session:
        for i, chunk in enumerate(chunks):
            payload = {"embeds": chunk}
            if mention and i == 0 and (mf := _mention_fields()):
                # First chunk only — a chunked alert must not ping N times.
                payload["content"], payload["allowed_mentions"] = mf
            success = await _post_with_retry(session, webhook_url, payload)

            if success:
                any_success = True
                logger.debug(f"Discord alert sent ({len(chunk)} embeds)")
            else:
                logger.error(f"Discord delivery failed after {MAX_RETRIES} attempts")
                failed_embeds.extend(chunk)

            # Rate limit spacing between chunk sends
            if len(chunks) > 1:
                await asyncio.sleep(1)

        # Discord proven reachable — flush alerts stranded by earlier outages
        if any_success:
            await _replay_fallback(session, webhook_url)

    # Save any failed embeds to fallback (after replay claim, so a failed
    # chunk isn't immediately re-sent within this same call)
    if failed_embeds:
        _save_to_fallback(failed_embeds, context=context or "retry_exhausted")
