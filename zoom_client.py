"""
Клиент Zoom API v2 — Server-to-Server OAuth
Документация: https://developers.zoom.us/docs/api/
"""
import base64
import logging
import time
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class ZoomAPIError(Exception):
    """Ошибка Zoom API"""
    def __init__(self, message: str, status_code: int = 0, response: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}


class ZoomClient:
    """
    Клиент Zoom API с автоматическим обновлением токена.
    Использует Server-to-Server OAuth (рекомендуемый метод Zoom).

    Настройка:
      1. Перейти на https://marketplace.zoom.us/develop/create
      2. Создать приложение типа "Server-to-Server OAuth"
      3. Добавить scopes: meeting:write:admin, meeting:read:admin, meeting:delete:admin
      4. Активировать приложение
      5. Скопировать Account ID, Client ID, Client Secret
    """
    BASE_URL = "https://api.zoom.us/v2"
    TOKEN_URL = "https://zoom.us/oauth/token"

    def __init__(self, account_id: str, client_id: str, client_secret: str):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._token_expires: float = 0

    # ─── Аутентификация ───────────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        """Получить/обновить access token"""
        # Проверяем кэшированный токен (с запасом 60 сек)
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        resp = requests.post(
            self.TOKEN_URL,
            params={
                "grant_type": "account_credentials",
                "account_id": self.account_id,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        if resp.status_code != 200:
            raise ZoomAPIError(
                f"Не удалось получить токен Zoom: {resp.text}",
                status_code=resp.status_code,
            )

        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600)
        logger.debug("Zoom token refreshed")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[dict]:
        """Универсальный запрос к Zoom API"""
        url = f"{self.BASE_URL}{endpoint}"
        resp = requests.request(method, url, headers=self._headers(), **kwargs)

        # 204 No Content — успех без тела
        if resp.status_code == 204:
            return None

        # 404 — не найдено (встреча удалена)
        if resp.status_code == 404:
            return None

        if not resp.ok:
            try:
                err = resp.json()
            except Exception:
                err = {"message": resp.text}
            raise ZoomAPIError(
                f"Zoom API ошибка {resp.status_code}: {err.get('message', resp.text)}",
                status_code=resp.status_code,
                response=err,
            )

        if resp.content:
            return resp.json()
        return None

    # ─── Встречи (Meetings) ───────────────────────────────────────────────────

    def create_meeting(
        self,
        topic: str,
        start_time_iso: str,
        duration_min: int = 60,
        timezone: str = "Europe/Moscow",
        agenda: str = "",
    ) -> dict:
        """
        Создать запланированную встречу Zoom.

        Args:
            topic: Тема встречи (берётся из названия задачи PlanFix)
            start_time_iso: Время начала в формате ISO 8601, например "2025-06-15T10:00:00"
            duration_min: Длительность в минутах
            timezone: Часовой пояс (по умолчанию Москва)
            agenda: Описание / повестка встречи

        Returns:
            dict с полями: id, join_url, password, uuid, start_time и др.
        """
        body = {
            "topic": topic,
            "type": 2,                    # 2 = Scheduled meeting
            "start_time": start_time_iso,
            "duration": duration_min,
            "timezone": timezone,
            "agenda": agenda,
            "settings": {
                "host_video": True,
                "participant_video": True,
                "join_before_host": False,
                "waiting_room": True,      # Зал ожидания — безопаснее
                "mute_upon_entry": True,
                "auto_recording": "none",
                "meeting_authentication": False,
                "allow_multiple_devices": True,
            },
        }

        result = self._request("POST", "/users/me/meetings", json=body)
        logger.info(
            f"Zoom meeting created: ID={result.get('id')}, "
            f"topic='{topic}', start={start_time_iso}"
        )
        return result

    def get_meeting(self, meeting_id: str) -> Optional[dict]:
        """Получить данные встречи"""
        return self._request("GET", f"/meetings/{meeting_id}")

    def update_meeting(
        self,
        meeting_id: str,
        topic: str = None,
        start_time_iso: str = None,
        duration_min: int = None,
        timezone: str = None,
        agenda: str = None,
    ) -> bool:
        """Обновить данные встречи (например, после изменения времени в PlanFix)"""
        body = {}
        if topic is not None:
            body["topic"] = topic
        if start_time_iso is not None:
            body["start_time"] = start_time_iso
        if duration_min is not None:
            body["duration"] = duration_min
        if timezone is not None:
            body["timezone"] = timezone
        if agenda is not None:
            body["agenda"] = agenda

        if not body:
            return True  # Нечего обновлять

        self._request("PATCH", f"/meetings/{meeting_id}", json=body)
        logger.info(f"Zoom meeting {meeting_id} updated: {body}")
        return True

    def delete_meeting(self, meeting_id: str) -> bool:
        """
        Удалить/отменить встречу.
        Возвращает True даже если встреча уже не существует (404).
        """
        self._request("DELETE", f"/meetings/{meeting_id}")
        logger.info(f"Zoom meeting {meeting_id} deleted")
        return True

    def list_meetings(self, meeting_type: str = "scheduled") -> list:
        """Список встреч текущего пользователя"""
        result = self._request("GET", f"/users/me/meetings", params={"type": meeting_type})
        return result.get("meetings", []) if result else []
