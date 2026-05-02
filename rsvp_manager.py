"""
Менеджер RSVP-подзадач — чеклист участников конференции.

Концепция:
  Для каждого участника задачи автоматически создаётся подзадача:
    "✋ Иван Иванов — подтвердите участие"

  Подзадача назначена только этому участнику, что обеспечивает:
  - Каждый видит только свою подзадачу (исполнитель)
  - Каждый меняет статус только своей задачи
  - Остальные могут только просматривать

  Статусы RSVP-подзадачи:
    ⏳ Ожидаем ответа  → начальный
    ✅ Буду            → участник подтвердил
    ❌ Не буду         → участник отказался
"""
import logging
from typing import Optional

from planfix_client import PlanfixClient
from config import Config

logger = logging.getLogger(__name__)


RSVP_MARKER = "🎯RSVP:"   # Префикс для идентификации RSVP-подзадач


def get_rsvp_subtask_name(user_name: str) -> str:
    return f"{RSVP_MARKER} {user_name} — подтвердите участие"


def is_rsvp_subtask(task: dict) -> bool:
    return RSVP_MARKER in task.get("name", "")


def extract_user_from_rsvp(subtask_name: str) -> str:
    """Извлечь имя пользователя из названия RSVP-подзадачи"""
    if RSVP_MARKER in subtask_name:
        return subtask_name.split(RSVP_MARKER)[-1].replace("— подтвердите участие", "").strip()
    return subtask_name


class RSVPManager:
    """Управляет RSVP-подзадачами для конференций"""

    def __init__(self, pf: PlanfixClient, cfg: Config):
        self.pf = pf
        self.cfg = cfg

    def sync_rsvp_for_task(self, task_id: int, task: dict) -> dict:
        """
        Синхронизировать RSVP-подзадачи с текущим списком участников.

        - Создаёт новые подзадачи для новых участников
        - Не удаляет существующие (чтобы сохранить уже данные ответы)

        Returns: {"created": [...], "existing": [...], "total": N}
        """
        participants = self.pf.get_task_participants(task)
        if not participants:
            logger.info(f"Task {task_id}: нет участников для RSVP")
            return {"created": [], "existing": [], "total": 0}

        # Получаем существующие RSVP-подзадачи
        existing_subtasks = self.pf.get_subtasks(task_id)
        existing_rsvp = {
            st["assignees"]["users"][0]["id"]: st
            for st in existing_subtasks
            if is_rsvp_subtask(st)
            and st.get("assignees", {}).get("users")
        }

        created = []
        existing = []

        for user in participants:
            uid = user.get("id")
            uname = user.get("name", f"Участник {uid}")

            if uid in existing_rsvp:
                existing.append(uname)
                logger.debug(f"RSVP подзадача уже есть для {uname} (task {task_id})")
                continue

            # Создаём RSVP-подзадачу
            try:
                subtask = self.pf.create_subtask(
                    parent_task_id=task_id,
                    name=get_rsvp_subtask_name(uname),
                    description=self._rsvp_description(uname),
                    assignee_id=uid,
                    status_id=self.cfg.rsvp_status_waiting or None,
                )
                created.append(uname)
                logger.info(f"Создана RSVP-подзадача для {uname} в задаче {task_id}")
            except Exception as e:
                logger.error(f"Ошибка создания RSVP для {uname}: {e}")

        return {
            "created": created,
            "existing": existing,
            "total": len(participants),
        }

    def get_rsvp_summary(self, task_id: int) -> dict:
        """
        Получить сводку RSVP по всем участникам.

        Returns: {
            "attending": ["Имя 1", ...],      # ✅ Буду
            "not_attending": ["Имя 2", ...],  # ❌ Не буду
            "pending": ["Имя 3", ...],        # ⏳ Ожидаем
        }
        """
        subtasks = self.pf.get_subtasks(task_id)
        result = {"attending": [], "not_attending": [], "pending": []}

        for st in subtasks:
            if not is_rsvp_subtask(st):
                continue
            name = extract_user_from_rsvp(st.get("name", ""))
            status_id = st.get("status", {}).get("id")

            if self.cfg.rsvp_status_yes and status_id == self.cfg.rsvp_status_yes:
                result["attending"].append(name)
            elif self.cfg.rsvp_status_no and status_id == self.cfg.rsvp_status_no:
                result["not_attending"].append(name)
            else:
                result["pending"].append(name)

        return result

    def format_rsvp_comment(self, task_id: int) -> str:
        """Сформировать текст комментария со сводкой RSVP"""
        summary = self.get_rsvp_summary(task_id)
        lines = ["📊 **Сводка участия:**\n"]

        if summary["attending"]:
            lines.append("✅ **Буду:**")
            for name in summary["attending"]:
                lines.append(f"  • {name}")

        if summary["not_attending"]:
            lines.append("\n❌ **Не буду:**")
            for name in summary["not_attending"]:
                lines.append(f"  • {name}")

        if summary["pending"]:
            lines.append("\n⏳ **Ожидаем ответа:**")
            for name in summary["pending"]:
                lines.append(f"  • {name}")

        total = len(summary["attending"]) + len(summary["not_attending"]) + len(summary["pending"])
        lines.append(f"\n_Всего участников: {total}_")
        return "\n".join(lines)

    @staticmethod
    def _rsvp_description(user_name: str) -> str:
        return (
            f"👋 {user_name}, пожалуйста подтвердите участие в конференции.\n\n"
            f"Измените статус этой задачи:\n"
            f"  ✅ **БУДУ** — если планируете присутствовать\n"
            f"  ❌ **НЕ БУДУ** — если не сможете\n\n"
            f"_Только вы можете изменить эту задачу_"
        )
