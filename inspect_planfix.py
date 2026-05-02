"""
Инспекция аккаунта PlanFix — БЕЗ изменения существующих данных.
Только читает текущее состояние для планирования.
"""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

ACCOUNT = os.environ["PLANFIX_ACCOUNT"]
TOKEN = os.environ["PLANFIX_TOKEN"]
BASE = f"https://{ACCOUNT}.planfix.com/rest"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def show(title, fn):
    print(f"\n{'━'*60}\n  {title}\n{'━'*60}")
    try:
        fn()
    except Exception as e:
        print(f"  ❌ {e}")


def diagnostic():
    """Базовая диагностика подключения"""
    print(f"\n🔍 PlanFix API Inspector → {ACCOUNT}.planfix.com")
    tests = [
        ("Задачи", "/task/list", "POST", {"offset": 0, "pageSize": 1}),
        ("Контакты", "/contact/list", "POST", {"offset": 0, "pageSize": 1}),
        ("Проекты", "/project/list", "POST", {"offset": 0, "pageSize": 1}),
        ("Сотрудники", "/employee/list", "POST", {"offset": 0, "pageSize": 1}),
    ]
    print("\n  ✅ = доступно, ❌ = нет прав\n")
    for name, ep, method, body in tests:
        r = requests.post(f"{BASE}{ep}", headers=H, json=body) if method == "POST" \
            else requests.get(f"{BASE}{ep}", headers=H)
        icon = "✅" if r.status_code == 200 else "❌"
        print(f"  {icon}  {name:<15} HTTP {r.status_code}")


def list_statuses():
    """Список текущих статусов задач"""
    r = requests.get(f"{BASE}/task/status/list", headers=H)
    if r.status_code == 200:
        data = r.json()
        statuses = data.get("statuses", []) or data.get("statusList", [])
        if not statuses:
            print(f"  Сырой ответ: {data}")
        for s in statuses:
            sid = s.get("id")
            name = s.get("name", "")
            stype = s.get("type", "?")
            print(f"  ID={sid:5}  {name}  [type={stype}]")
    else:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")


def list_custom_fields():
    """Список кастомных полей"""
    # Пробуем разные endpoint'ы
    for endpoint in ["/field/list", "/customField/list"]:
        r = requests.get(f"{BASE}{endpoint}", headers=H)
        if r.status_code == 200:
            data = r.json()
            fields = data.get("fields", []) or data.get("customFields", [])
            print(f"  (endpoint: {endpoint})")
            for f in fields[:50]:
                fid = f.get("id")
                name = f.get("name", "")
                ftype = f.get("type", "?")
                target = f.get("objectType", f.get("targetType", "?"))
                print(f"  ID={fid:5}  [{target:10}] [{ftype:12}]  {name}")
            return
    print(f"  ❌ Не найден endpoint полей")


def list_templates():
    """Список шаблонов задач"""
    r = requests.post(f"{BASE}/task/template/list", headers=H, json={"offset": 0, "pageSize": 100})
    if r.status_code == 200:
        templates = r.json().get("templates", [])
        if not templates:
            print(f"  Шаблонов задач не найдено")
        for t in templates:
            print(f"  ID={t.get('id'):5}  {t.get('name')}")
    else:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")


def list_projects():
    """Список проектов"""
    r = requests.post(f"{BASE}/project/list", headers=H, json={"offset": 0, "pageSize": 30})
    if r.status_code == 200:
        for p in r.json().get("projects", []):
            print(f"  ID={p.get('id'):5}  {p.get('name')}")


def list_employees():
    """Список сотрудников / роботов"""
    r = requests.post(f"{BASE}/employee/list", headers=H, json={"offset": 0, "pageSize": 50})
    if r.status_code == 200:
        for e in r.json().get("employees", []):
            robot = " 🤖" if e.get("isRobot") else ""
            print(f"  ID={e.get('id'):5}  {e.get('name')}{robot}")


if __name__ == "__main__":
    diagnostic()
    show("📋 СТАТУСЫ ЗАДАЧ (текущие)", list_statuses)
    show("🔧 КАСТОМНЫЕ ПОЛЯ (текущие)", list_custom_fields)
    show("📄 ШАБЛОНЫ ЗАДАЧ (текущие)", list_templates)
    show("📁 ПРОЕКТЫ", list_projects)
    show("👥 СОТРУДНИКИ", list_employees)
    print(f"\n{'━'*60}\n  Диагностика завершена. Ничего не было изменено.\n{'━'*60}\n")
