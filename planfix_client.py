"""
Клиент PlanFix REST API v2
Документация: https://planfix.com/help/REST_API
"""
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class PlanfixAPIError(Exception):
    """Ошибка PlanFix API"""
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class PlanfixClient:
    """Клиент PlanFix REST API с throttle (1 req/sec) и пагинацией"""

    def __init__(self, account: str, token: str):
        self.base = f"https://{account}.planfix.com/rest"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._last_request: float = 0

    def _throttle(self):
        """Соблюдение лимита 1 req/sec"""
        elapsed = time.time() - self._last_request
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        self._last_request = time.time()

    def get(self, path: str, params: dict = None) -> dict:
        self._throttle()
        resp = requests.get(f"{self.base}{path}", headers=self.headers, params=params)
        self._check(resp)
        return resp.json()

    def post(self, path: str, body: dict = None, silent: bool = False) -> dict:
        self._throttle()
        url = f"{self.base}{path}"
        if silent:
            url += "?silent=true"
        resp = requests.post(url, headers=self.headers, json=body or {})
        self._check(resp)
        return resp.json()

    def delete(self, path: str) -> dict:
        self._throttle()
        resp = requests.delete(f"{self.base}{path}", headers=self.headers)
        self._check(resp)
        return resp.json()

    def _check(self, resp: requests.Response):
        if not resp.ok:
            raise PlanfixAPIError(
                f"PlanFix API {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

    def list_all(self, path: str, body: dict = None, key: str = None, page_size: int = 100) -> list:
        """Получить все записи с автопагинацией"""
        key_map = {
            "/task/list": "tasks",
            "/employee/list": "employees",
            "/project/list": "projects",
        }
        resp_key = key or key_map.get(path, "items")
        results, offset = [], 0
        while True:
            data = self.post(path, {**(body or {}), "offset": offset, "pageSize": page_size})
            items = data.get(resp_key, [])
            results.extend(items)
            if len(items) < page_size:
                break
            offset += page_size
        return results

    # ─── Вспомогательные методы для задач ────────────────────────────────────

    def get_task(self, task_id: int) -> dict:
        """Получить задачу с полными данными"""
        result = self.get(f"/task/{task_id}")
        return result.get("task", result)

    def update_task(self, task_id: int, data: dict) -> dict:
        """Обновить задачу"""
        return self.post(f"/task/{task_id}", data)

    def add_comment(self, task_id: int, text: str, silent: bool = False) -> dict:
        """Добавить комментарий к задаче"""
        return self.post(
            f"/task/{task_id}/comments",
            {"description": text},
            silent=silent,
        )

    def set_custom_fields(self, task_id: int, fields: list) -> dict:
        """
        Установить кастомные поля задачи.
        fields = [{"field": {"id": N}, "value": "..."}]
        """
        return self.post(f"/task/{task_id}", {"customFieldData": fields})

    def get_custom_field_value(self, task: dict, field_id: int) -> Optional[str]:
        """Получить значение кастомного поля из задачи"""
        for f in task.get("customFieldData", []):
            if f.get("field", {}).get("id") == field_id:
                return f.get("value")
        return None

    def create_subtask(
        self,
        parent_task_id: int,
        name: str,
        description: str = "",
        assignee_id=None,
        status_id: int = None,
        object_id: int = None,
    ) -> dict:
        """
        Создать подзадачу. PlanFix ждёт ID в формате "user:N" — приводим автоматически.

        Args:
          assignee_id: int или строка вида "user:N" — исполнитель подзадачи
          object_id: ID шаблона/объекта (например, ZOOM-RSVP с кнопками Буду/Не буду)
        """
        body: dict = {
            "name": name,
            "parent": {"id": parent_task_id},
        }
        if description:
            body["description"] = description
        if assignee_id:
            # PlanFix принимает оба формата при чтении, но при создании надёжнее "user:N"
            if isinstance(assignee_id, int):
                assignee_str = f"user:{assignee_id}"
            else:
                # Если строка — добавляем префикс если его нет
                s = str(assignee_id)
                assignee_str = s if s.startswith("user:") else f"user:{s}"
            body["assignees"] = {"users": [{"id": assignee_str}]}
        if status_id:
            body["status"] = {"id": status_id}
        if object_id:
            body["object"] = {"id": object_id}
            body["template"] = {"id": object_id}
        return self.post("/task", body)

    def get_subtasks(self, parent_task_id: int) -> list:
        """Получить все подзадачи задачи"""
        result = self.post("/task/list", {
            "filters": [
                {"type": "parent", "operator": "equal", "value": {"id": parent_task_id}}
            ],
            "fields": "id,name,assignees,status",
            "offset": 0,
            "pageSize": 100,
        })
        return result.get("tasks", [])

    def get_task_participants(self, task: dict) -> list:
        """
        Извлечь всех участников задачи:
        - assignees (исполнители)
        - owner (постановщик)
        - auditors (аудиторы/наблюдатели)
        Возвращает список уникальных {"id": N, "name": "..."}
        """
        seen = set()
        participants = []

        def add_user(u: dict):
            uid = u.get("id")
            if uid and uid not in seen:
                seen.add(uid)
                participants.append(u)

        for user in task.get("assignees", {}).get("users", []):
            add_user(user)
        for group in task.get("assignees", {}).get("groups", []):
            # Для групп нужно отдельно получить состав — пропускаем в базовой версии
            pass

        owner = task.get("owner") or task.get("responsible")
        if isinstance(owner, dict) and owner.get("id"):
            add_user(owner)

        for user in task.get("auditors", {}).get("users", []):
            add_user(user)

        # Поле "members" — явный список участников, если он задан
        for user in task.get("members", {}).get("users", []):
            add_user(user)

        return participants

    def get_statuses(self) -> list:
        """Получить все статусы задач аккаунта (для настройки)"""
        return self.get("/task/status/list").get("statuses", [])

    def get_custom_fields(self) -> list:
        """Получить все кастомные поля (для настройки)"""
        return self.get("/field/list").get("fields", [])

    def get_templates(self) -> list:
        """Получить все шаблоны задач (для настройки) — старый endpoint"""
        return self.post("/task/template/list", {}).get("templates", [])

    def get_objects(self, page_size: int = 100) -> list:
        """
        Получить все объекты-шаблоны PlanFix (это те, что в UI «Объекты» при создании задачи).
        В админке доступны: /account/processes
        """
        return self.post("/object/list", {"offset": 0, "pageSize": page_size}).get("objects", [])

    def get_employees(self) -> list:
        """Получить список сотрудников"""
        return self.list_all("/employee/list", {}, key="employees")
