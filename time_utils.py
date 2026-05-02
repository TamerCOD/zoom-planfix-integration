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


def planfix_ts_to_datetime(ts: Union[int, float, str, dict, None]) -> Optional[datetime]:
    """
    Преобразовать значение даты/времени из PlanFix в datetime.

    Поддерживает форматы PlanFix:
      - dict: {"date": "16-01-2025", "time": "07:10", "datetime": "2025-01-16T07:10Z",
               "dateTimeUtcSeconds": "2025-01-16T07:10:00+0000"}
      - Unix timestamp (int/float)
      - ISO 8601 строки: "2025-01-16T07:10Z", "2025-01-16T07:10:00+0000"
      - Простая дата: "YYYY-MM-DD" или "DD-MM-YYYY"
    """
    if ts is None:
        return None

    # Если пришёл dict от PlanFix API — берём лучший вариант
    if isinstance(ts, dict):
        ts = ts.get("dateTimeUtcSeconds") or ts.get("datetime") or ts.get("date")
        if ts is None:
            return None

    try:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.UTC)

        s = str(ts).strip()

        # ISO с временем
        if "T" in s:
            # Заменяем формат "+0000" → "+00:00" для совместимости
            normalized = s.replace("Z", "+00:00")
            # Если timezone в формате "+0000" без двоеточия
            if len(normalized) >= 5 and normalized[-5] in "+-" and ":" not in normalized[-5:]:
                normalized = normalized[:-2] + ":" + normalized[-2:]
            try:
                dt = datetime.fromisoformat(normalized)
            except Exception:
                # Пробуем без миллисекунд
                dt = datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt

        # Только дата
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=pytz.UTC)
            except ValueError:
                continue

        return None
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
