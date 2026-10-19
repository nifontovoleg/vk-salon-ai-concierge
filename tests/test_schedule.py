"""Fix 2: grid, past dates, working hours, duration, timezone, per-master overlap."""

import json
from datetime import date, datetime, time, timedelta

import pytest

from app.services.schedule import Schedule, overlaps, parse_working_hours
from app.services.tools import ToolExecutor
from tests.helpers import future_day, make_settings


def test_parse_working_hours():
    hours = parse_working_hours("Пн-Сб 10:00-20:00, Вс 11:00-18:00")
    assert hours[0] == (time(10), time(20)) and hours[5] == (time(10), time(20))
    assert hours[6] == (time(11), time(18))
    closed_sunday = parse_working_hours("Пн-Пт 9.00–21.00; Сб 10:00-16:00; Вс выходной")
    assert 6 not in closed_sunday and closed_sunday[5] == (time(10), time(16))
    assert parse_working_hours("")[3] == (time(10), time(20))


def test_overlaps_half_open():
    assert overlaps(time(12), 60, time(12, 30), 45)
    assert not overlaps(time(11), 60, time(12), 60)


def test_grid_respects_duration_and_now():
    settings = make_settings(salon_working_hours="Пн-Вс 10:00-20:00", slot_step_minutes=30)
    day = date(2030, 1, 7)
    schedule = Schedule(settings, now=datetime(2030, 1, 7, 14, 10))
    grid = schedule.grid(day, 90)
    assert grid[0] == time(14, 30)  # прошедшее время сегодня отброшено
    assert grid[-1] == time(18, 30)  # 18:30 + 90 мин = 20:00
    assert schedule.grid(day - timedelta(days=1), 60) == []


def test_timezone_now():
    yekt = Schedule(make_settings(timezone="Asia/Yekaterinburg"))
    msk = Schedule(make_settings(timezone="Europe/Moscow"))
    diff = datetime.combine(yekt.today(), yekt.now().time()) - datetime.combine(msk.today(), msk.now().time())
    assert abs(diff - timedelta(hours=2)) <= timedelta(minutes=1)


def test_bad_timezone_rejected():
    with pytest.raises(ValueError):
        make_settings(timezone="Mars/Olympus")


async def _create(session, settings, **args):
    base = {"service_name": "Женская стрижка", "date": str(future_day(2)), "time": "12:00",
            "phone": "+79991234567"}
    base.update(args)
    return json.loads(await ToolExecutor(session, 7, settings=settings).execute("create_booking", base))


async def test_reject_past_and_off_grid(full_session):
    settings = make_settings()
    assert "прошедшую" in (await _create(full_session, settings, date="2020-01-01"))["error"]
    assert "сетки" in (await _create(full_session, settings, time="12:15"))["error"]
    assert "сетки" in (await _create(full_session, settings, time="23:00"))["error"]
    assert "сетки" in (await _create(full_session, settings, time="19:30"))["error"]


async def test_reject_closed_day(full_session):
    settings = make_settings(salon_working_hours="Пн-Сб 10:00-20:00")
    day = future_day(1)
    while day.weekday() != 6:
        day += timedelta(days=1)
    result = await _create(full_session, settings, date=str(day))
    assert "не работает" in result["error"]


async def test_overlap_per_master(full_session):
    settings = make_settings(slot_step_minutes=30)
    assert (await _create(full_session, settings, master_name="Анна"))["success"]
    # Анна занята 12:00–13:00
    assert "занято" in (await _create(full_session, settings, master_name="Анна", time="12:30"))["error"]
    assert "занято" in (await _create(full_session, settings, master_name="Анна", time="11:30"))["error"]
    assert (await _create(full_session, settings, master_name="Анна", time="11:00"))["success"]
    # Дмитрий в это же время свободен
    assert (await _create(full_session, settings, master_name="Дмитрий", time="12:30"))["success"]


async def test_auto_assign_free_master(full_session):
    settings = make_settings()
    first = await _create(full_session, settings, time="15:00")
    second = await _create(full_session, settings, time="15:00")
    assert {first["master"], second["master"]} == {"Анна", "Дмитрий"}
    assert "занято" in (await _create(full_session, settings, time="15:00"))["error"]

    slots = json.loads(await ToolExecutor(full_session, 7, settings=settings).execute(
        "get_available_slots", {"date": str(future_day(2)), "service_name": "Женская стрижка"}))
    assert "15:00" not in slots["available_slots"] and "14:00" in slots["available_slots"]
    assert slots["duration_minutes"] == 60


async def test_slots_past_date_error(full_session):
    result = json.loads(await ToolExecutor(full_session, 7, settings=make_settings()).execute(
        "get_available_slots", {"date": "2020-01-01"}))
    assert "прошлом" in result["error"]


def test_default_slot_step_is_30_minutes():
    from app.config.settings import Settings

    assert Settings.model_fields["slot_step_minutes"].default == 30


async def test_saturday_half_hour_slot_is_bookable(full_session):
    settings = make_settings()  # шаг по умолчанию — 30 минут
    day = future_day(1)
    while day.weekday() != 5:
        day += timedelta(days=1)
    assert (await _create(full_session, settings, date=str(day), time="14:30"))["success"]
