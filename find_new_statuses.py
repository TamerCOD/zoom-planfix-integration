"""
Поиск новых статусов в PlanFix через инспекцию свежих задач.
Создаёт временную задачу под каждый возможный статус, чтобы вытянуть его ID.

Альтернатива: попросить пользователя создать тестовую задачу в каждом статусе.
Этот скрипт ищет статусы через попытку обновления задачи.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()
ACCOUNT = os.environ["PLANFIX_ACCOUNT"]
TOKEN = os.environ["PLANFIX_TOKEN"]
BASE = f"https://{ACCOUNT}.planfix.com/rest"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def post(path, body=None):
    return requests.post(f"{BASE}{path}", headers=H, json=body or {})


# Способ 1: получить статусы из ВСЕХ задач (не только последних)
print("Сканирую все задачи на предмет статусов...")

statuses = {}
offset = 0
while True:
    r = post("/task/list", {
        "offset": offset,
        "pageSize": 100,
        "fields": "id,status",
    })
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        break
    tasks = r.json().get("tasks", [])
    if not tasks:
        break
    for t in tasks:
        s = t.get("status")
        if isinstance(s, dict):
            sid = s.get("id")
            if sid and sid not in statuses:
                statuses[sid] = s.get("name", "?")
    if len(tasks) < 100:
        break
    offset += 100

print(f"\nНайдено уникальных статусов в задачах: {len(statuses)}")
for sid, name in sorted(statuses.items()):
    print(f"  ID={sid}: {name}")

# Способ 2: попытка установить произвольные ID статусов (валидация)
print("\nПроверяю существование статусов с ID 1-100 через тестовую попытку...")
print("(это безопасно — мы НЕ применяем изменения, только получаем ошибки)\n")

# Берём любую задачу для теста
r = post("/task/list", {"offset": 0, "pageSize": 1, "fields": "id"})
if r.status_code == 200 and r.json().get("tasks"):
    test_id = r.json()["tasks"][0]["id"]
    found_ids = []

    # Проверяем диапазон через ошибки API
    # PlanFix возвращает разные ошибки для несуществующего и существующего статуса
    import time
    for sid in range(1, 50):
        # Используем silent=true чтобы не плодить уведомления
        # И сразу откатываем (но проще просто проверить через get)
        # Альтернатива: попробуем получить task с фильтром по статусу
        rr = post("/task/list", {
            "offset": 0,
            "pageSize": 1,
            "fields": "id,status",
            "filters": [{"type": 51, "operator": "equal", "value": str(sid)}]
        })
        # Если статус существует - запрос пройдёт (даже если задач нет)
        if rr.status_code == 200:
            tasks_with = rr.json().get("tasks", [])
            if tasks_with:
                s = tasks_with[0].get("status", {})
                if s.get("id") == sid:
                    found_ids.append((sid, s.get("name")))
                    print(f"  ID={sid:3}: {s.get('name')} (есть задачи)")
        time.sleep(0.1)

    print(f"\nВсего найдено по фильтру: {len(found_ids)}")
