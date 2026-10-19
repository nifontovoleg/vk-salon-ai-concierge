"""Fixes 3–5: handoff triggers, no-admin honesty, silent bot + forwarding."""

import logging
from unittest.mock import AsyncMock

import pytest

from app.handlers.messages import execute_response, handle_release_dialog, handle_take_dialog
from app.repositories import DialogRepository
from app.schemas.message import DialogActionType, IncomingMessage
from app.services.dialog_service import DialogService, is_resume_command, wants_admin
from tests.helpers import ADMIN_ID, CLIENT_ID, make_settings


@pytest.mark.parametrize(
    "text",
    ["Позовите администратора", "админ", "Можно оператора?", "Соедините с менеджером",
     "хочу поговорить с живым человеком", "позовите человека", "Свяжите меня с сотрудником"],
)
def test_admin_triggers(text):
    assert wants_admin(text)


@pytest.mark.parametrize(
    "text",
    ["Запишите двух человек на стрижку", "человек", "Сколько человек у вас работает?",
     "администраторская стойка у входа?", "Подойду на маникюр"],
)
def test_admin_not_triggered(text):
    assert not wants_admin(text)


def test_resume_only_whole_command():
    assert is_resume_command("Отмена")
    assert is_resume_command("назад!")
    assert is_resume_command("Вернуться к боту")
    assert not is_resume_command("хочу вернуться к Анне")
    assert not is_resume_command("перенесите назад на час")


async def _msg(service, text="", intent=None, user=CLIENT_ID):
    return await service.handle_message(IncomingMessage(user_id=user, peer_id=user, text=text, intent=intent))


async def test_resume_word_inside_sentence_keeps_handoff(full_session, settings):
    openrouter = AsyncMock()
    openrouter.chat_completion.side_effect = RuntimeError("offline")
    service = DialogService(full_session, openrouter, settings)
    await _msg(service, intent="admin")
    await _msg(service, text="Назад к вопросу о цене: сколько стоит стрижка?")
    assert await DialogRepository(full_session).has_active_transfer(CLIENT_ID)
    await _msg(service, intent="resume")
    assert not await DialogRepository(full_session).has_active_transfer(CLIENT_ID)


async def test_no_admins_honest_reply(seeded_session, caplog):
    settings = make_settings()
    service = DialogService(seeded_session, AsyncMock(), settings)
    with caplog.at_level(logging.WARNING):
        response = await _msg(service, text="Позовите администратора")
    assert response.actions[0].type == DialogActionType.REPLY
    text = response.actions[0].text
    assert "не подключены" in text and "Передал" not in text
    assert settings.salon_phone in text and settings.salon_address in text
    assert not await DialogRepository(seeded_session).has_active_transfer(CLIENT_ID)
    assert "no active administrators" in caplog.text


async def test_no_admins_llm_tool(seeded_session):
    from app.repositories import ConsentRepository

    await ConsentRepository(seeded_session).give(CLIENT_ID, None)
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = lambda r: r["choices"][0]["message"]
    call = {"id": "c1", "type": "function", "function": {"name": "transfer_to_admin", "arguments": '{"reason": "жалоба"}'}}
    openrouter.chat_completion.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "Передаю администратору!"}}]},
    ]
    response = await _msg(DialogService(seeded_session, openrouter, make_settings()), text="У меня жалоба на мастера")
    assert response.actions[0].type == DialogActionType.REPLY
    assert "не подключены" in response.actions[0].text


async def test_engaged_dialog_is_silent_and_forwarded(app_db, fake_api):
    settings = make_settings()
    async with app_db() as session:
        await _msg(DialogService(session, AsyncMock(), settings), intent="admin")
        await session.commit()

    reply = await handle_take_dialog(fake_api, ADMIN_ID, CLIENT_ID)
    assert "Вы взяли диалог" in reply
    assert "Администратор подключился" in fake_api.to(CLIENT_ID)[0]

    openrouter = AsyncMock()
    async with app_db() as session:
        response = await _msg(DialogService(session, openrouter, settings), text="Можно перенести на 16:00?")
        await session.commit()
    assert response.client_texts == []
    openrouter.chat_completion.assert_not_called()

    answers = []

    async def answer(text, keyboard=None):
        answers.append(text)

    await execute_response(fake_api, settings, CLIENT_ID, response, answer)
    assert answers == []
    forwarded = fake_api.to(ADMIN_ID)[-1]
    assert "Можно перенести на 16:00?" in forwarded and f"sel={CLIENT_ID}" in forwarded

    assert "возвращён боту" in await handle_release_dialog(fake_api, ADMIN_ID, CLIENT_ID)
    assert "снова на связи" in fake_api.to(CLIENT_ID)[-1]


async def test_take_by_second_admin_refused(app_db, fake_api):
    from app.repositories import AdministratorRepository

    settings = make_settings()
    async with app_db() as session:
        await AdministratorRepository(session).create(501, "Second")
        await _msg(DialogService(session, AsyncMock(), settings), intent="admin")
        await session.commit()
    await handle_take_dialog(fake_api, ADMIN_ID, CLIENT_ID)
    assert "другой администратор" in await handle_take_dialog(fake_api, 501, CLIENT_ID)


async def test_client_resume_notifies_engaged_admin(app_db, fake_api):
    settings = make_settings()
    async with app_db() as session:
        await _msg(DialogService(session, AsyncMock(), settings), intent="admin")
        await session.commit()
    await handle_take_dialog(fake_api, ADMIN_ID, CLIENT_ID)
    async with app_db() as session:
        response = await _msg(DialogService(session, AsyncMock(), settings), text="отмена")
        await session.commit()
    types = [a.type for a in response.actions]
    assert types == [DialogActionType.REPLY, DialogActionType.FORWARD_TO_ADMIN]
    assert response.actions[1].admin_vk_id == ADMIN_ID


async def test_take_closed_request(app_db, fake_api):
    assert "уже закрыт" in await handle_take_dialog(fake_api, ADMIN_ID, CLIENT_ID)
    assert fake_api.sent == []
