"""
Помощник по настройке — выводит все ID статусов, полей и шаблонов PlanFix.
Запускайте этот скрипт после создания статусов/полей/шаблонов в UI PlanFix
для получения нужных ID для .env файла.

Запуск:
  python setup_helper.py
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

ACCOUNT = os.environ.get("PLANFIX_ACCOUNT", "")
TOKEN = os.environ.get("PLANFIX_TOKEN", "")

if not ACCOUNT or not TOKEN:
    print("❌ Укажите PLANFIX_ACCOUNT и PLANFIX_TOKEN в .env файле")
    sys.exit(1)


def main():
    from planfix_client import PlanfixClient
    from zoom_client import ZoomClient

    pf = PlanfixClient(ACCOUNT, TOKEN)

    print(f"\n{'='*60}")
    print(f"  PlanFix Setup Helper — {ACCOUNT}.planfix.com")
    print(f"{'='*60}\n")

    # ── Статусы задач ─────────────────────────────────────────────────────────
    print("📋 СТАТУСЫ ЗАДАЧ:")
    print("-" * 40)
    try:
        statuses = pf.get_statuses()
        if statuses:
            for s in statuses:
                print(f"  ID={s.get('id'):5}  {s.get('name')}")
        else:
            print("  (нет статусов)")
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    print()

    # ── Кастомные поля ────────────────────────────────────────────────────────
    print("🔧 ПОЛЬЗОВАТЕЛЬСКИЕ ПОЛЯ ЗАДАЧ:")
    print("-" * 40)
    try:
        fields = pf.get_custom_fields()
        task_fields = [f for f in fields if f.get("targetType") in ("task", None)]
        if task_fields:
            for f in task_fields:
                print(f"  ID={f.get('id'):5}  [{f.get('type','?'):12}]  {f.get('name')}")
        else:
            print("  (нет кастомных полей)")
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    print()

    # ── Шаблоны задач ─────────────────────────────────────────────────────────
    print("📄 ШАБЛОНЫ ЗАДАЧ:")
    print("-" * 40)
    try:
        templates = pf.get_templates()
        if templates:
            for t in templates:
                print(f"  ID={t.get('id'):5}  {t.get('name')}")
        else:
            print("  (нет шаблонов)")
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    print()

    # ── Проекты ───────────────────────────────────────────────────────────────
    print("📁 ПРОЕКТЫ (первые 20):")
    print("-" * 40)
    try:
        projects = pf.post("/project/list", {"offset": 0, "pageSize": 20})
        for p in projects.get("projects", []):
            print(f"  ID={p.get('id'):5}  {p.get('name')}")
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    print()

    # ── Сотрудники ────────────────────────────────────────────────────────────
    print("👥 СОТРУДНИКИ / РОБОТЫ:")
    print("-" * 40)
    try:
        employees = pf.post("/employee/list", {"offset": 0, "pageSize": 50})
        for e in employees.get("employees", []):
            robot = " [🤖 Робот]" if e.get("isRobot") else ""
            print(f"  ID={e.get('id'):5}  {e.get('name')}{robot}")
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    print()

    # ── Проверка Zoom ─────────────────────────────────────────────────────────
    zoom_account = os.environ.get("ZOOM_ACCOUNT_ID", "")
    zoom_client = os.environ.get("ZOOM_CLIENT_ID", "")
    zoom_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")

    if zoom_account and zoom_client and zoom_secret:
        print("🎥 ZOOM API:")
        print("-" * 40)
        try:
            z = ZoomClient(zoom_account, zoom_client, zoom_secret)
            meetings = z.list_meetings()
            print(f"  ✅ Подключение успешно")
            print(f"  Запланировано встреч: {len(meetings)}")
        except Exception as e:
            print(f"  ❌ Ошибка Zoom: {e}")
        print()

    # ── Итоговые инструкции ───────────────────────────────────────────────────
    print("━" * 60)
    print("📝 ЗАПОЛНИТЕ .env ФАЙЛ:")
    print("━" * 60)
    print("""
Скопируйте нужные ID из вывода выше и заполните .env:

STATUS_NEW=          # ID статуса "Новая"
STATUS_ACTIVE=       # ID статуса "Актуальная"
STATUS_COMPLETED=    # ID статуса "Завершённая"
STATUS_CANCELLED=    # ID статуса "Отменённая"

RSVP_STATUS_WAITING= # ID статуса RSVP "Ожидаем ответа"
RSVP_STATUS_YES=     # ID статуса RSVP "Буду"
RSVP_STATUS_NO=      # ID статуса RSVP "Не буду"

FIELD_ZOOM_MEETING_ID= # ID поля "Zoom Meeting ID"
FIELD_ZOOM_JOIN_URL=   # ID поля "Zoom Ссылка"
FIELD_ZOOM_PASSWORD=   # ID поля "Zoom Пароль"
FIELD_ZOOM_UUID=       # ID поля "Zoom UUID"

PLANFIX_ZOOM_TEMPLATE_ID= # ID шаблона "Конференция ZOOM"
""")


if __name__ == "__main__":
    main()
