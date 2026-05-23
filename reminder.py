"""
Менеджер напоминаний о Zoom-встречах.

Логика:
  - Каждые ~60 секунд сканируем активные встречи (через ZOOM_META маркеры)
  - Для каждой встречи проверяем: близко ли время старта?
    • за 3 часа (≈ 10800 сек)
    • за 1 час (≈ 3600 сек)
    • за 10 минут (≈ 600 сек)
  - При первом попадании в окно — отправляем уведомление + помечаем в meta

Окна используются с допуском, чтобы не пропускать (если поллинг был задержан).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

from planfix_client import PlanfixClient
from telegram_client import TelegramClient
from time_utils import format_meeting_time

logger = logging.getLogger(__name__)


# Окна напоминаний: (название, секунд_до_встречи_от, секунд_до_встречи_до, ключ_в_meta)
REMINDER_WINDOWS = [
    # 3 часа: окно 3h ± 1.5min
    ("3h", 3 * 3600 - 90, 3 * 3600 + 90, "rem_3h"),
    # 1 час: окно 1h ± 1.5min
    ("1h", 1 * 3600 - 90, 1 * 3600 + 90, "rem_1h"),
    # 10 минут: окно 10min ± 1.5min
    ("10min", 10 * 60 - 90, 10 * 60 + 90, "rem_10m"),
]


class ReminderManager:
    """Шлёт напоминания участникам перед началом Zoom-встречи."""

    def __init__(
        self,
        pf: PlanfixClient,
        tg: Optional[TelegramClient],
        cfg,
    ):
        self.pf = pf
        self.tg = tg
        self.cfg = cfg

    async def check_reminders(self, tasks: list, extract_meta_fn, inject_meta_fn):
        """
        Проверить все Zoom-задачи и отправить напоминания при необходимости.
        Вызывается из основного tick() поллера.
        """
        now = datetime.now(pytz.UTC)
        sent = 0

        for task in tasks:
            meta = extract_meta_fn(task.get("description") or "")
            if not meta or not meta.get("meeting_id"):
                continue
            if meta.get("cancelled"):
                continue

            # Парсим время старта
            start_iso = meta.get("start_time")
            if not start_iso:
                continue

            try:
                # start_iso в формате "2025-12-31T10:00:00" в timezone бишкек
                tz = pytz.timezone(self.cfg.zoom_timezone)
                # Если ISO без timezone — считаем что в local timezone
                if "+" not in start_iso[10:] and "Z" not in start_iso:
                    start_dt = tz.localize(datetime.fromisoformat(start_iso))
                else:
                    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            except Exception as e:
                logger.warning(f"Не удалось распарсить start_time '{start_iso}': {e}")
                continue

            # Время до встречи в секундах
            seconds_to_meeting = (start_dt - now).total_seconds()
            if seconds_to_meeting < 0:
                continue  # Встреча уже идёт или закончилась

            # Проверяем каждое окно напоминания
            updated_meta = False
            for label, sec_from, sec_to, meta_key in REMINDER_WINDOWS:
                if not (sec_from <= seconds_to_meeting <= sec_to):
                    continue
                if meta.get(meta_key):
                    continue  # уже отправлено

                # Шлём напоминание
                await self._send_reminder(task, meta, label, seconds_to_meeting)
                meta[meta_key] = datetime.utcnow().isoformat()
                updated_meta = True
                sent += 1

            # Если что-то отправили — обновляем meta в задаче
            if updated_meta:
                new_desc = inject_meta_fn(task.get("description") or "", meta)
                try:
                    self.pf.update_task(task["id"], {"description": new_desc})
                except Exception as e:
                    logger.warning(f"Не удалось обновить meta task {task['id']}: {e}")

        if sent:
            logger.info(f"📬 Отправлено напоминаний: {sent}")

    async def _send_reminder(self, task: dict, meta: dict, label: str, seconds_left: float):
        """Отправить одно напоминание"""
        task_id = task["id"]
        topic = meta.get("topic", task.get("name", ""))
        join_url = meta.get("join_url", "")
        meeting_id = meta.get("meeting_id", "")
        password = meta.get("password", "")
        initiator = meta.get("initiator_name", "")
        pf_url = f"https://{self.cfg.planfix_account}.planfix.com/task/{task_id}"

        # Время в формате местного timezone
        time_str = format_meeting_time(meta.get("start_time"), self.cfg.zoom_timezone)

        # Заголовок зависит от окна
        labels = {
            "3h": ("🔔 Напоминание: встреча через 3 часа", "ЧЕРЕЗ 3 ЧАСА"),
            "1h": ("⏰ Скоро встреча: через 1 час", "ЧЕРЕЗ 1 ЧАС"),
            "10min": ("🚨 Через 10 минут начинается встреча!", "ЧЕРЕЗ 10 МИН"),
        }
        title, short = labels.get(label, ("Напоминание", "СКОРО"))

        # ─── Telegram уведомление ────────────────────────────────────────────
        if self.tg:
            tg_text = (
                f"{title}\n\n"
                f"📌 *Тема:* {topic}\n"
                f"🕐 *Начало:* {time_str}\n"
            )
            if initiator:
                tg_text += f"👤 *Инициатор:* {initiator}\n"
            tg_text += (
                f"\n🔗 *Ссылка:*\n{join_url}\n\n"
                f"🔑 Meeting ID: `{meeting_id}`\n"
                f"🔐 Пароль: `{password}`\n\n"
                f"📋 [Задача в PlanFix]({pf_url})"
            )
            self.tg.send_message_safe(tg_text, disable_web_page_preview=True)

        # ─── Комментарий в PlanFix (HTML формат — \n не работает) ─────────────
        try:
            comment = (
                f'<b>⏰ Напоминание — {short}</b><br><br>'
                f'📌 <b>Тема:</b> {topic}<br>'
                f'🕐 <b>Начало:</b> {time_str}<br><br>'
                f'🔗 <a href="{join_url}">{join_url}</a><br>'
                f'🔑 <b>Meeting ID:</b> {meeting_id}<br>'
                f'🔐 <b>Пароль:</b> {password}'
            )
            self.pf.add_comment(task_id, comment)
        except Exception as e:
            logger.warning(f"Не удалось добавить комментарий-напоминание: {e}")

        logger.info(f"📬 Напоминание {label} отправлено для task {task_id} ({topic})")
