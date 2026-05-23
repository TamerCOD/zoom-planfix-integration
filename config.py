"""
Конфигурация интеграции PlanFix ↔ Zoom
Все значения берутся из переменных окружения (.env файл)
"""
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ─── PlanFix ───────────────────────────────────────────────────────────────
    planfix_account: str = ""          # subdomain: mycompany.planfix.com
    planfix_token: str = ""            # Bearer-токен робота

    # ID шаблона задачи "Конференция ZOOM" (узнать через API или URL в браузере)
    planfix_zoom_template_id: int = 0

    # ID объекта "ZOOM-RSVP" — используется для RSVP-подзадач (с кнопками Буду/Не буду)
    planfix_rsvp_template_id: int = 0

    # ID проекта, где создаются конференции (если нужна фильтрация по проекту)
    planfix_zoom_project_id: int = 0

    # ─── Статусы задачи (получить ID через GET /task/list → status.id) ────────
    status_new: int = 0           # Новая
    status_active: int = 0        # Актуальная
    status_completed: int = 0     # Завершённая
    status_cancelled: int = 0     # Отменённая

    # ─── Статусы RSVP-подзадач ────────────────────────────────────────────────
    rsvp_status_waiting: int = 0   # Ожидаем ответа
    rsvp_status_yes: int = 0       # Буду
    rsvp_status_no: int = 0        # Не буду

    # ─── ID кастомных полей задачи (создать в Управление → Поля) ─────────────
    field_zoom_meeting_id: int = 0    # Тип: строка  — "Zoom Meeting ID"
    field_zoom_join_url: int = 0      # Тип: URL     — "Zoom Ссылка"
    field_zoom_password: int = 0      # Тип: строка  — "Zoom Пароль"
    field_zoom_uuid: int = 0          # Тип: строка  — "Zoom UUID" (скрытое)

    # ─── Zoom API (Server-to-Server OAuth) ────────────────────────────────────
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    zoom_default_duration_min: int = 60    # Длительность по умолчанию
    zoom_timezone: str = "Europe/Moscow"

    # ─── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""        # Bot Token из BotFather
    telegram_chat_id: str = ""          # ID группы/чата для уведомлений

    # ─── Webhook-сервер ───────────────────────────────────────────────────────
    webhook_secret: str = ""
    server_port: int = 8000
    log_level: str = "INFO"

    def __post_init__(self):
        """Загрузить значения из переменных окружения"""
        self.planfix_account = os.environ.get("PLANFIX_ACCOUNT", self.planfix_account)
        self.planfix_token = os.environ.get("PLANFIX_TOKEN", self.planfix_token)
        self.planfix_zoom_template_id = int(os.environ.get("PLANFIX_ZOOM_TEMPLATE_ID", self.planfix_zoom_template_id))
        self.planfix_rsvp_template_id = int(os.environ.get("PLANFIX_RSVP_TEMPLATE_ID", self.planfix_rsvp_template_id))
        self.planfix_zoom_project_id = int(os.environ.get("PLANFIX_ZOOM_PROJECT_ID", self.planfix_zoom_project_id))

        self.status_new = int(os.environ.get("STATUS_NEW", self.status_new))
        self.status_active = int(os.environ.get("STATUS_ACTIVE", self.status_active))
        self.status_completed = int(os.environ.get("STATUS_COMPLETED", self.status_completed))
        self.status_cancelled = int(os.environ.get("STATUS_CANCELLED", self.status_cancelled))

        self.rsvp_status_waiting = int(os.environ.get("RSVP_STATUS_WAITING", self.rsvp_status_waiting))
        self.rsvp_status_yes = int(os.environ.get("RSVP_STATUS_YES", self.rsvp_status_yes))
        self.rsvp_status_no = int(os.environ.get("RSVP_STATUS_NO", self.rsvp_status_no))

        self.field_zoom_meeting_id = int(os.environ.get("FIELD_ZOOM_MEETING_ID", self.field_zoom_meeting_id))
        self.field_zoom_join_url = int(os.environ.get("FIELD_ZOOM_JOIN_URL", self.field_zoom_join_url))
        self.field_zoom_password = int(os.environ.get("FIELD_ZOOM_PASSWORD", self.field_zoom_password))
        self.field_zoom_uuid = int(os.environ.get("FIELD_ZOOM_UUID", self.field_zoom_uuid))

        self.zoom_account_id = os.environ.get("ZOOM_ACCOUNT_ID", self.zoom_account_id)
        self.zoom_client_id = os.environ.get("ZOOM_CLIENT_ID", self.zoom_client_id)
        self.zoom_client_secret = os.environ.get("ZOOM_CLIENT_SECRET", self.zoom_client_secret)
        self.zoom_default_duration_min = int(os.environ.get("ZOOM_DEFAULT_DURATION_MIN", self.zoom_default_duration_min))
        self.zoom_timezone = os.environ.get("ZOOM_TIMEZONE", self.zoom_timezone)

        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", self.telegram_bot_token)
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", self.telegram_chat_id)

        self.webhook_secret = os.environ.get("WEBHOOK_SECRET", self.webhook_secret)
        self.server_port = int(os.environ.get("SERVER_PORT", self.server_port))
        self.log_level = os.environ.get("LOG_LEVEL", self.log_level)

    def validate(self):
        """Проверить обязательные настройки"""
        errors = []
        if not self.planfix_account:
            errors.append("PLANFIX_ACCOUNT не задан")
        if not self.planfix_token:
            errors.append("PLANFIX_TOKEN не задан")
        if not self.zoom_account_id:
            errors.append("ZOOM_ACCOUNT_ID не задан")
        if not self.zoom_client_id:
            errors.append("ZOOM_CLIENT_ID не задан")
        if not self.zoom_client_secret:
            errors.append("ZOOM_CLIENT_SECRET не задан")
        if errors:
            raise ValueError("Ошибки конфигурации:\n" + "\n".join(f"  - {e}" for e in errors))
        return True


# Глобальный экземпляр конфигурации
config = Config()
