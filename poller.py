"""
Поллер PlanFix — умное обнаружение Zoom-задач БЕЗ настройки UI/автоматизаций.

Принцип работы:
  1. Каждую N секунд опрашивает PlanFix через REST API
  2. Ищет задачи с признаками Zoom-конференции:
     - Название содержит маркер: "ZOOM:", "ЗУМ:", "[ZOOM]", "#zoom"
     - Или статус совпадает с PLANFIX_ZOOM_STATUS_ID (если задано в env)
     - Или принадлежит проекту PLANFIX_ZOOM_PROJECT_ID
  3. Для каждой найденной задачи проверяет состояние через скрытый маркер
     в описании задачи и предпринимает действия:
       - Нет маркера → создать Zoom-встречу
       - Название/время изменились → обновить Zoom-встречу
       - Появился тег #cancel или [ОТМЕНА] → удалить встречу
       - Список участников изменился → синхронизировать RSVP-подзадачи

Маркер в описании задачи (невидимый для пользователя):
  <!-- ZOOM_META: {"meeting_id": "12345", "uuid": "xxx", "join_url": "...",
                   "password": "...", "topic": "...", "start_time": "..."} -->
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from planfix_client import PlanfixClient, PlanfixAPIError
from zoom_client import ZoomClient, ZoomAPIError
from telegram_client import TelegramClient
from time_utils import to_zoom_iso, calc_duration_minutes, format_meeting_time

logger = logging.getLogger(__name__)

# ─── Маркеры распознавания ────────────────────────────────────────────────────

ZOOM_TRIGGERS = [
    r"^\s*ZOOM\s*[:\-—]",
    r"^\s*ЗУМ\s*[:\-—]",
    r"\[ZOOM\]",
    r"\[ЗУМ\]",
    r"#zoom\b",
    r"#зум\b",
    r"конференция\s+zoom",
    r"конференция\s+зум",
]

CANCEL_MARKERS = [
    r"\[ОТМЕНА\]",
    r"\[ОТМЕНЕНА\]",
    r"\[CANCELLED\]",
    r"#cancel\b",
    r"#отмена\b",
]

META_BEGIN = "<!-- ZOOM_META:"
META_END = "-->"
META_PATTERN = re.compile(r"<!--\s*ZOOM_META:\s*(\{[^}]+\})\s*-->", re.DOTALL)
RSVP_MARKER = "[RSVP]"


def is_zoom_task(task: dict, zoom_status_id: int = 0, zoom_project_id: int = 0) -> bool:
    """Проверка, является ли задача Zoom-конференцией"""
    name = task.get("name", "") or ""
    for pat in ZOOM_TRIGGERS:
        if re.search(pat, name, re.IGNORECASE):
            return True

    if zoom_status_id:
        sid = task.get("status", {}).get("id")
        if sid and int(sid) == zoom_status_id:
            return True

    if zoom_project_id:
        pid = task.get("project", {}).get("id")
        if pid and int(pid) == zoom_project_id:
            return True

    return False


def is_cancellation_signal(task: dict) -> bool:
    """Проверка признаков отмены конференции"""
    name = task.get("name", "") or ""
    desc = task.get("description", "") or ""
    text = f"{name}\n{desc}"
    for pat in CANCEL_MARKERS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def extract_meta(description: str) -> Optional[dict]:
    """Извлечь скрытый JSON-маркер из описания задачи"""
    if not description or META_BEGIN not in description:
        return None
    m = META_PATTERN.search(description)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception as e:
            logger.warning(f"Не удалось распарсить ZOOM_META: {e}")
    return None


def inject_meta(description: str, meta: dict) -> str:
    """Записать/обновить скрытый маркер в описании"""
    meta_str = json.dumps(meta, ensure_ascii=False)
    new_marker = f"{META_BEGIN} {meta_str} {META_END}"

    if META_BEGIN in (description or ""):
        # Заменяем существующий
        return META_PATTERN.sub(new_marker, description)
    # Добавляем в конец
    if description:
        return f"{description}\n\n{new_marker}"
    return new_marker


def strip_meta(description: str) -> str:
    """Убрать маркер из описания"""
    if not description or META_BEGIN not in description:
        return description
    return META_PATTERN.sub("", description).strip()


# ─── Главный поллер ───────────────────────────────────────────────────────────


class ZoomPoller:
    def __init__(
        self,
        pf: PlanfixClient,
        zoom: ZoomClient,
        cfg,
        tg: TelegramClient = None,
        poll_interval: int = 60,
    ):
        self.pf = pf
        self.zoom = zoom
        self.tg = tg
        self.cfg = cfg
        self.poll_interval = poll_interval
        self._processed_in_run = set()
        self._last_modified_check = int(time.time())
        # Кеш состояния обработанных задач: {task_id: hash_of_relevant_fields}
        self._task_state_cache: dict = {}
        # Максимальный ID последней просканированной задачи
        self._max_known_id = 0
        self._initialized = False
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        """Главный цикл поллинга"""
        logger.info(
            f"🔄 Поллер запущен. Интервал: {self.poll_interval}s. "
            f"Аккаунт: {self.pf.base}"
        )
        while not self._stop:
            try:
                await self.tick()
            except Exception as e:
                logger.exception(f"Ошибка в poll-цикле: {e}")
            await asyncio.sleep(self.poll_interval)

    async def tick(self):
        """Одна итерация поллинга"""
        self._processed_in_run.clear()

        if not self._initialized:
            await self._initial_scan()
            self._initialized = True
            return

        # Сканируем хвост списка задач (там самые новые)
        tasks = self._fetch_tail_tasks(scan_pages=3)

        if not tasks:
            return

        # Обрабатываем все Zoom-задачи (новые ИЛИ изменённые)
        zoom_tasks = [
            t for t in tasks if is_zoom_task(
                t,
                zoom_status_id=self.cfg.status_active,
                zoom_project_id=self.cfg.planfix_zoom_project_id,
            )
        ]

        new_zoom_tasks = []
        for t in zoom_tasks:
            tid = t["id"]
            state_hash = self._compute_state_hash(t)
            if self._task_state_cache.get(tid) != state_hash:
                new_zoom_tasks.append(t)
                self._task_state_cache[tid] = state_hash

        if new_zoom_tasks:
            logger.info(
                f"Tick: {len(tasks)} задач в хвосте, {len(zoom_tasks)} Zoom-задач, "
                f"{len(new_zoom_tasks)} требуют обработки"
            )

        for task in new_zoom_tasks:
            if task["id"] in self._processed_in_run:
                continue
            try:
                await self._process_task(task)
            except Exception as e:
                logger.exception(f"Ошибка обработки задачи {task.get('id')}: {e}")
            self._processed_in_run.add(task["id"])

    async def _initial_scan(self):
        """
        Первичное сканирование при старте — кэшируем состояние существующих задач,
        чтобы не обрабатывать их повторно. Идём от последней страницы к началу.
        """
        logger.info("🔍 Первичное сканирование задач...")
        offset = 0
        total = 0

        # Сначала найдём общее количество (через быстрый скан)
        for page in range(50):  # макс 5000 задач
            res = self.pf.post("/task/list", {
                "offset": offset,
                "pageSize": 100,
                "fields": "id,name",
            })
            tasks = res.get("tasks", [])
            if not tasks:
                break
            total += len(tasks)
            for t in tasks:
                tid = t["id"]
                if tid > self._max_known_id:
                    self._max_known_id = tid
                # Не вычисляем state hash — пропускаем существующие при старте
                # (только новые, появившиеся после старта, будут обработаны)
                self._task_state_cache[tid] = "_initial_"
            if len(tasks) < 100:
                break
            offset += 100

        logger.info(
            f"🔍 Просканировано {total} задач, max_id={self._max_known_id}. "
            f"Поллинг готов к работе."
        )

    def _fetch_tail_tasks(self, scan_pages: int = 3) -> list:
        """
        Получить задачи с хвоста списка (там новейшие).
        scan_pages — сколько последних страниц по 100 сканировать.
        """
        try:
            # Сначала найдём общий размер списка через первую страницу
            first = self.pf.post("/task/list", {
                "offset": 0,
                "pageSize": 100,
                "fields": "id",
            })
            # Total может быть в разных местах
            total_count = first.get("total") or first.get("count")

            # Если total неизвестен — пробежимся пагинацией от 0
            if not total_count:
                # Соберём все
                offset = 0
                all_tasks = []
                for _ in range(50):
                    r = self.pf.post("/task/list", {
                        "offset": offset,
                        "pageSize": 100,
                        "fields": "id,name,description,status,project,assignees,owner,startDateTime,endDateTime",
                    })
                    items = r.get("tasks", [])
                    if not items:
                        break
                    all_tasks.extend(items)
                    if len(items) < 100:
                        break
                    offset += 100
                # Возвращаем последние scan_pages * 100
                tail_size = scan_pages * 100
                return all_tasks[-tail_size:] if all_tasks else []

            # Иначе берём ровно последние страницы
            results = []
            start_offset = max(0, total_count - scan_pages * 100)
            for page in range(scan_pages + 1):
                offset = start_offset + page * 100
                r = self.pf.post("/task/list", {
                    "offset": offset,
                    "pageSize": 100,
                    "fields": "id,name,description,status,project,assignees,owner,startDateTime,endDateTime",
                })
                items = r.get("tasks", [])
                if not items:
                    break
                results.extend(items)
                if len(items) < 100:
                    break
            return results
        except Exception as e:
            logger.error(f"Не удалось получить хвост задач: {e}")
            return []

    @staticmethod
    def _compute_state_hash(task: dict) -> str:
        """Хэш ключевых полей для определения изменений"""
        import hashlib
        key = json.dumps({
            "name": task.get("name"),
            "description": task.get("description") or "",
            "status_id": (task.get("status") or {}).get("id"),
            "start": task.get("startDateTime"),
            "end": task.get("endDateTime"),
            "assignees": task.get("assignees"),
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(key.encode()).hexdigest()

    async def _process_task(self, task_summary: dict):
        """Обработать одну Zoom-задачу"""
        task_id = task_summary["id"]

        # Получаем полные данные задачи
        try:
            full = self.pf.get_task(task_id)
        except Exception as e:
            logger.error(f"Не удалось получить задачу {task_id}: {e}")
            return

        # PlanFix может вернуть минимальный объект — мерджим
        task = {**task_summary, **(full or {})}
        if not task.get("name"):
            return  # пропускаем пустые

        meta = extract_meta(task.get("description") or "")

        # ─── Признак отмены ───────────────────────────────────────────────────
        if is_cancellation_signal(task):
            if meta and meta.get("meeting_id"):
                await self._cancel_meeting(task, meta)
            return

        # ─── Уже создана — проверяем, надо ли обновить ────────────────────────
        if meta and meta.get("meeting_id"):
            await self._maybe_update_meeting(task, meta)
            await self._sync_rsvp_subtasks(task)
            return

        # ─── Новая Zoom-задача — создаём встречу ──────────────────────────────
        await self._create_meeting_for_task(task)

    async def _create_meeting_for_task(self, task: dict):
        """Создать Zoom-встречу для задачи + Telegram + комментарий в PlanFix"""
        task_id = task["id"]
        name = task.get("name", f"Конференция #{task_id}")
        topic = self._extract_topic(name)
        pf_url = f"https://{self.cfg.planfix_account}.planfix.com/task/{task_id}"

        date_begin = self._extract_datetime(task.get("startDateTime") or task.get("dateBegin"))
        date_end = self._extract_datetime(task.get("endDateTime") or task.get("dateEnd"))

        if not date_begin:
            logger.info(f"Task {task_id}: нет startDateTime — пропускаю")
            try:
                self.pf.add_comment(
                    task_id,
                    "⚠️ Чтобы автоматически создать Zoom-встречу — укажите дату и время начала задачи.",
                    silent=True,
                )
            except Exception:
                pass
            return

        start_time = to_zoom_iso(date_begin, self.cfg.zoom_timezone)
        duration = calc_duration_minutes(date_begin, date_end, self.cfg.zoom_default_duration_min)
        time_str = format_meeting_time(date_begin, self.cfg.zoom_timezone)

        # ── Шаг 1: уведомление в Telegram о новой задаче ──────────────────────
        if self.tg:
            self.tg.send_message_safe(
                f"🆕 *Новая задача-конференция в PlanFix*\n\n"
                f"📌 *Тема:* {topic}\n"
                f"🕐 *Время:* {time_str}\n"
                f"⏱ *Длительность:* {duration} мин\n"
                f"🔗 [Открыть в PlanFix]({pf_url})\n\n"
                f"_Создаю Zoom-встречу..._",
                disable_web_page_preview=True,
            )

        logger.info(
            f"Создаю Zoom-встречу для task {task_id}: '{topic}' @ {start_time} ({duration} min)"
        )

        # ── Шаг 2: создаём встречу в Zoom ─────────────────────────────────────
        try:
            meeting = self.zoom.create_meeting(
                topic=topic,
                start_time_iso=start_time,
                duration_min=duration,
                timezone=self.cfg.zoom_timezone,
                agenda=f"PlanFix #{task_id}",
            )
        except Exception as e:
            logger.error(f"Zoom API ошибка: {e}")
            try:
                self.pf.add_comment(task_id, f"❌ Не удалось создать Zoom-встречу: {e}")
            except Exception:
                pass
            if self.tg:
                self.tg.send_message_safe(
                    f"❌ *Ошибка создания Zoom-встречи*\nЗадача: {topic}\n`{e}`"
                )
            return

        meeting_id = str(meeting["id"])
        join_url = meeting["join_url"]
        password = meeting.get("password", "")
        uuid = meeting.get("uuid", "")

        # Сохраняем метаданные в описании
        meta = {
            "meeting_id": meeting_id,
            "uuid": uuid,
            "join_url": join_url,
            "password": password,
            "topic": topic,
            "start_time": start_time,
            "duration": duration,
            "created_at": datetime.utcnow().isoformat(),
        }

        new_desc = inject_meta(task.get("description") or "", meta)
        try:
            self.pf.update_task(task_id, {"description": new_desc})
        except Exception as e:
            logger.warning(f"Не удалось обновить описание {task_id}: {e}")

        # ── Шаг 4: комментарий в PlanFix со ссылкой ──────────────────────────
        comment = (
            f"🎥 **Zoom-конференция создана автоматически**\n\n"
            f"📌 **Тема:** {topic}\n"
            f"🕐 **Время:** {time_str}\n"
            f"⏱ **Длительность:** {duration} мин\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 **Ссылка для подключения:**\n{join_url}\n\n"
            f"🔑 **Meeting ID:** `{meeting_id}`\n"
            f"🔐 **Пароль:** `{password}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 Чтобы отменить — добавьте `[ОТМЕНА]` в название задачи или `#cancel` в описание."
        )
        try:
            self.pf.add_comment(task_id, comment)
        except Exception as e:
            logger.warning(f"Не удалось добавить комментарий: {e}")

        # ── Шаг 5: уведомление в Telegram со ссылкой ─────────────────────────
        if self.tg:
            self.tg.send_message_safe(
                f"✅ *Zoom-встреча создана!*\n\n"
                f"📌 *Тема:* {topic}\n"
                f"🕐 *Время:* {time_str}\n"
                f"⏱ *Длительность:* {duration} мин\n\n"
                f"🔗 *Ссылка для подключения:*\n{join_url}\n\n"
                f"🔑 *Meeting ID:* `{meeting_id}`\n"
                f"🔐 *Пароль:* `{password}`\n\n"
                f"📋 [Задача в PlanFix]({pf_url})",
                disable_web_page_preview=True,
            )

        # ── Шаг 6: RSVP-подзадачи для участников ─────────────────────────────
        await self._sync_rsvp_subtasks(task)

        logger.info(f"✅ Task {task_id}: Zoom meeting {meeting_id} создан")

    async def _maybe_update_meeting(self, task: dict, meta: dict):
        """Проверить, изменились ли тема/время и обновить Zoom-встречу при необходимости"""
        task_id = task["id"]
        new_topic = self._extract_topic(task.get("name", ""))
        date_begin = self._extract_datetime(task.get("startDateTime") or task.get("dateBegin"))

        if not date_begin:
            return

        new_start = to_zoom_iso(date_begin, self.cfg.zoom_timezone)
        new_duration = calc_duration_minutes(
            date_begin,
            self._extract_datetime(task.get("endDateTime") or task.get("dateEnd")),
            self.cfg.zoom_default_duration_min,
        )

        # Сравниваем с тем, что было
        changed_topic = new_topic != meta.get("topic")
        changed_time = new_start != meta.get("start_time")
        changed_dur = new_duration != meta.get("duration")

        if not (changed_topic or changed_time or changed_dur):
            return

        try:
            self.zoom.update_meeting(
                meeting_id=meta["meeting_id"],
                topic=new_topic if changed_topic else None,
                start_time_iso=new_start if changed_time else None,
                duration_min=new_duration if changed_dur else None,
                timezone=self.cfg.zoom_timezone,
            )
        except Exception as e:
            logger.error(f"Zoom update error for {meta['meeting_id']}: {e}")
            return

        # Обновляем метаданные
        meta.update({
            "topic": new_topic,
            "start_time": new_start,
            "duration": new_duration,
            "updated_at": datetime.utcnow().isoformat(),
        })
        new_desc = inject_meta(task.get("description") or "", meta)
        try:
            self.pf.update_task(task_id, {"description": new_desc})
            time_str = format_meeting_time(date_begin, self.cfg.zoom_timezone)
            self.pf.add_comment(
                task_id,
                f"📅 **Zoom-встреча обновлена**\n"
                f"Новая тема: {new_topic}\n"
                f"Новое время: {time_str}\n"
                f"Длительность: {new_duration} мин",
            )
            logger.info(f"Task {task_id}: Zoom meeting {meta['meeting_id']} обновлён")
        except Exception as e:
            logger.warning(f"Не удалось обновить task {task_id}: {e}")

    async def _cancel_meeting(self, task: dict, meta: dict):
        """Отменить Zoom-встречу + уведомить Telegram"""
        task_id = task["id"]
        meeting_id = meta.get("meeting_id")
        topic = meta.get("topic", task.get("name", ""))
        pf_url = f"https://{self.cfg.planfix_account}.planfix.com/task/{task_id}"

        try:
            self.zoom.delete_meeting(meeting_id)
        except Exception as e:
            logger.error(f"Не удалось удалить Zoom встречу {meeting_id}: {e}")

        try:
            self.pf.add_comment(
                task_id,
                f"🚫 **Zoom-встреча отменена**\n\nMeeting ID `{meeting_id}` удалён из Zoom.",
            )
        except Exception:
            pass

        if self.tg:
            self.tg.send_message_safe(
                f"🚫 *Zoom-конференция ОТМЕНЕНА*\n\n"
                f"📌 *Тема:* {topic}\n"
                f"🔑 *Meeting ID:* `{meeting_id}`\n\n"
                f"📋 [Задача в PlanFix]({pf_url})",
                disable_web_page_preview=True,
            )

        # Удаляем маркер из описания + добавляем флаг отмены
        new_desc = strip_meta(task.get("description") or "")
        meta["cancelled"] = True
        meta["cancelled_at"] = datetime.utcnow().isoformat()
        new_desc = inject_meta(new_desc, meta)
        try:
            self.pf.update_task(task_id, {"description": new_desc})
        except Exception:
            pass

        logger.info(f"Task {task_id}: встреча {meeting_id} отменена")

    async def _sync_rsvp_subtasks(self, task: dict):
        """Создать RSVP-подзадачи для всех участников, у которых их ещё нет"""
        task_id = task["id"]
        participants = self.pf.get_task_participants(task)
        if not participants:
            return

        try:
            existing_subtasks = self.pf.get_subtasks(task_id)
        except Exception:
            existing_subtasks = []

        existing_users = set()
        for st in existing_subtasks:
            if RSVP_MARKER in (st.get("name") or ""):
                for u in (st.get("assignees", {}) or {}).get("users", []):
                    existing_users.add(u.get("id"))

        for p in participants:
            uid = p.get("id")
            if not uid or uid in existing_users:
                continue
            uname = p.get("name", f"Участник {uid}")

            try:
                self.pf.create_subtask(
                    parent_task_id=task_id,
                    name=f"{RSVP_MARKER} {uname} — подтвердите участие",
                    description=(
                        f"👋 {uname}, подтвердите участие в конференции.\n\n"
                        f"Измените статус этой задачи:\n"
                        f"  ✅ **«В работе»** = БУДУ\n"
                        f"  ✔ **«Завершенная»** = подтверждено\n"
                        f"  ❌ Закройте задачу с тегом #notgoing = НЕ БУДУ\n\n"
                        f"_Только вы можете изменить эту задачу_"
                    ),
                    assignee_id=uid,
                )
                logger.info(f"Создана RSVP-подзадача для {uname} в task {task_id}")
            except Exception as e:
                logger.warning(f"Не удалось создать RSVP для {uname}: {e}")

    @staticmethod
    def _extract_topic(name: str) -> str:
        """Извлечь чистую тему встречи (убираем триггерные префиксы)"""
        cleaned = name
        for pat in ZOOM_TRIGGERS + CANCEL_MARKERS:
            cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip(" :-—") or name

    @staticmethod
    def _extract_datetime(dt_value):
        """
        Извлечь дату/время из объекта PlanFix API.

        PlanFix возвращает datetime в формате:
          {"date": "16-01-2025", "time": "07:10", "datetime": "2025-01-16T07:10Z",
           "dateTimeUtcSeconds": "2025-01-16T07:10:00+0000"}

        Также поддерживает простые строки и Unix timestamps.
        """
        if not dt_value:
            return None
        if isinstance(dt_value, dict):
            # Предпочитаем dateTimeUtcSeconds, потом datetime
            return (
                dt_value.get("dateTimeUtcSeconds")
                or dt_value.get("datetime")
                or dt_value.get("date")
            )
        return dt_value
