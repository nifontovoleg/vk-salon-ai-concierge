"""Порт anonymizer.test.js (Олег Нифонтов) на pytest — все исходные кейсы."""

import re

from app.anonymizer import anonymize, find_pii, restore_card, restore_text


def _count(stats, type_):
    return next(item["count"] for item in stats["types"] if item["type"] == type_)


def test_contacts_replaced_with_tokens_and_restored_exactly():
    src = "Здравствуйте! Меня зовут Иван Петров. Пишите на ivan.petrov@example.com или звоните +7 (900) 123-45-67."
    r = anonymize(src)
    assert "Иван Петров" not in r.text
    assert "ivan.petrov@example.com" not in r.text
    assert "900" not in r.text
    assert "[ИМЯ_1]" in r.text and "[EMAIL_1]" in r.text and "[ТЕЛЕФОН_1]" in r.text
    assert _count(r.stats, "ИМЯ") == 1
    assert _count(r.stats, "EMAIL") == 1
    assert _count(r.stats, "ТЕЛЕФОН") == 1
    assert restore_text(r.text, r.mapping) == src


def test_known_form_fields_win_and_are_deduplicated():
    src = "\n".join(
        [
            "Текст заявки:",
            '"""',
            "Нужны 5 кресел. Пишите на ivan@example.com или звоните +7 900 000-00-00.",
            '"""',
            "",
            "Контактные данные из формы:",
            "- Имя: Иван Петров",
            "- Телефон: +7 900 000-00-00",
            "- E-mail: ivan@example.com",
        ]
    )
    known = [
        {"type": "ИМЯ", "value": "Иван Петров"},
        {"type": "ТЕЛЕФОН", "value": "+7 900 000-00-00"},
        {"type": "EMAIL", "value": "ivan@example.com"},
    ]
    r = anonymize(src, known=known)
    assert "Иван Петров" not in r.text
    assert "ivan@example.com" not in r.text
    assert "900" not in r.text
    assert r.stats["total"] == 3
    assert "[ИМЯ_1]" in r.text and "[ТЕЛЕФОН_1]" in r.text and "[EMAIL_1]" in r.text
    assert restore_text(r.text, r.mapping) == src


def test_local_phone_with_separators_without_context():
    src = "Мой номер 900 123-45-67, жду звонка."
    r = anonymize(src)
    assert "900 123-45-67" not in r.text
    assert "[ТЕЛЕФОН_1]" in r.text
    assert restore_text(r.text, r.mapping) == src


def test_addresses_recognized_by_keywords_and_restored():
    src = "Доставка: г. Москва, ул. Ленина, д. 5, кв. 10. Бюджет 1,5 млн."
    r = anonymize(src)
    assert "Москва" not in r.text
    assert "Ленина" not in r.text
    assert re.search(r"\[АДРЕС_\d+\]", r.text)
    assert "Бюджет 1,5 млн" in r.text
    assert restore_text(r.text, r.mapping) == src


def test_documents_bank_details_and_birth_dates():
    src = (
        "Паспорт 45 06 123456, СНИЛС 123-456-789 00, ИНН 7712345678, карта 4111 1111 1111 1111, "
        "р/с 40817810099910004312, дата рождения 12.05.1990."
    )
    r = anonymize(src)
    for type_ in ("ПАСПОРТ", "СНИЛС", "ИНН", "КАРТА", "СЧЁТ", "ДАТА_РОЖДЕНИЯ"):
        assert f"[{type_}_1]" in r.text, f"missing token {type_}"
        assert any(item["type"] == type_ for item in r.stats["types"]), f"missing stats {type_}"
    for raw in ("45 06 123456", "123-456-789", "7712345678", "4111 1111", "40817810099910004312", "12.05.1990"):
        assert raw not in r.text
    assert restore_text(r.text, r.mapping) == src


def test_logins_and_messenger_links():
    src = "Мой телеграм @ivan_petrov и https://t.me/ivan_petrov."
    r = anonymize(src)
    assert "@ivan_petrov" not in r.text
    assert "[ЛОГИН_1]" in r.text
    assert restore_text(r.text, r.mapping) == src


def test_company_names_and_order_numbers_untouched():
    src = "Компания Альфа-Логистик, заказ № 1234567890, нужно 10 кресел."
    r = anonymize(src)
    assert r.text == src
    assert len(r.mapping) == 0


def test_token_literals_in_input_cannot_collide():
    src = "Раньше здесь был [ИМЯ_1], теперь меня зовут Иван Петров."
    r = anonymize(src)
    assert "служебная метка" in r.text
    assert r.text.count("[ИМЯ_1]") == 1
    assert restore_text(r.text, r.mapping) == "Раньше здесь был [служебная метка], теперь меня зовут Иван Петров."


def test_unknown_model_tokens_become_neutral_phrase():
    mapping = {"[ИМЯ_1]": {"type": "ИМЯ", "value": "Иван Петров"}}
    restored = restore_text("Здравствуйте, [ИМЯ_1]! Ваш [ИМЯ_9].", mapping)
    assert restored == "Здравствуйте, Иван Петров! Ваш данные клиента."


def test_restore_card_walks_nested_structures():
    mapping = {
        "[ИМЯ_1]": {"type": "ИМЯ", "value": "Иван Петров"},
        "[ТЕЛЕФОН_1]": {"type": "ТЕЛЕФОН", "value": "+7 900 000-00-00"},
    }
    card = {
        "summary": "[ИМЯ_1] просит перезвонить",
        "key_data": ["[ИМЯ_1]", "[ТЕЛЕФОН_1]"],
        "nested": {"reply": "Здравствуйте, [ИМЯ_1]!"},
        "needs_human": False,
    }
    assert restore_card(card, mapping) == {
        "summary": "Иван Петров просит перезвонить",
        "key_data": ["Иван Петров", "+7 900 000-00-00"],
        "nested": {"reply": "Здравствуйте, Иван Петров!"},
        "needs_human": False,
    }


def test_find_pii_reports_categories_without_values():
    assert find_pii("") == {"total": 0, "types": []}
    assert find_pii(None) == {"total": 0, "types": []}
    found = find_pii("Иван Петров, +7 900 123-45-67, ivan@example.com, г. Москва, ул. Ленина, 5")
    assert found["total"] > 0
    assert all(isinstance(item["type"], str) and "value" not in item for item in found["types"])
    assert "Иван Петров" not in str(found)


def test_known_surname_masked_without_first_name():
    src = "Клиент: Соболев. Прошу перезвонить по заявке."
    r = anonymize(src, known=[{"type": "ИМЯ", "value": "Даниил Соболев"}])
    assert "Соболев" not in r.text
    assert "[ИМЯ_1]" in r.text
    assert _count(r.stats, "ИМЯ") == 1
    assert restore_text(r.text, r.mapping) == src


def test_full_known_name_masked_as_one_token():
    r = anonymize("Меня зовут Даниил Соболев, нужны кресла.", known=[{"type": "ИМЯ", "value": "Даниил Соболев"}])
    assert "Даниил" not in r.text and "Соболев" not in r.text
    assert len(re.findall(r"\[ИМЯ_\d+\]", r.text)) == 1


def test_surname_after_marker_masked_common_words_not():
    src = "Фамилия: Соболев. Гражданин Российской Федерации."
    r = anonymize(src)
    assert "Соболев" not in r.text
    assert "[ИМЯ_1]" in r.text
    assert "Российской" in r.text
    assert restore_text(r.text, r.mapping) == src


def test_empty_input_and_long_requests():
    empty = anonymize("")
    assert empty.text == "" and empty.stats["total"] == 0
    long_text = "Заявка без персональных данных. " * 600 + "ivan@example.com"
    assert len(long_text) < 20_000
    r = anonymize(long_text)
    assert len(r.text) < len(long_text)
    assert "[EMAIL_1]" in r.text
    assert "ivan@example.com" not in r.text


# --- дополнительно к JS-тестам: общая таблица меток для бота ---


def test_session_shares_tokens_across_texts_and_neutralizes_unknown():
    from app.anonymizer import PiiSession

    session = PiiSession()
    first = session.anonymize("Меня зовут Иван Петров, тел. +7 900 123-45-67")
    second = session.anonymize('{"client_phone": "+7 (900) 123-45-67"}')
    assert "[ТЕЛЕФОН_1]" in first and "[ТЕЛЕФОН_1]" in second
    assert session.restore({"phone": "[ТЕЛЕФОН_1]", "x": ["[ИМЯ_1]"]}) == {
        "phone": "+7 900 123-45-67",
        "x": ["Иван Петров"],
    }
    assert PiiSession().restore("Ваш [ИМЯ_3]") == "Ваш данные клиента"
