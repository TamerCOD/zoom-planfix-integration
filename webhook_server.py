"""
Webhook-сервер FastAPI — точка входа для событий PlanFix.

Схема потока:
  PlanFix Automation (HTTP-запрос) ──► POST /webhook/planfix
      ├─ task.created  ──► create_zoom_meeting()
      ├─ task.updated  ──► handle_status_change() / handle_time_change()
      └─ (любое)       ──► sync_rsvp()

Запуск:
  uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response

from config import Config
from planfix_client import PlanfixClient, PlanfixAPIError
from zoom_client import ZoomClient, ZoomAPIError
from rsvp_manager import RSVPManager
from time_utils import to_zoom_iso, calc_duration_minutes, format_meeting_time

# ─── Инициализация ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

cfg = Config()
pf = PlanfixClient(cfg.planfix_account, cfg.planfix_token)
zoom = ZoomClient(cfg.zoom_account_id, cfg.zoom_client_id, cfg.zoom_client_secret)
rsvp = RSVPManager(pf, cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Проверка конфигурации при старте"""
    try:
        cfg.validate()
        logger.info("✅ Конфигурация проверена")
        logger.info(f"   PlanFix: {cfg.planfix_account}.planfix.com")
        logger.info(f"   Template ID: {cfg.planfix_zoom_template_id}")
        logger.info(f"   Port: {cfg.server_port}")
    except ValueError as e:
        logger.error(f"❌ Ошибка конфигурации:\n{e}")
    yield


app = FastAPI(
    title="PlanFix ↔ Zoom Integration",
    version="1.0.0",
    lifespan=lifespan,
)

# ─── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Проверка работоспособности сервера"""
    return {"status": "ok", "service": "planfix-zoom-integration"}


@app.post("/webhook/planfix")
async def planfix_webhook(request: Request):
    """
    Главный webhook-обработчик событий от PlanFix.

    PlanFix отправляет POST-запрос при каждом триггере автоматизации.
    Тело запроса формируем сами в настройках автоматизации PlanFix.

    Ожидаемый формат тела (настраивается в PlanFix → Автоматизация → HTTP-запрос):
    {
      "event": "task.created",       // или "task.updated", "task.cancelled"
      "task_id": 12345,
      "status_id": 10,               // только для task.updated
      "prev_status_id": 5,           // предыдущий статус
      "changed_fields": "status,dateBegin"  // список изменённых полей
    }
    """
    body_bytes = await request.body()

    # Логируем входящий запрос (первые 500 символов)
    logger.info(f"Webhook received: {body_bytes[:500]}")

    try:
        data = json.loads(body_bytes)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook body")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = data.get("event", "")
    task_id = data.get("task_id")

    if not task_id:
        logger.warning("Webhook: task_id не указан")
        return {"status": "skip", "reason": "no task_id"}

    task_id = int(task_id)
    logger.info(f"Processing event='{event}' task_id={task_id}")

    # ─── Диспетчер событий ────────────────────────────────────────────────────
    try:
        if event == "task.created":
            await handle_task_created(task_id)

        elif event == "task.cancelled":
            await handle_task_cancelled(task_id)

        elif event == "task.completed":
            await handle_task_completed(task_id)

        elif event == "task.updated":
            changed_fields = data.get("changed_fields", "")
            new_status_id = int(data.get("status_id", 0))
            prev_status_id = int(data.get("prev_status_id", 0))
            await handle_task_updated(
                task_id,
                new_status_id=new_status_id,
                prev_status_id=prev_status_id,
                changed_fields=changed_fields,
            )

        elif event == "task.participant_added":
            await handle_participant_change(task_id)

        else:
            logger.info(f"Unknown event '{event}', skipping")

    except PlanfixAPIError as e:
        logger.error(f"PlanFix API error for task {task_id}: {e}")
        return {"status": "error", "detail": str(e)}
    except ZoomAPIError as e:
        logger.error(f"Zoom API error for task {task_id}: {e}")
        return {"status": "error", "detail": str(e)}
    except Exception as e:
        logger.exception(f"Unexpected error for task {task_id}: {e}")
        return {"status": "error", "detail": str(e)}

    return {"status": "ok"}


# ─── Обработчики событий ─────────────────────────────────────────────────────


async def handle_task_created(task_id: int):
    """
    Задача создана → создаём встречу Zoom, прикрепляем данные к задаче,
    создаём RSVP-подзадачи для участников.
    """
    task = pf.get_task(task_id)

    # Проверяем, что это задача-конференция (по шаблону или проекту)
    if not _is_zoom_conference_task(task):
        logger.info(f"Task {task_id} is not a Zoom conference, skipping")
        return

    # Проверяем, не создана ли встреча уже
    existing_meeting_id = pf.get_custom_field_value(task, cfg.field_zoom_meeting_id)
    if existing_meeting_id:
        logger.info(f"Task {task_id} already has Zoom meeting {existing_meeting_id}, skipping")
        return

    # Получаем время начала
    date_begin = task.get("dateBegin") or task.get("dateStart")
    if not date_begin:
        pf.add_comment(
            task_id,
            "⚠️ **Zoom встреча не создана**: не указана дата и время начала конференции.\n"
            "Пожалуйста, укажите дату начала — встреча будет создана автоматически при следующем обновлении задачи.",
        )
        logger.warning(f"Task {task_id}: dateBegin не задан")
        return

    date_end = task.get("dateEnd")
    topic = task.get("name", f"Конференция #{task_id}")
    description = task.get("description", "")
    duration = calc_duration_minutes(date_begin, date_end, cfg.zoom_default_duration_min)
    start_time_zoom = to_zoom_iso(date_begin, cfg.zoom_timezone)

    logger.info(f"Creating Zoom meeting: topic='{topic}', start={start_time_zoom}, duration={duration}min")

    # Создаём встречу в Zoom
    meeting = zoom.create_meeting(
        topic=topic,
        start_time_iso=start_time_zoom,
        duration_min=duration,
        timezone=cfg.zoom_timezone,
        agenda=f"PlanFix #{task_id}: {description[:200]}" if description else f"PlanFix #{task_id}",
    )

    meeting_id = str(meeting["id"])
    join_url = meeting["join_url"]
    password = meeting.get("password", "")
    uuid = meeting.get("uuid", "")

    # Сохраняем данные встречи в кастомных полях задачи
    fields_to_set = []
    if cfg.field_zoom_meeting_id:
        fields_to_set.append({"field": {"id": cfg.field_zoom_meeting_id}, "value": meeting_id})
    if cfg.field_zoom_join_url:
        fields_to_set.append({"field": {"id": cfg.field_zoom_join_url}, "value": join_url})
    if cfg.field_zoom_password:
        fields_to_set.append({"field": {"id": cfg.field_zoom_password}, "value": password})
    if cfg.field_zoom_uuid:
        fields_to_set.append({"field": {"id": cfg.field_zoom_uuid}, "value": uuid})

    if fields_to_set:
        pf.set_custom_fields(task_id, fields_to_set)

    # Добавляем комментарий с данными встречи
    time_str = format_meeting_time(date_begin, cfg.zoom_timezone)
    comment = _build_meeting_comment(
        topic=topic,
        time_str=time_str,
        duration=duration,
        join_url=join_url,
        meeting_id=meeting_id,
        password=password,
    )
    pf.add_comment(task_id, comment)

    # Создаём RSVP-подзадачи для участников
    rsvp_result = rsvp.sync_rsvp_for_task(task_id, task)
    if rsvp_result["created"]:
        logger.info(
            f"Task {task_id}: создано {len(rsvp_result['created'])} RSVP-подзадач"
        )

    # Переводим задачу в статус "Актуальная" если статус задан
    if cfg.status_active:
        pf.update_task(task_id, {"status": {"id": cfg.status_active}})

    logger.info(f"✅ Task {task_id}: Zoom meeting {meeting_id} создана и прикреплена")


async def handle_task_cancelled(task_id: int):
    """Задача отменена → удаляем встречу Zoom"""
    task = pf.get_task(task_id)

    if not _is_zoom_conference_task(task):
        return

    meeting_id = pf.get_custom_field_value(task, cfg.field_zoom_meeting_id)
    if not meeting_id:
        logger.info(f"Task {task_id}: нет Meeting ID, нечего отменять")
        return

    # Удаляем встречу в Zoom
    deleted = zoom.delete_meeting(meeting_id)
    if deleted:
        pf.add_comment(
            task_id,
            f"🚫 **Конференция отменена**\n\n"
            f"Zoom встреча (Meeting ID: `{meeting_id}`) была удалена.\n"
            f"Участники получат уведомление об отмене от Zoom.",
        )
        # Очищаем поля
        _clear_zoom_fields(task_id)
        logger.info(f"Task {task_id}: Zoom meeting {meeting_id} удалена")
    else:
        logger.warning(f"Task {task_id}: не удалось удалить Zoom meeting {meeting_id}")


async def handle_task_completed(task_id: int):
    """Задача завершена → отмечаем комментарием"""
    task = pf.get_task(task_id)
    if not _is_zoom_conference_task(task):
        return

    meeting_id = pf.get_custom_field_value(task, cfg.field_zoom_meeting_id)

    # Добавляем сводку RSVP
    rsvp_comment = rsvp.format_rsvp_comment(task_id)
    pf.add_comment(
        task_id,
        f"✅ **Конференция завершена**\n\n"
        f"{rsvp_comment}\n\n"
        f"_Meeting ID: {meeting_id}_",
    )
    logger.info(f"Task {task_id}: конференция завершена")


async def handle_task_updated(
    task_id: int,
    new_status_id: int,
    prev_status_id: int,
    changed_fields: str,
):
    """Задача обновлена → обрабатываем изменения статуса и времени"""
    task = pf.get_task(task_id)

    if not _is_zoom_conference_task(task):
        return

    # ── Изменение статуса ─────────────────────────────────────────────────────
    if new_status_id and new_status_id != prev_status_id:
        if cfg.status_cancelled and new_status_id == cfg.status_cancelled:
            await handle_task_cancelled(task_id)
            return
        elif cfg.status_completed and new_status_id == cfg.status_completed:
            await handle_task_completed(task_id)
            return

    # ── Изменение времени / темы ───────────────────────────────────────────────
    changed = [f.strip() for f in changed_fields.split(",") if f.strip()]
    time_fields = {"dateBegin", "dateStart", "dateEnd", "name"}

    if time_fields.intersection(changed):
        meeting_id = pf.get_custom_field_value(task, cfg.field_zoom_meeting_id)
        if not meeting_id:
            # Если встречи нет — попробуем создать (возможно, время только что добавили)
            date_begin = task.get("dateBegin") or task.get("dateStart")
            if date_begin:
                await handle_task_created(task_id)
            return

        # Обновляем встречу Zoom
        date_begin = task.get("dateBegin") or task.get("dateStart")
        date_end = task.get("dateEnd")
        topic = task.get("name", f"Конференция #{task_id}")
        start_time = to_zoom_iso(date_begin, cfg.zoom_timezone)
        duration = calc_duration_minutes(date_begin, date_end, cfg.zoom_default_duration_min)

        zoom.update_meeting(
            meeting_id=meeting_id,
            topic=topic,
            start_time_iso=start_time,
            duration_min=duration,
            timezone=cfg.zoom_timezone,
        )

        time_str = format_meeting_time(date_begin, cfg.zoom_timezone)
        pf.add_comment(
            task_id,
            f"📅 **Встреча Zoom обновлена**\n\n"
            f"Новое время: **{time_str}**\n"
            f"Длительность: {duration} мин\n"
            f"Тема: {topic}\n\n"
            f"_Meeting ID: {meeting_id}_",
        )
        logger.info(f"Task {task_id}: Zoom meeting {meeting_id} обновлена")

    # ── Изменение участников ──────────────────────────────────────────────────
    participant_fields = {"assignees", "members", "auditors"}
    if participant_fields.intersection(changed):
        await handle_participant_change(task_id)


async def handle_participant_change(task_id: int):
    """Добавлен новый участник → создаём RSVP-подзадачу"""
    task = pf.get_task(task_id)
    if not _is_zoom_conference_task(task):
        return

    rsvp_result = rsvp.sync_rsvp_for_task(task_id, task)
    if rsvp_result["created"]:
        names = ", ".join(rsvp_result["created"])
        pf.add_comment(
            task_id,
            f"👥 **Добавлены новые участники**\n\n"
            f"Созданы запросы на подтверждение участия для:\n"
            + "\n".join(f"  • {n}" for n in rsvp_result["created"]),
        )
        logger.info(f"Task {task_id}: RSVP создано для {names}")


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _is_zoom_conference_task(task: dict) -> bool:
    """Проверить, является ли задача конференцией Zoom"""
    # Проверка по шаблону
    if cfg.planfix_zoom_template_id:
        template_id = task.get("template", {}).get("id")
        if template_id and int(template_id) == cfg.planfix_zoom_template_id:
            return True

    # Проверка по проекту
    if cfg.planfix_zoom_project_id:
        project_id = task.get("project", {}).get("id")
        if project_id and int(project_id) == cfg.planfix_zoom_project_id:
            return True

    # Если ни шаблон, ни проект не заданы — считаем все задачи конференциями
    if not cfg.planfix_zoom_template_id and not cfg.planfix_zoom_project_id:
        return True

    return False


def _build_meeting_comment(
    topic: str,
    time_str: str,
    duration: int,
    join_url: str,
    meeting_id: str,
    password: str,
) -> str:
    """Сформировать комментарий с данными Zoom-встречи"""
    return (
        f"🎥 **Zoom-конференция создана автоматически**\n\n"
        f"📌 **Тема:** {topic}\n"
        f"🕐 **Время:** {time_str}\n"
        f"⏱ **Длительность:** {duration} мин\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 **Ссылка для подключения:**\n"
        f"{join_url}\n\n"
        f"🔑 **Meeting ID:** `{meeting_id}`\n"
        f"🔐 **Пароль:** `{password}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💡 _Скопируйте ссылку или нажмите «Открыть Zoom»_\n"
        f"_Для подтверждения участия — отметьте свою подзадачу ниже_"
    )


def _clear_zoom_fields(task_id: int):
    """Очистить поля Zoom после отмены встречи"""
    fields = []
    if cfg.field_zoom_meeting_id:
        fields.append({"field": {"id": cfg.field_zoom_meeting_id}, "value": ""})
    if cfg.field_zoom_uuid:
        fields.append({"field": {"id": cfg.field_zoom_uuid}, "value": ""})
    if fields:
        try:
            pf.set_custom_fields(task_id, fields)
        except Exception as e:
            logger.warning(f"Не удалось очистить поля Zoom для task {task_id}: {e}")


# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=cfg.server_port,
        reload=False,
        log_level=cfg.log_level.lower(),
    )
