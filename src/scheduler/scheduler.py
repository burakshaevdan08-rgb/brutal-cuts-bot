"""Планировщик APScheduler (AsyncIOScheduler), работающий в одном event loop с ботом.

Задачи:

* ``reminders_poll`` — каждые ``SCHEDULER_POLL_SECONDS`` (по умолчанию 60) секунд
  ищет в БД записи, которым пора отправить напоминания (за 24 ч и за 2 ч)
  и запросы оценки (через 45 минут после визита);
* ``barber_digest`` — ежедневно в ``DIGEST_TIME`` (по умолчанию 09:30, часовой пояс клуба)
  рассылает мастерам расписание на день.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.core.config import Settings
from src.scheduler.tasks import process_due_reminders, process_review_requests, send_barber_digests
from src.services.crm import BaseCRMService

logger = logging.getLogger(__name__)

REMINDERS_JOB_ID = "reminders_poll"
DIGEST_JOB_ID = "barber_digest"


async def _run_reminders(bot: Bot, crm: BaseCRMService, settings: Settings) -> None:
    """Один проход опроса БД: напоминания и запросы оценки.

    Задачи изолированы: сбой одной не мешает другой и не останавливает следующие проходы.
    """
    try:
        await process_due_reminders(bot, crm, settings)
    except Exception:
        logger.exception("Сбой при обработке напоминаний")
    try:
        await process_review_requests(bot, crm, settings)
    except Exception:
        logger.exception("Сбой при отправке запросов оценки")


async def _run_digest(bot: Bot, crm: BaseCRMService, settings: Settings) -> None:
    try:
        await send_barber_digests(bot, crm, settings)
    except Exception:
        logger.exception("Сбой при рассылке дайджеста барберов")


def _on_job_event(event: JobEvent) -> None:
    if event.code == EVENT_JOB_MISSED:
        logger.warning("Задача %s пропущена (бот был занят или выключен)", event.job_id)
    elif event.code == EVENT_JOB_ERROR:
        logger.error("Задача %s завершилась с ошибкой", event.job_id)


def create_scheduler(bot: Bot, crm: BaseCRMService, settings: Settings) -> AsyncIOScheduler:
    """Создаёт и настраивает планировщик (запускается отдельно: ``scheduler.start()``)."""
    scheduler = AsyncIOScheduler(
        timezone=settings.TIMEZONE,
        job_defaults={
            "coalesce": True,        # пропущенные запуски схлопываются в один
            "max_instances": 1,      # параллельные проходы одной задачи не запускаются
            "misfire_grace_time": 300,
        },
    )
    kwargs = {"bot": bot, "crm": crm, "settings": settings}

    # Первый проход — сразу при старте: бот мог быть выключен, и напоминания накопились.
    scheduler.add_job(
        _run_reminders,
        trigger=IntervalTrigger(seconds=settings.SCHEDULER_POLL_SECONDS),
        id=REMINDERS_JOB_ID,
        name="Опрос БД: напоминания и запросы оценки",
        kwargs=kwargs,
        next_run_time=dt.datetime.now(settings.tzinfo),
        replace_existing=True,
    )

    hour, minute = settings.digest_hour_minute
    scheduler.add_job(
        _run_digest,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.TIMEZONE),
        id=DIGEST_JOB_ID,
        name="Утренний дайджест барберов",
        kwargs=kwargs,
        misfire_grace_time=3600,  # после короткой недоступности дайджест всё равно отправится
        replace_existing=True,
    )

    scheduler.add_listener(_on_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return scheduler
