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
from typing import Optional, Tuple

from planfix_client import PlanfixClient, PlanfixAPIError
from zoom_client import ZoomClient, ZoomAPIError
from telegram_client import TelegramClient
from reminder import ReminderManager
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

# Маркеры в комментариях — только они дают авторизованную отмену
CANCEL_COMMENT_MARKERS = [
    r"^\s*/cancel\b",
    r"^\s*/отмена\b",
    r"^\s*\[ОТМЕНА\]",
    r"^\s*\[ОТМЕНЕНА\]",
    r"^\s*ОТМЕНА\s*$",
    r"^\s*ОТМЕНИТЬ\s*$",
]

META_BEGIN = "[ZOOM_META_v1]"
META_END = "[/ZOOM_META_v1]"
META_PATTERN = re.compile(r"\[ZOOM_META_v1\]([^\[]+)\[/ZOOM_META_v1\]", re.DOTALL)
RSVP_MARKER = "[RSVP]"


def is_zoom_task(
    task: dict,
    zoom_status_id: int = 0,
    zoom_project_id: int = 0,
    zoom_template_id: int = 0,
) -> bool:
    """
    Проверка, является ли задача Zoom-конференцией.
    Сработает любое из условий:
      - Имя задачи содержит маркер ZOOM: / [ZOOM] / #zoom / Конференция Zoom
      - Шаблон/объект задачи = zoom_template_id
      - Статус задачи = zoom_status_id
      - Проект = zoom_project_id
    """
    name = task.get("name", "") or ""
    for pat in ZOOM_TRIGGERS:
        if re.search(pat, name, re.IGNORECASE):
            return True

    # Объект (шаблон) задачи в PlanFix v2
    if zoom_template_id:
        template = task.get("template") or task.get("object")
        if isinstance(template, dict):
            tid = template.get("id")
            if tid and int(tid) == zoom_template_id:
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
    """Проверка признаков отмены конференции в названии/описании"""
    name = task.get("name", "") or ""
    desc = task.get("description", "") or ""
    text = f"{name}\n{desc}"
    for pat in CANCEL_MARKERS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def is_authorized_cancel_comment(comment_text: str) -> bool:
    """Является ли текст комментария командой отмены"""
    if not comment_text:
        return False
    for pat in CANCEL_COMMENT_MARKERS:
        if re.search(pat, comment_text, re.IGNORECASE | re.MULTILINE):
            return True
    return False


def parse_user_id(owner: dict) -> Optional[int]:
    """
    PlanFix возвращает ID в разных форматах: 12 или "user:12".
    Извлекает числовой ID.
    """
    if not owner:
        return None
    raw = owner.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        # "user:12" -> 12
        if ":" in raw:
            try:
                return int(raw.split(":")[-1])
            except ValueError:
                return None
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def extract_meta(description: str) -> Optional[dict]:
    """Извлечь base64-закодированный JSON-маркер из описания задачи"""
    if not description or META_BEGIN not in description:
        return None
    m = META_PATTERN.search(description)
    if m:
        import base64
        try:
            data = m.group(1).strip()
            decoded = base64.b64decode(data).decode("utf-8")
            return json.loads(decoded)
        except Exception as e:
            logger.warning(f"Не удалось распарсить ZOOM_META: {e}")
    return None


def inject_meta(description: str, meta: dict) -> str:
    """Записать/обновить маркер в описании (base64 для компактности)"""
    import base64
    meta_str = json.dumps(meta, ensure_ascii=False)
    encoded = base64.b64encode(meta_str.encode("utf-8")).decode("ascii")
    new_marker = f"{META_BEGIN}{encoded}{META_END}"

    if META_BEGIN in (description or ""):
        return META_PATTERN.sub(new_marker, description)
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
        # Локи per-task_id для защиты от race condition (webhook + polling)
        self._task_locks: dict = {}
        # Менеджер напоминаний
        self.reminders = ReminderManager(pf, tg, cfg)

    def _get_lock(self, task_id: int) -> asyncio.Lock:
        if task_id not in self._task_locks:
            self._task_locks[task_id] = asyncio.Lock()
        return self._task_locks[task_id]

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
            logger.info("Tick: пусто (нет задач в хвосте)")
            return

        # Обрабатываем все Zoom-задачи (новые ИЛИ изменённые)
        zoom_tasks = [
            t for t in tasks if is_zoom_task(
                t,
                zoom_status_id=self.cfg.status_active,
                zoom_project_id=self.cfg.planfix_zoom_project_id,
                zoom_template_id=self.cfg.planfix_zoom_template_id,
            )
        ]

        new_zoom_tasks = []
        for t in zoom_tasks:
            tid = t["id"]
            state_hash = self._compute_state_hash(t)
            if self._task_state_cache.get(tid) != state_hash:
                new_zoom_tasks.append(t)
                self._task_state_cache[tid] = state_hash

        # Всегда логируем (легче дебажить)
        logger.info(
            f"Tick: {len(tasks)} задач в хвосте, {len(zoom_tasks)} Zoom-задач, "
            f"{len(new_zoom_tasks)} требуют обработки "
            f"(template_id={self.cfg.planfix_zoom_template_id})"
        )

        # ─── Проверяем напоминания для всех Zoom-задач (даже если ничего нового) ──
        try:
            await self.reminders.check_reminders(
                zoom_tasks,
                extract_meta_fn=extract_meta,
                inject_meta_fn=inject_meta,
            )
        except Exception as e:
            logger.exception(f"Ошибка в reminder check: {e}")

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
                        "fields": "id,name,description,status,project,assignees,owner,startDateTime,endDateTime,object,template",
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
                    "fields": "id,name,description,status,project,assignees,owner,startDateTime,endDateTime,object,template",
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
        """Обработать одну Zoom-задачу с локом против race condition"""
        task_id = task_summary["id"]

        lock = self._get_lock(task_id)
        if lock.locked():
            logger.info(f"Task {task_id}: уже обрабатывается, пропускаю")
            return

        async with lock:
            await self._process_task_locked(task_id, task_summary)

    async def _process_task_locked(self, task_id: int, task_summary: dict):
        # Получаем полные данные задачи (с владельцем и описанием)
        try:
            full = self.pf.get(
                f"/task/{task_id}",
                params={"fields": "id,name,description,status,owner,assignees,startDateTime,endDateTime,project,object,template"},
            )
            full_task = full.get("task", full)
        except Exception as e:
            logger.error(f"Не удалось получить задачу {task_id}: {e}")
            return

        # PlanFix может вернуть минимальный объект — мерджим
        task = {**task_summary, **(full_task or {})}
        if not task.get("name"):
            return  # пропускаем пустые

        meta = extract_meta(task.get("description") or "")

        # ─── Авторизованная отмена через комментарий /cancel ────────────────
        if meta and meta.get("meeting_id"):
            cancel_result = await self._check_authorized_cancel(task, meta)
            if cancel_result == "authorized":
                await self._cancel_meeting(task, meta)
                return
            elif cancel_result == "unauthorized":
                # Уже отправлено уведомление в _check_authorized_cancel
                return

        # ─── Признак отмены через имя задачи (доп. проверка на инициатора) ──
        if is_cancellation_signal(task):
            if meta and meta.get("meeting_id"):
                # Проверяем, что инициатор в task.owner
                current_owner_id = parse_user_id(task.get("owner") or {})
                meta_initiator_id = meta.get("initiator_id")
                if meta_initiator_id and current_owner_id == parse_user_id({"id": meta_initiator_id}):
                    await self._cancel_meeting(task, meta)
                else:
                    # Возвращаем имя — отмена через имя ненадёжна
                    logger.warning(
                        f"Task {task_id}: отмена через имя — инициатор не подтверждён, игнорируем"
                    )
            return

        # ─── Уже создана — проверяем, надо ли обновить ────────────────────────
        if meta and meta.get("meeting_id"):
            await self._maybe_update_meeting(task, meta)
            await self._sync_rsvp_subtasks(task)
            return

        # ─── Новая Zoom-задача — создаём встречу ──────────────────────────────
        await self._create_meeting_for_task(task)

    async def _check_authorized_cancel(self, task: dict, meta: dict) -> str:
        """
        Проверить, есть ли в свежих комментариях команда отмены от инициатора.

        Returns:
            "authorized"   — инициатор оставил команду /cancel
            "unauthorized" — кто-то другой пытался отменить (отправляем warning)
            "none"         — команд отмены нет
        """
        task_id = task["id"]
        meta_initiator_id = meta.get("initiator_id")
        if not meta_initiator_id:
            return "none"

        # Получаем последние 20 комментариев
        try:
            res = self.pf.post(
                f"/task/{task_id}/comments/list",
                {"offset": 0, "pageSize": 20, "fields": "id,description,owner,dateTime"},
            )
            comments = res.get("comments", [])
        except Exception as e:
            logger.warning(f"Не удалось получить комментарии {task_id}: {e}")
            return "none"

        # Время создания meta — учитываем только комментарии после создания встречи
        meta_created_at = meta.get("created_at", "")

        for c in comments:
            text = (c.get("description") or "").strip()
            # Убираем HTML теги — PlanFix может их добавлять
            clean_text = re.sub(r"<[^>]+>", "", text)
            if not is_authorized_cancel_comment(clean_text):
                continue

            comment_owner_id = parse_user_id(c.get("owner") or {})
            comment_dt = (c.get("dateTime") or {}).get("dateTimeUtcSeconds", "")

            # Пропускаем старые (до создания встречи)
            if meta_created_at and comment_dt and comment_dt < meta_created_at:
                continue

            # Проверяем, не обработан ли уже этот комментарий
            if meta.get("cancel_comment_id") == c.get("id"):
                # Уже отметили этот комментарий — возможно был запрет, не повторяем
                return "none"

            # Сравниваем автора комментария с инициатором
            if comment_owner_id == parse_user_id({"id": meta_initiator_id}):
                logger.info(f"Task {task_id}: авторизованная отмена от инициатора (comment {c.get('id')})")
                return "authorized"

            # Кто-то другой пытается отменить
            logger.warning(
                f"Task {task_id}: попытка отмены не-инициатором "
                f"(comment_owner={comment_owner_id}, meta_initiator={meta_initiator_id})"
            )
            try:
                self.pf.add_comment(
                    task_id,
                    f'<b>⚠️ Отмена встречи отклонена</b><br><br>'
                    f'Только инициатор (<b>{meta.get("initiator_name", "?")}</b>) '
                    f'может отменить эту конференцию.',
                )
            except Exception:
                pass
            if self.tg:
                self.tg.send_message_safe(
                    f"⚠️ *Попытка несанкционированной отмены*\n\n"
                    f"Тема: {meta.get('topic')}\n"
                    f"Инициатор: {meta.get('initiator_name')}\n"
                    f"_Только инициатор может отменить встречу._"
                )
            # Помечаем этот comment_id, чтобы не повторять
            meta["cancel_comment_id"] = c.get("id")
            new_desc = inject_meta(task.get("description") or "", meta)
            try:
                self.pf.update_task(task_id, {"description": new_desc})
            except Exception:
                pass
            return "unauthorized"

        return "none"

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

        # Извлекаем инициатора (постановщика задачи)
        initiator = task.get("owner") or {}
        initiator_id = initiator.get("id") if isinstance(initiator, dict) else None
        initiator_name = (
            initiator.get("name")
            if isinstance(initiator, dict)
            else None
        ) or "не указан"

        # Список участников для отображения
        participants = self.pf.get_task_participants(task)
        participant_names = [p.get("name", "?") for p in participants]
        participants_text = ", ".join(participant_names) if participant_names else "не указаны"

        # ── Шаг 1: уведомление в Telegram о новой задаче ──────────────────────
        if self.tg:
            self.tg.send_message_safe(
                f"🆕 *Новая задача-конференция в PlanFix*\n\n"
                f"📌 *Тема:* {topic}\n"
                f"🕐 *Время:* {time_str}\n"
                f"⏱ *Длительность:* {duration} мин\n"
                f"👤 *Инициатор:* {initiator_name}\n"
                f"👥 *Участники:* {participants_text}\n"
                f"🔗 [Открыть в PlanFix]({pf_url})\n\n"
                f"_Создаю Zoom-встречу..._",
                disable_web_page_preview=True,
            )

        logger.info(
            f"Создаю Zoom-встречу для task {task_id}: '{topic}' @ {start_time} ({duration} min) "
            f"by initiator={initiator_name}"
        )

        # ── Шаг 2: создаём встречу в Zoom ─────────────────────────────────────
        # В agenda Zoom указываем инициатора и участников
        agenda_text = (
            f"Инициатор: {initiator_name}\n"
            f"Участники: {participants_text}\n"
            f"PlanFix задача: {pf_url}"
        )
        try:
            meeting = self.zoom.create_meeting(
                topic=topic,
                start_time_iso=start_time,
                duration_min=duration,
                timezone=self.cfg.zoom_timezone,
                agenda=agenda_text,
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

        # Сохраняем метаданные в описании (включая инициатора для access control)
        meta = {
            "meeting_id": meeting_id,
            "uuid": uuid,
            "join_url": join_url,
            "password": password,
            "topic": topic,
            "start_time": start_time,
            "duration": duration,
            "created_at": datetime.utcnow().isoformat(),
            "initiator_id": initiator_id,
            "initiator_name": initiator_name,
        }

        new_desc = inject_meta(task.get("description") or "", meta)
        try:
            self.pf.update_task(task_id, {"description": new_desc})
        except Exception as e:
            logger.warning(f"Не удалось обновить описание {task_id}: {e}")

        # ── Шаг 4: комментарий в PlanFix со ссылкой (HTML — PlanFix вырезает \n) ─
        comment = (
            f'<b>🎥 Zoom-конференция создана автоматически</b>'
            f'<br><br>'
            f'📌 <b>Тема:</b> {topic}<br>'
            f'🕐 <b>Время:</b> {time_str}<br>'
            f'⏱ <b>Длительность:</b> {duration} мин<br>'
            f'👤 <b>Инициатор:</b> {initiator_name}<br>'
            f'👥 <b>Участники:</b> {participants_text}'
            f'<br><br>━━━━━━━━━━━━━━━━━━━━━━<br>'
            f'🔗 <b>Ссылка для подключения:</b><br>'
            f'<a href="{join_url}">{join_url}</a>'
            f'<br><br>'
            f'🔑 <b>Meeting ID:</b> {meeting_id}<br>'
            f'🔐 <b>Пароль:</b> {password}'
            f'<br>━━━━━━━━━━━━━━━━━━━━━━<br><br>'
            f'<b>❗ Отмена встречи</b><br>'
            f'Только инициатор ({initiator_name}) может отменить. Для отмены — '
            f'напишите в комментарии: <b>/cancel</b>'
            f'<br><br>'
            f'<b>📋 Подтверждение участия (RSVP)</b><br>'
            f'Для каждого участника создана подзадача ниже. Каждый видит только свою — '
            f'меняет статус: «В работе» = БУДУ, «Отказ»/«Отмененная» = НЕ БУДУ.'
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
                f"⏱ *Длительность:* {duration} мин\n"
                f"👤 *Инициатор:* {initiator_name}\n"
                f"👥 *Участники:* {participants_text}\n\n"
                f"🔗 *Ссылка для подключения:*\n{join_url}\n\n"
                f"🔑 *Meeting ID:* `{meeting_id}`\n"
                f"🔐 *Пароль:* `{password}`\n\n"
                f"📋 [Задача в PlanFix]({pf_url})\n\n"
                f"_Каждому участнику создана подзадача RSVP в PlanFix._",
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
                f'<b>📅 Zoom-встреча обновлена</b><br><br>'
                f'📌 <b>Тема:</b> {new_topic}<br>'
                f'🕐 <b>Время:</b> {time_str}<br>'
                f'⏱ <b>Длительность:</b> {new_duration} мин',
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
                f'<b>🚫 Zoom-встреча отменена</b><br><br>'
                f'Meeting ID <b>{meeting_id}</b> удалён из Zoom.<br>'
                f'Участники получили уведомление об отмене.',
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
        """
        Создать RSVP-подзадачи для участников, у которых их ещё нет.
        Список уже созданных RSVP хранится в ZOOM_META (rsvp_users)
        — это надёжнее чем запрашивать подзадачи у PlanFix
        (API не возвращает связь parent/children стабильно).
        """
        task_id = task["id"]
        participants = self.pf.get_task_participants(task)
        if not participants:
            logger.info(f"Task {task_id}: нет участников — RSVP не создаём")
            return

        # Достаём meta и список уже созданных RSVP
        meta = extract_meta(task.get("description") or "") or {}
        rsvp_users = set(meta.get("rsvp_users", []))

        join_url = meta.get("join_url", "")
        meeting_id = meta.get("meeting_id", "")
        password = meta.get("password", "")
        topic = meta.get("topic", task.get("name", ""))
        time_str = format_meeting_time(
            self._extract_datetime(task.get("startDateTime")),
            self.cfg.zoom_timezone,
        )

        created_any = False
        for p in participants:
            uid = parse_user_id(p)
            if not uid or uid in rsvp_users:
                continue
            uname = p.get("name", f"Участник {uid}")

            # HTML формат (PlanFix вырезает \n но сохраняет <br>, <b>, <i>)
            description = (
                f'<b>👋 {uname}, подтвердите участие в Zoom-конференции</b><br><br>'
                f'📌 <b>Тема:</b> {topic}<br>'
                f'🕐 <b>Когда:</b> {time_str}<br>'
            )
            if join_url:
                description += (
                    f'<br>🔗 <b>Ссылка:</b> <a href="{join_url}">{join_url}</a><br>'
                    f'🔑 <b>Meeting ID:</b> {meeting_id}<br>'
                    f'🔐 <b>Пароль:</b> {password}<br>'
                )
            description += (
                f'<br>━━━━━━━━━━━━━━━━━━━━<br>'
                f'<b>Как ответить:</b><br>'
                f'✅ <b>«В работе»</b> = БУДУ на встрече<br>'
                f'🚫 <b>«Отказ»</b> или «Отмененная» = НЕ БУДУ<br><br>'
                f'<i>Эта подзадача назначена только вам.</i>'
            )

            try:
                self.pf.create_subtask(
                    parent_task_id=task_id,
                    name=f"{RSVP_MARKER} {uname}: будете на встрече?",
                    description=description,
                    assignee_id=uid,
                    object_id=self.cfg.planfix_rsvp_template_id or None,
                )
                rsvp_users.add(uid)
                created_any = True
                logger.info(
                    f"✅ RSVP-подзадача создана для {uname} (uid={uid}) в task {task_id} "
                    f"(rsvp_object={self.cfg.planfix_rsvp_template_id or 'default'})"
                )
            except Exception as e:
                logger.warning(f"Не удалось создать RSVP для {uname}: {e}")

        # Если создали хотя бы одну — обновляем meta
        if created_any:
            meta["rsvp_users"] = list(rsvp_users)
            new_desc = inject_meta(task.get("description") or "", meta)
            try:
                self.pf.update_task(task_id, {"description": new_desc})
            except Exception as e:
                logger.warning(f"Не удалось обновить meta после RSVP: {e}")

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
