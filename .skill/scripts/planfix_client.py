"""
PlanFix REST API client — battle-tested, with the gotchas baked in.

Key design decisions (learned the hard way):
  - 1 req/sec throttle (PlanFix enforces this)
  - Auto user:N format for assignees/participants
  - Explicit `fields` parameter on every read
  - Tail-based pagination (newest tasks are at the END, no sort works)
  - State-hash change detection (since date-modified filter doesn't work)
"""
import json
import logging
import time
from typing import Optional, Union

import requests

logger = logging.getLogger(__name__)


class PlanfixError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def to_pf_user_id(uid: Union[int, str]) -> str:
    """Convert any user ID to PlanFix `user:N` string format"""
    if isinstance(uid, int):
        return f"user:{uid}"
    s = str(uid)
    return s if s.startswith("user:") else f"user:{s}"


def parse_user_id(user_obj: dict) -> Optional[int]:
    """Parse PlanFix user object → plain integer ID"""
    if not user_obj:
        return None
    raw = user_obj.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
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


class PlanfixClient:
    """Throttled REST client with auto-retry on 504/429."""

    def __init__(self, account: str, token: str, throttle_s: float = 1.05):
        self.base = f"https://{account}.planfix.com/rest"
        self.h = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.throttle_s = throttle_s
        self._last_request = 0.0
        self.account = account

    # ────────────────────────── transport ──────────────────────────

    def _throttle(self):
        wait = self.throttle_s - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _check(self, r: requests.Response) -> dict:
        if r.status_code in (429, 504):
            # Brief pause and retry once
            time.sleep(2)
            r = requests.request(r.request.method, r.request.url,
                                 headers=self.h, data=r.request.body)
        if not r.ok:
            raise PlanfixError(
                f"PlanFix {r.status_code}: {r.text[:300]}",
                status=r.status_code,
            )
        try:
            return r.json()
        except Exception:
            return {}

    def get(self, path: str, params: dict = None) -> dict:
        self._throttle()
        r = requests.get(f"{self.base}{path}", headers=self.h, params=params)
        return self._check(r)

    def post(self, path: str, body: dict = None, silent: bool = False) -> dict:
        self._throttle()
        url = f"{self.base}{path}"
        if silent:
            url += "?silent=true"
        r = requests.post(url, headers=self.h, json=body or {})
        return self._check(r)

    def delete(self, path: str) -> dict:
        """Note: PlanFix doesn't support DELETE for tasks — only for some resources"""
        self._throttle()
        r = requests.delete(f"{self.base}{path}", headers=self.h)
        return self._check(r)

    # ────────────────────────── tasks ──────────────────────────

    DEFAULT_TASK_FIELDS = (
        "id,name,description,status,project,assignees,owner,participants,"
        "members,auditors,startDateTime,endDateTime,object,template,parent,"
        "customFieldData,priority,duration"
    )

    def get_task(self, task_id: int, fields: str = None) -> dict:
        """Get task — ALWAYS specify fields, otherwise PlanFix returns minimal {id, name}"""
        params = {"fields": fields or self.DEFAULT_TASK_FIELDS}
        result = self.get(f"/task/{task_id}", params=params)
        return result.get("task", result)

    def update_task(self, task_id: int, body: dict) -> dict:
        """Update task. Common fields:
           - name, description (HTML!), status: {id}
           - assignees: {users: [{id: 'user:N'}]}
           - participants: {users: [{id: 'user:N'}]}
           - startDateTime: {datetime: 'DD-MM-YYYY HH:MM'}
           - customFieldData: [{field: {id: N}, value: ...}]
        """
        return self.post(f"/task/{task_id}", body)

    def create_task(
        self,
        name: str,
        *,
        description: str = "",
        object_id: int = None,
        assignees: list = None,        # list of int user IDs
        participants: list = None,     # list of int user IDs
        owner_id: int = None,
        project_id: int = None,
        start_dt: str = None,          # "DD-MM-YYYY HH:MM"
        end_dt: str = None,
        custom_fields: list = None,    # [{id, value}]
        parent_id: int = None,
    ) -> dict:
        body = {"name": name}
        if description:
            body["description"] = description
        if object_id:
            body["object"] = {"id": object_id}
            body["template"] = {"id": object_id}
        if parent_id:
            body["parent"] = {"id": parent_id}
        if assignees:
            body["assignees"] = {"users": [{"id": to_pf_user_id(u)} for u in assignees]}
        if participants:
            body["participants"] = {"users": [{"id": to_pf_user_id(u)} for u in participants]}
        if owner_id:
            body["owner"] = {"id": to_pf_user_id(owner_id)}
        if project_id:
            body["project"] = {"id": project_id}
        if start_dt:
            body["startDateTime"] = {"datetime": start_dt}
        if end_dt:
            body["endDateTime"] = {"datetime": end_dt}
        if custom_fields:
            body["customFieldData"] = [
                {"field": {"id": f["id"]}, "value": f["value"]} for f in custom_fields
            ]
        return self.post("/task", body)

    def add_comment(self, task_id: int, html_text: str, silent: bool = False) -> dict:
        """Add comment. USE HTML — \\n is stripped, markdown doesn't render"""
        return self.post(f"/task/{task_id}/comments", {"description": html_text}, silent=silent)

    def list_comments(self, task_id: int, page_size: int = 20) -> list:
        result = self.post(
            f"/task/{task_id}/comments/list",
            {"offset": 0, "pageSize": page_size,
             "fields": "id,description,owner,dateTime"},
        )
        return result.get("comments", [])

    def set_custom_fields(self, task_id: int, fields: list) -> dict:
        """fields = [{"field": {"id": N}, "value": ...}]"""
        return self.post(f"/task/{task_id}", {"customFieldData": fields})

    # ──────────────────── tail-based pagination ────────────────────

    def scan_tail(self, pages: int = 3, fields: str = None) -> list:
        """
        Get the LATEST N pages of tasks (newest IDs at the END of pagination).
        PlanFix returns tasks oldest-first regardless of sort param.
        """
        fields = fields or "id,name,description,status,assignees,participants,startDateTime,endDateTime,object,parent"
        all_tasks = []
        for offset in range(0, 10000, 100):  # safety cap
            r = self.post("/task/list", {
                "offset": offset, "pageSize": 100, "fields": fields,
            })
            items = r.get("tasks", [])
            if not items:
                break
            all_tasks.extend(items)
            if len(items) < 100:
                break
        # Return last N pages
        return all_tasks[-pages * 100:] if all_tasks else []

    def find_subtasks(self, parent_id: int, scan_pages: int = 5) -> list:
        """
        Find subtasks of a task. The parent filter doesn't work in /task/list,
        so we scan the tail and filter in Python.
        """
        recent = self.scan_tail(pages=scan_pages, fields="id,name,parent,assignees,status")
        return [
            t for t in recent
            if (t.get("parent") or {}).get("id") == parent_id
        ]

    # ────────────────────────── objects ──────────────────────────

    def list_objects(self) -> list:
        result = self.post("/object/list",
                           {"offset": 0, "pageSize": 100,
                            "fields": "id,name,description"})
        return result.get("objects", [])

    def find_object_by_name(self, name: str) -> Optional[dict]:
        for o in self.list_objects():
            if name.lower() in (o.get("name") or "").lower():
                return o
        return None

    def get_object_custom_fields(self, object_id: int) -> list:
        """
        Returns the custom fields attached to an object/template.
        This is the ONLY way to discover custom field IDs via API.
        """
        r = self.get(f"/object/{object_id}")
        obj = r.get("object", {})
        out = []
        seen_ids = set()
        for f in obj.get("customFieldData", []):
            field = f.get("field") or {}
            fid = field.get("id")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                out.append({
                    "id": fid,
                    "name": field.get("name"),
                    "type": field.get("type"),
                })
        return out

    # ────────────────────────── users ──────────────────────────

    def list_users(self) -> list:
        """Use /user/list — /employee/list returns 404"""
        result = self.post("/user/list",
                           {"offset": 0, "pageSize": 200,
                            "fields": "id,name,login,email,role"})
        return result.get("users", [])

    def find_user(self, name_substring: str) -> Optional[dict]:
        for u in self.list_users():
            if name_substring.lower() in (u.get("name") or "").lower():
                return u
        return None

    # ────────────────────────── participants ──────────────────────────

    @staticmethod
    def get_meeting_attendees(task: dict) -> list:
        """
        ⚠️ For meeting attendees, use `participants` field, NOT `assignees`.
        This is the #1 source of RSVP bugs.
        """
        result = list((task.get("participants") or {}).get("users", []) or [])
        if not result:
            # Fallback for old tasks
            result = list((task.get("assignees") or {}).get("users", []) or [])
        return result

    # ────────────────────────── diagnostics ──────────────────────────

    def healthcheck(self) -> dict:
        """Quick diagnostic — which endpoints work, which don't"""
        results = {}
        tests = [
            ("tasks", "POST", "/task/list", {"offset": 0, "pageSize": 1}),
            ("objects", "POST", "/object/list", {"offset": 0, "pageSize": 1}),
            ("users", "POST", "/user/list", {"offset": 0, "pageSize": 1}),
            ("projects", "POST", "/project/list", {"offset": 0, "pageSize": 1}),
            ("contacts", "POST", "/contact/list", {"offset": 0, "pageSize": 1}),
        ]
        for name, method, path, body in tests:
            try:
                if method == "POST":
                    self.post(path, body)
                else:
                    self.get(path)
                results[name] = "ok"
            except PlanfixError as e:
                results[name] = f"FAIL({e.status})"
        return results
