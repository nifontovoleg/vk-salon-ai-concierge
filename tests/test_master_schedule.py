"""Gap 3: per-master working hours and days off."""

import json
import logging
from dataclasses import dataclass
from datetime import date, time

from app.repositories import MasterRepository
from app.services.booking_service import sync_master_schedules
from app.services.schedule import Schedule, parse_days_off
from app.services.tools import ToolExecutor
from tests.helpers import make_settings, next_weekday


@dataclass
class M:
    name: str = "Анна"
    working_hours: str | None = None
    days_off: str | None = None


def test_parse_days_off():
    days = parse_days_off("2030-01-05, 2030-01-10..2030-01-12; 31.12.2030, мусор")
    assert days == {date(2030, 1, 5), date(2030, 1, 10), date(2030, 1, 11), date(2030, 1, 12), date(2030, 12, 31)}


def test_window_is_intersection():
    schedule = Schedule(make_settings(salon_working_hours="Пн-Сб 10:00-20:00"))
    master = M(working_hours="Пн-Ср 12:00-22:00")
    monday, thursday, sunday = date(2030, 1, 7), date(2030, 1, 10), date(2030, 1, 13)
    assert schedule.window(monday, master) == (time(12), time(20))
    assert schedule.window(thursday, master) is None
    assert schedule.window(sunday, M()) is None  # салон закрыт
    assert schedule.window(thursday, M()) == (time(10), time(20))  # без графика — часы салона


def test_days_off_master_and_salon():
    monday = date(2030, 1, 7)
    schedule = Schedule(make_settings(salon_days_off="2030-01-08"))
    assert schedule.window(monday, M(days_off="2030-01-06..2030-01-07")) is None
    assert schedule.window(monday, M()) is not None
    assert schedule.window(date(2030, 1, 8), M()) is None
    assert schedule.window(date(2030, 1, 8)) is None


def test_bad_master_hours_fall_back_to_salon(caplog):
    schedule = Schedule(make_settings())
    with caplog.at_level(logging.WARNING):
        assert schedule.window(date(2030, 1, 7), M(working_hours="когда-нибудь")) == (time(10), time(20))
    assert "не распознаны" in caplog.text


async def _setup(session, anna="Пн-Ср 12:00-18:00", dmitry="Чт-Вс 10:00-14:00", anna_off=None):
    repo = MasterRepository(session)
    for master in await repo.list_active():
        master.working_hours = {"Анна": anna, "Дмитрий": dmitry}[master.name]
        master.days_off = anna_off if master.name == "Анна" else None
    await session.commit()


async def _call(session, name, settings=None, **args):
    executor = ToolExecutor(session, 7, settings=settings or make_settings())
    return json.loads(await executor.execute(name, args))


def _booking(day, time_, master=None):
    args = {"service_name": "Женская стрижка", "date": day.isoformat(), "time": time_, "phone": "+79991234567"}
    if master:
        args["master_name"] = master
    return args


async def test_slots_follow_master_hours(full_session):
    await _setup(full_session)
    monday = next_weekday(0)
    anna = await _call(full_session, "get_available_slots", date=monday.isoformat(), master_name="Анна",
                       service_name="Женская стрижка")
    assert anna["available_slots"][0] == "12:00" and anna["available_slots"][-1] == "17:00"
    dmitry = await _call(full_session, "get_available_slots", date=monday.isoformat(), master_name="Дмитрий")
    assert dmitry["available_slots"] == [] and "не работает" in dmitry["note"]
    salon = await _call(full_session, "get_available_slots", date=monday.isoformat())
    assert salon["available_slots"][0] == "12:00"  # в понедельник работает только Анна


async def test_booking_validation_uses_master_hours(full_session):
    await _setup(full_session)
    monday, thursday = next_weekday(0), next_weekday(3)
    off_day = await _call(full_session, "create_booking", **_booking(thursday, "12:00", "Анна"))
    assert "не работает" in off_day["error"] and off_day["nearest_free_slots"]
    outside = await _call(full_session, "create_booking", **_booking(monday, "10:00", "Анна"))
    assert "сетки" in outside["error"]
    auto = await _call(full_session, "create_booking", **_booking(thursday, "11:00"))
    assert auto["success"] and auto["master"] == "Дмитрий"
    late = await _call(full_session, "create_booking", **_booking(thursday, "15:00"))
    assert "сетки" in late["error"]


async def test_vacation_blocks_booking(full_session):
    monday = next_weekday(0)
    await _setup(full_session, anna_off=f"{monday.isoformat()}..{monday.isoformat()}")
    result = await _call(full_session, "create_booking", **_booking(monday, "12:00", "Анна"))
    assert "не работает" in result["error"]


async def test_get_masters_shows_schedule(full_session):
    await _setup(full_session)
    data = await _call(full_session, "get_masters")
    anna = next(m for m in data["masters"] if m["name"] == "Анна")
    assert anna["working_hours"] == "Пн-Ср 12:00-18:00" and len(anna["next_7_days"]) == 7


async def test_sync_from_env(full_session):
    settings = make_settings(
        master_schedules="Анна=Пн-Ср 10:00-18:00 | Дмитрий=ерунда | Пётр=Пн 10:00-12:00",
        master_days_off="Анна=2030-01-06..2030-01-07",
    )
    warnings = await sync_master_schedules(full_session, settings)
    masters = {m.name: m for m in await MasterRepository(full_session).list_active()}
    assert masters["Анна"].working_hours == "Пн-Ср 10:00-18:00"
    assert masters["Анна"].days_off == "2030-01-06..2030-01-07"
    assert masters["Дмитрий"].working_hours is None
    assert any("Пётр" in w for w in warnings) and any("Дмитрий" in w for w in warnings)


def test_seed_masters_have_valid_hours():
    from app.services.schedule import parse_hours_strict
    from scripts.seed import SEED_MASTERS

    for _, _, hours, _ in SEED_MASTERS:
        assert hours is None or parse_hours_strict(hours)
