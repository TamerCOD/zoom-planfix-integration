"""
Главный сервер интеграции PlanFix ↔ Zoom.

Архитектура:
  ┌─ Background Poller (основной механизм) ────────────────────────────────┐
  │  Каждые N секунд опрашивает PlanFix REST API:                          │
  │  - Ищет задачи с маркерами Zoom (имя содержит "ZOOM:" или "[ZOOM]")   │
  │  - Создаёт/обновляет/отменяет встречи в Zoom                          │
  │  - Синхронизирует RSVP-подзадачи участников                           │
  │  Не требует никакой настройки в PlanFix UI/Автоматизациях!            │
  └───────────────────────────────────────────────────────────────────────┘

  ┌─ Webhook endpoint /webhook/planfix (резервный) ───────────────────────┐
  │  Принимает POST от PlanFix Автоматизаций (если они настроены)         │
  │  Полезно для мгновенной реакции на события без ожидания поллинга      │
  └───────────────────────────────────────────────────────────────────────┘

  ┌─ Endpoint /health ────────────────────────────────────────────────────┐
  │  Healthcheck для Railway / мониторинга                                │
  └───────────────────────────────────────────────────────────────────────┘
"""
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from config import Config
from planfix_client import PlanfixClient, PlanfixAPIError
from zoom_client import ZoomClient, ZoomAPIError
from telegram_client import TelegramClient
from poller import ZoomPoller

# ─── Логирование ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("zoom-planfix")

# ─── Инициализация ────────────────────────────────────────────────────────────

cfg = Config()
pf = PlanfixClient(cfg.planfix_account, cfg.planfix_token)
zoom = ZoomClient(cfg.zoom_account_id, cfg.zoom_client_id, cfg.zoom_client_secret)
tg = TelegramClient(cfg.telegram_bot_token, cfg.telegram_chat_id) if cfg.telegram_bot_token else None

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
poller = ZoomPoller(pf, zoom, cfg, tg=tg, poll_interval=POLL_INTERVAL)
_poller_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Старт/стоп фонового поллера"""
    global _poller_task
    try:
        cfg.validate()
        logger.info("✅ Конфигурация ОК")
    except Exception as e:
        logger.error(f"⚠️ Конфигурация: {e}")

    logger.info(f"🚀 Стартую сервер (poll={POLL_INTERVAL}s)")
    logger.info(f"   PlanFix:    {cfg.planfix_account}.planfix.com")
    logger.info(f"   Zoom:       account={cfg.zoom_account_id[:8]}...")
    logger.info(f"   Telegram:   {'ENABLED chat=' + cfg.telegram_chat_id if tg else 'DISABLED'}")
    logger.info(f"   Timezone:   {cfg.zoom_timezone}")

    # Запускаем поллер
    _poller_task = asyncio.create_task(poller.run())

    yield

    logger.info("🛑 Остановка сервера")
    poller.stop()
    if _poller_task:
        _poller_task.cancel()
        try:
            await _poller_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="PlanFix ↔ Zoom Integration",
    version="2.0.0",
    description="Автономная интеграция PlanFix и Zoom через поллинг.",
    lifespan=lifespan,
)


# ─── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return {
        "service": "PlanFix ↔ Zoom Integration",
        "version": "2.0.0",
        "mode": "polling",
        "poll_interval_s": POLL_INTERVAL,
        "endpoints": ["/health", "/status", "/webhook/planfix", "/poll-now"],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "planfix-zoom-integration"}


@app.get("/status")
async def status():
    """Расширенный статус: подключения, последний поллинг"""
    pf_ok = False
    zoom_ok = False
    try:
        pf.post("/task/list", {"offset": 0, "pageSize": 1})
        pf_ok = True
    except Exception as e:
        logger.warning(f"PlanFix check failed: {e}")
    try:
        zoom._get_access_token()
        zoom_ok = True
    except Exception as e:
        logger.warning(f"Zoom check failed: {e}")

    return {
        "service": "planfix-zoom",
        "planfix_connected": pf_ok,
        "zoom_connected": zoom_ok,
        "last_poll_check_ts": poller._last_modified_check,
        "poll_interval_s": POLL_INTERVAL,
    }


@app.post("/poll-now")
async def poll_now():
    """Запустить поллинг прямо сейчас (для тестирования)"""
    try:
        await poller.tick()
        return {"status": "ok", "message": "Poll cycle executed"}
    except Exception as e:
        logger.exception("Manual poll failed")
        return {"status": "error", "message": str(e)}


@app.post("/webhook/planfix")
async def planfix_webhook(request: Request):
    """
    Резервный webhook-endpoint. Если в PlanFix настроены Автоматизации с HTTP-запросом,
    они могут попадать сюда и триггерить немедленный пересчёт задачи.
    """
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    task_id = data.get("task_id") or data.get("taskId")
    logger.info(f"Webhook received: task_id={task_id}, event={data.get('event')}")

    if task_id:
        # Триггерим обработку конкретной задачи
        try:
            full = pf.get_task(int(task_id))
            await poller._process_task(full)
            return {"status": "ok", "task_id": task_id}
        except Exception as e:
            logger.exception(f"Webhook processing error: {e}")
            return {"status": "error", "detail": str(e)}

    # Если task_id не передан — запускаем общий tick
    try:
        await poller.tick()
        return {"status": "ok", "message": "Tick triggered"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or cfg.server_port)
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level=cfg.log_level.lower(),
    )
