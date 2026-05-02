"""
Глубокая инспекция PlanFix — извлекает структуру данных из реальных задач.
Не модифицирует ничего.
"""
import json
import os
import requests
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()
ACCOUNT = os.environ["PLANFIX_ACCOUNT"]
TOKEN = os.environ["PLANFIX_TOKEN"]
BASE = f"https://{ACCOUNT}.planfix.com/rest"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def post(path, body=None):
    return requests.post(f"{BASE}{path}", headers=H, json=body or {})


def get(path):
    return requests.get(f"{BASE}{path}", headers=H)


print(f"\n=== PlanFix Explorer: {ACCOUNT}.planfix.com ===\n")

# 1. Получаем 10 последних задач со всеми возможными полями
print("[1] Загружаю последние задачи...")
r = post("/task/list", {
    "offset": 0,
    "pageSize": 10,
    "fields": "id,name,description,status,template,project,assignees,owner,priority,dateBegin,dateEnd,startDateTime,endDateTime,duration,customFieldData",
})

if r.status_code != 200:
    print(f"  ERROR: HTTP {r.status_code}: {r.text[:300]}")
else:
    data = r.json()
    tasks = data.get("tasks", [])
    print(f"  Получено задач: {len(tasks)}")

    # Извлекаем уникальные статусы и шаблоны из задач
    statuses_seen = {}
    templates_seen = {}
    custom_fields_seen = {}
    projects_seen = {}

    for t in tasks:
        s = t.get("status")
        if isinstance(s, dict):
            sid = s.get("id")
            if sid not in statuses_seen:
                statuses_seen[sid] = s.get("name", "?")

        tpl = t.get("template")
        if isinstance(tpl, dict):
            tid = tpl.get("id")
            if tid and tid not in templates_seen:
                templates_seen[tid] = tpl.get("name", "?")

        proj = t.get("project")
        if isinstance(proj, dict):
            pid = proj.get("id")
            if pid and pid not in projects_seen:
                projects_seen[pid] = proj.get("name", "?")

        for f in t.get("customFieldData", []) or []:
            field = f.get("field", {})
            fid = field.get("id")
            if fid and fid not in custom_fields_seen:
                custom_fields_seen[fid] = {
                    "name": field.get("name", "?"),
                    "type": field.get("type", "?"),
                }

    print(f"\n  -> Статусов в задачах: {len(statuses_seen)}")
    for sid, name in sorted(statuses_seen.items()):
        print(f"      ID={sid}: {name}")

    print(f"\n  -> Шаблонов в задачах: {len(templates_seen)}")
    for tid, name in sorted(templates_seen.items()):
        print(f"      ID={tid}: {name}")

    print(f"\n  -> Проектов в задачах: {len(projects_seen)}")
    for pid, name in sorted(projects_seen.items()):
        print(f"      ID={pid}: {name}")

    print(f"\n  -> Кастомные поля в задачах: {len(custom_fields_seen)}")
    for fid, info in sorted(custom_fields_seen.items()):
        print(f"      ID={fid}: '{info['name']}' [type={info['type']}]")

# 2. Полное содержимое первой задачи (для понимания структуры)
if tasks:
    first_task_id = tasks[0]["id"]
    print(f"\n[2] Полная структура задачи #{first_task_id}:")
    r2 = get(f"/task/{first_task_id}")
    if r2.status_code == 200:
        data = r2.json()
        # Показываем структуру (первые 2000 символов)
        s = json.dumps(data, ensure_ascii=False, indent=2)
        print(s[:3000])
        if len(s) > 3000:
            print(f"\n   ... (ещё {len(s) - 3000} символов)")

# 3. Проверяем альтернативные endpoints
print(f"\n[3] Проверка альтернативных эндпоинтов:")
test_endpoints = [
    ("GET", "/processstatus/list"),
    ("GET", "/process/list"),
    ("GET", "/customfield/list"),
    ("POST", "/customfield/list"),
    ("POST", "/tasktemplate/list"),
    ("POST", "/template/list"),
    ("GET", "/account"),
    ("GET", "/account/info"),
    ("GET", "/user/list"),
    ("POST", "/user/list"),
]
for method, ep in test_endpoints:
    r = post(ep, {"offset": 0, "pageSize": 1}) if method == "POST" else get(ep)
    icon = "OK" if r.status_code == 200 else "--"
    note = ""
    if r.status_code == 200:
        try:
            keys = list(r.json().keys())[:5]
            note = f" keys={keys}"
        except:
            pass
    print(f"   [{icon}] {method:5} {ep:30} HTTP {r.status_code}{note}")

print("\n=== Готово ===\n")
