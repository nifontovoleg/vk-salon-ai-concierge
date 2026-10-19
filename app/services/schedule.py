"""Salon working hours, booking grid and overlap checks.

Время записей хранится как локальное время салона (TIMEZONE из .env).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta

from app.config import Settings

logger = logging.getLogger(__name__)

DAY_CODES = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DEFAULT_HOURS = (time(10, 0), time(20, 0))

_DAY = r"(?:пн|вт|ср|чт|пт|сб|вс)"
_DAY_ITEM = rf"{_DAY}(?:\s*-\s*{_DAY})?"
_TIME = r"\d{1,2}[:.]\d{2}"
_SEGMENT_RE = re.compile(
    rf"(?P<days>{_DAY_ITEM}(?:\s*,\s*{_DAY_ITEM})*|ежедневно|без выходных)?\s*:?\s*"
    rf"(?P<open>{_TIME})\s*-\s*(?P<close>{_TIME})"
)

WorkingHours = dict[int, tuple[time, time]]


def _parse_time(value: str) -> time:
    hours, minutes = re.split(r"[:.]", value)
    return time(int(hours), int(minutes))


def _expand_days(spec: str | None) -> list[int]:
    if not spec or spec in ("ежедневно", "без выходных"):
        return list(range(7))
    days: list[int] = []
    for item in re.split(r"\s*,\s*", spec):
        if "-" in item:
            first, last = (DAY_CODES[p.strip()] for p in item.split("-"))
            current = first
            while True:
                days.append(current)
                if current == last:
                    break
                current = (current + 1) % 7
        else:
            days.append(DAY_CODES[item.strip()])
    return days


def parse_working_hours(text: str) -> WorkingHours:
    """Parse strings like «Пн-Сб 10:00-20:00, Вс 11:00-18:00».

    Дни, которых нет в строке, считаются выходными. Пустая или нераспознанная
    строка — каждый день 10:00–20:00.
    """
    normalized = (text or "").lower().replace("ё", "е").replace("–", "-").replace("—", "-")
    hours: WorkingHours = {}
    for match in _SEGMENT_RE.finditer(normalized):
        try:
            opening, closing = _parse_time(match["open"]), _parse_time(match["close"])
        except ValueError:
            continue
        if closing <= opening:
            continue
        for day in _expand_days(match["days"]):
            hours[day] = (opening, closing)
    if not hours:
        if normalized.strip():
            logger.warning("SALON_WORKING_HOURS не распознаны, использую 10:00-20:00 ежедневно")
        hours = {day: DEFAULT_HOURS for day in range(7)}
    return hours


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def overlaps(start_a: time, duration_a: int, start_b: time, duration_b: int) -> bool:
    a0, b0 = _minutes(start_a), _minutes(start_b)
    return a0 < b0 + duration_b and b0 < a0 + duration_a


class Schedule:
    """Booking grid for the salon."""

    def __init__(self, settings: Settings, now: datetime | None = None) -> None:
        self._tz = settings.tz
        self.step = settings.slot_step_minutes
        self.hours = parse_working_hours(settings.salon_working_hours)
        self._fixed_now = now

    def now(self) -> datetime:
        """Current local salon time (naive)."""
        if self._fixed_now is not None:
            return self._fixed_now
        return datetime.now(self._tz).replace(tzinfo=None, second=0, microsecond=0)

    def today(self) -> date:
        return self.now().date()

    def window(self, day: date) -> tuple[time, time] | None:
        return self.hours.get(day.weekday())

    def describe_day(self, day: date) -> str:
        window = self.window(day)
        if window is None:
            return "выходной"
        return f"{window[0].strftime('%H:%M')}–{window[1].strftime('%H:%M')}"

    def grid(self, day: date, duration: int, include_past: bool = False) -> list[time]:
        """Start times on the grid where a service of `duration` minutes fits before closing."""
        window = self.window(day)
        if window is None:
            return []
        now = self.now()
        if not include_past and day < now.date():
            return []
        opening, closing = window
        current = datetime.combine(day, opening)
        end = datetime.combine(day, closing)
        slots: list[time] = []
        while current + timedelta(minutes=duration) <= end:
            if include_past or current > now:
                slots.append(current.time())
            current += timedelta(minutes=self.step)
        return slots

    def validate_start(self, day: date, start: time, duration: int) -> str | None:
        """Return an error message or None if the start time is bookable."""
        now = self.now()
        if day < now.date():
            return "Нельзя записаться на прошедшую дату"
        if self.window(day) is None:
            return f"{day.strftime('%d.%m.%Y')} салон не работает"
        full_grid = self.grid(day, duration, include_past=True)
        if start not in full_grid:
            preview = ", ".join(t.strftime("%H:%M") for t in full_grid[:12]) or "нет"
            return f"Запись возможна только на время из сетки ({self.describe_day(day)}): {preview}"
        if datetime.combine(day, start) <= now:
            return "Это время уже прошло"
        return None


def is_free(
    start: time,
    duration: int,
    busy: list[tuple],
    master_id: int | None,
    has_masters: bool,
) -> bool:
    """Check a candidate interval against (start, duration, master_id) bookings.

    Если мастера заведены, пересечения считаются по мастеру; запись без мастера
    (старые данные) блокирует всех. Если мастеров нет — салон как одно рабочее место.
    """
    for b_start, b_duration, b_master in busy:
        if has_masters and b_master is not None and b_master != master_id:
            continue
        if overlaps(start, duration, b_start, b_duration):
            return False
    return True
