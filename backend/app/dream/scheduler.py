"""
APScheduler setup for Dream consolidation and other periodic tasks.

Schedule:
  - Dream consolidation: configurable cron (default 03:00 daily) AND
    idle-time trigger (fires DREAM_IDLE_MINUTES after last transcript)
  - Soul OU step: every 30 minutes
  - Memory heat decay: weekly
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.config import get_config

_scheduler: AsyncIOScheduler | None = None
_idle_check_task: asyncio.Task | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    return _scheduler


async def _run_dream_for_all_devices() -> None:
    """Run dream consolidation for all known devices."""
    from app.db import get_db
    from app.dream.consolidator import DreamConsolidator

    # Collect devices with pending work
    async with get_db() as conn:
        cursor = await conn.execute(
            """SELECT DISTINCT device_id FROM sessions WHERE consolidated=0
               UNION
               SELECT DISTINCT device_id FROM ambient_transcripts
               WHERE processed_for_memory=0"""
        )
        rows = await cursor.fetchall()

    for row in rows:
        device_id = row["device_id"]
        consolidator = DreamConsolidator(device_id)
        if await consolidator.should_run():
            logger.info(f"[Dream] Starting consolidation for device: {device_id}")
            await consolidator.run()


async def _idle_trigger_loop() -> None:
    """
    Poll loop: check if any device has been silent for DREAM_IDLE_MINUTES.
    Fires a Dream run for that device when idle threshold is crossed.
    Runs every 60 seconds.
    """
    from app.pipeline import _last_transcript_at
    from app.dream.consolidator import DreamConsolidator

    while True:
        await asyncio.sleep(60)
        cfg = get_config()
        idle_seconds = cfg.DREAM_IDLE_MINUTES * 60
        now = datetime.now(timezone.utc)

        for device_id, last_ts in list(_last_transcript_at.items()):
            elapsed = (now - last_ts).total_seconds()
            if elapsed >= idle_seconds:
                logger.info(
                    f"[Dream/idle] Device {device_id} idle for "
                    f"{elapsed:.0f}s (threshold={idle_seconds}s) → triggering"
                )
                # Remove from dict so we don't re-trigger until next transcript
                _last_transcript_at.pop(device_id, None)

                consolidator = DreamConsolidator(device_id)
                if await consolidator.should_run():
                    try:
                        await consolidator.run()
                    except Exception as exc:
                        logger.error(f"[Dream/idle] Failed for {device_id}: {exc}")


async def _soul_background_step() -> None:
    """Advance OU process for all active soul states."""
    from app.pipeline import _souls
    for soul in _souls.values():
        soul.ou_step(dt=0.5)


def setup_scheduler() -> AsyncIOScheduler:
    global _idle_check_task

    cfg = get_config()
    scheduler = get_scheduler()

    # ── Cron-based Dream ──────────────────────────────────────────
    cron_parts = cfg.DREAM_SCHEDULE_CRON.split()
    if len(cron_parts) == 5:
        minute, hour, day, month, day_of_week = cron_parts
        dream_trigger = CronTrigger(
            minute=minute, hour=hour,
            day=day, month=month, day_of_week=day_of_week,
        )
    else:
        dream_trigger = CronTrigger(hour=3, minute=0)

    scheduler.add_job(
        _run_dream_for_all_devices,
        trigger=dream_trigger,
        id="dream_consolidation",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── Soul OU step ──────────────────────────────────────────────
    scheduler.add_job(
        _soul_background_step,
        trigger="interval",
        minutes=30,
        id="soul_ou_step",
        replace_existing=True,
    )

    # ── Idle-time Dream trigger (asyncio task, not APScheduler) ──
    # Started separately after scheduler.start() since it needs the event loop
    return scheduler


def start_idle_trigger() -> None:
    """
    Launch the idle-detection coroutine as a background asyncio task.
    Must be called after the event loop is running (i.e. inside lifespan).
    """
    global _idle_check_task
    if _idle_check_task is None or _idle_check_task.done():
        _idle_check_task = asyncio.create_task(
            _idle_trigger_loop(), name="dream-idle-trigger"
        )
        logger.info(
            f"[Dream] Idle trigger started "
            f"(threshold={get_config().DREAM_IDLE_MINUTES} min)"
        )
