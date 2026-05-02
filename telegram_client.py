"""
Клиент Telegram Bot API для отправки уведомлений в чат/группу.
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramClient:
    """Минималистичный клиент для отправки сообщений в Telegram."""

    def __init__(self, token: str, default_chat_id: str = ""):
        self.token = token
        self.default_chat_id = default_chat_id
        self.api_base = f"https://api.telegram.org/bot{token}"

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: str = "Markdown",
        disable_web_page_preview: bool = False,
        reply_to: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Отправить сообщение в чат.
        Возвращает Telegram-ответ или None при ошибке.
        """
        chat = chat_id or self.default_chat_id
        if not chat:
            logger.warning("Telegram chat_id не задан — пропускаю отправку")
            return None

        payload = {
            "chat_id": chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to

        try:
            r = requests.post(f"{self.api_base}/sendMessage", json=payload, timeout=10)
            if not r.ok:
                logger.error(f"Telegram sendMessage HTTP {r.status_code}: {r.text[:200]}")
                return None
            return r.json()
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return None

    def send_message_safe(self, text: str, **kwargs) -> Optional[dict]:
        """
        Отправить с автоматическим fallback на plain text при ошибке Markdown.
        """
        result = self.send_message(text, **kwargs)
        if result is None and kwargs.get("parse_mode", "Markdown") == "Markdown":
            # Пробуем без Markdown (escape-проблемы)
            kwargs_no_md = {**kwargs, "parse_mode": ""}
            return self.send_message(text, **kwargs_no_md)
        return result

    def get_me(self) -> Optional[dict]:
        """Информация о боте — для healthcheck"""
        try:
            r = requests.get(f"{self.api_base}/getMe", timeout=10)
            return r.json() if r.ok else None
        except Exception as e:
            logger.error(f"getMe failed: {e}")
            return None
