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


def parse_hours_strict(text: str | None) -> WorkingHours:
    """Parse «Пн-Сб 10:00-20:00, Вс 11:00-18:00»; returns {} if nothing recognized."""
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
    return hours


def parse_working_hours(text: str) -> WorkingHours:
    """Salon hours. Дни, которых нет в строке, — выходные.

    Пустая или нераспознанная строка — каждый день 10:00–20:00.
    """
    hours = parse_hours_strict(text)
    if not hours:
        if (text or "").strip():
            logger.warning("SALON_WORKING_HOURS не распознаны, использую 10:00-20:00 ежедневно")
        hours = {day: DEFAULT_HOURS for day in range(7)}
    return hours


def _parse_date(value: str) -> date:
    value = value.strip()
    if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value):
        day, month, year = (int(p) for p in value.split("."))
        return date(year, month, day)
    return date.fromisoformat(value)


def parse_days_off(text: str | None) -> set[date]:
    """Parse «2026-11-02, 2026-11-10..2026-11-15, 31.12.2026» into a set of dates."""
    result: set[date] = set()
    for item in re.split(r"[,;]", text or ""):
        item = item.strip()
        if not item:
            continue
        try:
            if ".." in item:
                first, last = (_parse_date(p) for p in item.split("..", 1))
                if last < first or (last - first).days > 366:
                    raise ValueError(item)
                current = first
                while current <= last:
                    result.add(current)
                    current += timedelta(days=1)
            else:
                result.add(_parse_date(item))
        except ValueError:
            logger.warning("Не распознана дата выходного: %s", item)
    return result


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def overlaps(start_a: time, duration_a: int, start_b: time, duration_b: int) -> bool:
    a0, b0 = _minutes(start_a), _minutes(start_b)
    return a0 < b0 + duration_b and b0 < a0 + duration_a


class Schedule:
    """Booking grid for the salon and its masters.

    `master` — любой объект с атрибутами `working_hours` и `days_off`
    (строки в тех же форматах) или None для салона в целом.
    """

    def __init__(self, settings: Settings, now: datetime | None = None) -> None:
        self._tz = settings.tz
        self.step = settings.slot_step_minutes
        self.hours = parse_working_hours(settings.salon_working_hours)
        self.salon_days_off = parse_days_off(getattr(settings, "salon_days_off", ""))
        self._fixed_now = now
        self._cache: dict[str, object] = {}

    def now(self) -> datetime:
        """Current local salon time (naive)."""
        if self._fixed_now is not None:
            return self._fixed_now
        return datetime.now(self._tz).replace(tzinfo=None, second=0, microsecond=0)

    def today(self) -> date:
        return self.now().date()

    def _master_hours(self, master) -> WorkingHours | None:
        raw = (getattr(master, "working_hours", None) or "").strip()
        if not raw:
            return None
        key = "h:" + raw
        if key not in self._cache:
            parsed = parse_hours_strict(raw)
            if not parsed:
                logger.warning("Часы мастера %s не распознаны: %s", getattr(master, "name", "?"), raw)
            self._cache[key] = parsed or None
        return self._cache[key]  # type: ignore[return-value]

    def _master_days_off(self, master) -> set[date]:
        raw = (getattr(master, "days_off", None) or "").strip()
        if not raw:
            return set()
        key = "d:" + raw
        if key not in self._cache:
            self._cache[key] = parse_days_off(raw)
        return self._cache[key]  # type: ignore[return-value]

    def window(self, day: date, master=None) -> tuple[time, time] | None:
        """Working window for the day: salon hours ∩ master hours, minus days off."""
        if day in self.salon_days_off:
            return None
        salon = self.hours.get(day.weekday())
        if salon is None or master is None:
            return salon
        if day in self._master_days_off(master):
            return None
        own = self._master_hours(master)
        if own is None:
            return salon
        personal = own.get(day.weekday())
        if personal is None:
            return None
        opening, closing = max(salon[0], personal[0]), min(salon[1], personal[1])
        return (opening, closing) if opening < closing else None

    def describe_day(self, day: date, master=None) -> str:
        window = self.window(day, master)
        if window is None:
            return "выходной"
        return f"{window[0].strftime('%H:%M')}–{window[1].strftime('%H:%M')}"

    def grid(self, day: date, duration: int, include_past: bool = False, master=None) -> list[time]:
        """Start times where a service of `duration` minutes fits before closing."""
        window = self.window(day, master)
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

    def validate_start(self, day: date, start: time, duration: int, masters: list | None = None) -> str | None:
        """Return an error message or None if `start` is on the grid of any of `masters`.

        masters=None или [None] — проверка по часам салона.
        """
        candidates = masters or [None]
        now = self.now()
        if day < now.date():
            return "Нельзя записаться на прошедшую дату"
        if all(self.window(day, m) is None for m in candidates):
            if len(candidates) == 1 and candidates[0] is not None:
                name = getattr(candidates[0], "name", "мастер")
                return f"Мастер {name} {day.strftime('%d.%m.%Y')} не работает"
            return f"{day.strftime('%d.%m.%Y')} салон не работает"
        full_grid = sorted(
            {t for m in candidates for t in self.grid(day, duration, include_past=True, master=m)}
        )
        if start not in full_grid:
            preview = ", ".join(t.strftime("%H:%M") for t in full_grid[:12]) or "нет"
            hours = self.describe_day(day, candidates[0]) if len(candidates) == 1 else self.describe_day(day)
            return f"Запись возможна только на время из сетки ({hours}): {preview}"
        if datetime.combine(day, start) <= now:
            return "Это время уже прошло"
        return None


def minutes_of(value: time) -> int:
    return _minutes(value)


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
