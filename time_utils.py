"""
Утилиты для работы со временем PlanFix ↔ Zoom
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Union

import pytz

logger = logging.getLogger(__name__)

# PlanFix хранит даты как Unix timestamp (integer) для datetime-полей
# или как строку "YYYY-MM-DD" для date-полей


def planfix_ts_to_datetime(ts: Union[int, float, str, None]) -> Optional[datetime]:
    """
    Преобразовать значение даты/времени из PlanFix в datetime.
    PlanFix использует Unix timestamp для полей datetime.
    """
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.UTC)
        # Строка ISO или просто дата
        s = str(ts).strip()
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
        # Только дата "YYYY-MM-DD" — используем полночь UTC
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
    except Exception as e:
        logger.warning(f"Не удалось разобрать дату PlanFix '{ts}': {e}")
        return None


def to_zoom_iso(ts: Union[int, float, str, None], timezone: str = "Europe/Moscow") -> Optional[str]:
    """
    Преобразовать время PlanFix в формат Zoom API: "2025-06-15T10:00:00"
    Zoom принимает время в указанном timezone без суффикса Z/+offset.
    """
    dt = planfix_ts_to_datetime(ts)
    if dt is None:
        return None
    tz = pytz.timezone(timezone)
    local = dt.astimezone(tz)
    return local.strftime("%Y-%m-%dT%H:%M:%S")


def calc_duration_minutes(
    date_begin: Union[int, float, str, None],
    date_end: Union[int, float, str, None],
    default: int = 60,
) -> int:
    """
    Вычислить длительность встречи в минутах по датам начала и конца задачи.
    Если даты отсутствуют или некорректны — возвращает default.
    """
    start = planfix_ts_to_datetime(date_begin)
    end = planfix_ts_to_datetime(date_end)

    if start and end and end > start:
        minutes = int((end - start).total_seconds() / 60)
        # Ограничиваем разумными пределами: от 15 мин до 8 часов
        return max(15, min(minutes, 480))

    return default


def format_meeting_time(ts: Union[int, float, str, None], timezone: str = "Europe/Moscow") -> str:
    """Форматировать время для отображения в комментарии PlanFix"""
    dt = planfix_ts_to_datetime(ts)
    if dt is None:
        return "время не указано"
    tz = pytz.timezone(timezone)
    local = dt.astimezone(tz)
    return local.strftime("%d.%m.%Y %H:%M") + f" ({timezone})"


def is_future(ts: Union[int, float, str, None]) -> bool:
    """Проверить, что время в будущем"""
    dt = planfix_ts_to_datetime(ts)
    if dt is None:
        return False
    return dt > datetime.now(pytz.UTC)
