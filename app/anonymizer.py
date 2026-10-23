"""Обезличивание персональных данных перед отправкой текста в LLM.

Порт на Python модуля anonymizer.js Олега Нифонтова (intens2010):
те же правила, приоритеты и формат меток ``[ТЕЛЕФОН_1]``. Поиск построен на
регулярных выражениях и словаре имён — это не шифрование, необычные записи
(имя без фамилии, адрес без ключевых слов, номер «прописью») могут быть пропущены.

API:
    anonymize(text, known=...) -> AnonymizeResult(text, mapping, stats)
    find_pii(text)            -> {"total": n, "types": [...]}
    restore_text(text, mapping)
    restore_card(obj, mapping)
    PiiSession — общая таблица меток на один диалог/запрос к модели.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

TOKEN_TYPES = [
    "ИМЯ",
    "ТЕЛЕФОН",
    "EMAIL",
    "АДРЕС",
    "ПАСПОРТ",
    "СНИЛС",
    "ИНН",
    "ОГРН",
    "КАРТА",
    "СЧЁТ",
    "ДАТА_РОЖДЕНИЯ",
    "ЛОГИН",
]

TYPE_LABELS = {
    "ИМЯ": "имя",
    "ТЕЛЕФОН": "телефон",
    "EMAIL": "e-mail",
    "АДРЕС": "адрес",
    "ПАСПОРТ": "паспорт",
    "СНИЛС": "СНИЛС",
    "ИНН": "ИНН",
    "ОГРН": "ОГРН",
    "КАРТА": "банковская карта",
    "СЧЁТ": "расчётный счёт",
    "ДАТА_РОЖДЕНИЯ": "дата рождения",
    "ЛОГИН": "логин",
}

TOKEN_TYPE_PATTERN = "|".join(TOKEN_TYPES)

NAMES = frozenset(
    [
        "александр",
        "алексей",
        "альберт",
        "анатолий",
        "андрей",
        "антон",
        "аркадий",
        "арсений",
        "артём",
        "артем",
        "артур",
        "борис",
        "вадим",
        "валентин",
        "валерий",
        "василий",
        "виктор",
        "виталий",
        "владимир",
        "владислав",
        "всеволод",
        "вячеслав",
        "геннадий",
        "георгий",
        "глеб",
        "григорий",
        "даниил",
        "данил",
        "денис",
        "дмитрий",
        "евгений",
        "егор",
        "захар",
        "иван",
        "игнат",
        "игорь",
        "илья",
        "кирилл",
        "константин",
        "лев",
        "леонид",
        "макар",
        "максим",
        "марк",
        "матвей",
        "мирон",
        "михаил",
        "никита",
        "николай",
        "олег",
        "павел",
        "пётр",
        "петр",
        "платон",
        "роман",
        "руслан",
        "савелий",
        "семён",
        "семен",
        "сергей",
        "станислав",
        "степан",
        "тимофей",
        "тимур",
        "фёдор",
        "федор",
        "филипп",
        "эдуард",
        "юрий",
        "ярослав",
        "алла",
        "альбина",
        "анастасия",
        "ангелина",
        "анна",
        "валентина",
        "валерия",
        "вера",
        "виктория",
        "галина",
        "дарья",
        "диана",
        "евгения",
        "екатерина",
        "елена",
        "зоя",
        "инна",
        "ирина",
        "карина",
        "ксения",
        "кристина",
        "лариса",
        "лидия",
        "лилия",
        "любовь",
        "маргарита",
        "марина",
        "мария",
        "милана",
        "надежда",
        "наталья",
        "наталия",
        "нина",
        "оксана",
        "олимпиада",
        "ольга",
        "полина",
        "светлана",
        "снежана",
        "софья",
        "софия",
        "таисия",
        "татьяна",
        "ульяна",
        "эльвира",
        "эмма",
        "юлия",
        "яна",
    ]
)

# Эквиваленты классов Unicode из JS (\p{L}, \p{N}) для модуля re.
L = r"[^\W\d_]"  # буква
LN = r"[^\W_]"  # буква или цифра
D = "[0-9]"  # \d в JS — только ASCII

SURNAME_RE = re.compile(
    r"(?:ов|ев|ёв|ин|ын|ский|цкий|ская|цкая|ых|их|ова|ева|ёва|ина|ына|скому|цкому|ской|цкой|ову|еву|ину|ыну)$"
)
PATRONYMIC_RE = re.compile(r"(?:ович|евич|ьич|ич|овна|евна|ична|инична|овичу|евичу|овне|евне|ичне)$")
NOT_A_SURNAME = frozenset(
    {
        "российской",
        "российский",
        "федерации",
        "федерация",
        "республики",
        "области",
        "края",
        "города",
        "улицы",
        "проспекта",
        "клиент",
        "заказчик",
    }
)

PHONE_CONTEXT_RE = re.compile(
    r"(?:тел\.?|телефон|моб\.?|мобильн|позвон|звон|контакт|whatsapp|ватсап|telegram|телеграм)", re.I
)
CARD_CONTEXT_RE = re.compile(r"(?:карт|visa|mastercard|maestro|мир)", re.I)
ACCOUNT_CONTEXT_RE = re.compile(r"(?:сч[её]т|счет|р/с|расч[её]тн)", re.I)
PASSPORT_CONTEXT_RE = re.compile(r"(?:паспорт|серия|выдан)", re.I)
INN_CONTEXT_RE = re.compile(r"(?:инн)", re.I)
OGRN_CONTEXT_RE = re.compile(r"(?:огрн|огрнип)", re.I)
BIRTHDATE_CONTEXT_RE = re.compile(r"(?:родил|рождения|д\.\s?р\.|дата рожд)", re.I)
ADDRESS_HINT_RE = re.compile(
    r"(?:[Гг]\.|город|[Уу]л\.?|улица|[Пп]р-т|проспект|[Пп]ер\.?|переулок|[Бб]-р|бульвар|[Шш]оссе|[Шш]\.|"
    r"[Нн]аб\.?|набережная|[Пп]л\.?|площадь|[Мм]кр\.?|микрорайон|[Оо]бл\.?|область|[Кк]рай|[Рр]есп\.?|"
    r"республика|[Пп]ос\.?|посёлок|деревня|село|[Дд]ом|[Дд]\.|[Кк]в\.?|квартира|[Оо]фис|[Пп]ом\.?|помещение|[Ээ]таж)"
)


def luhn_valid(digits: str) -> bool:
    total = 0
    double = False
    for ch in reversed(digits):
        digit = int(ch)
        if double:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        double = not double
    return total % 10 == 0


def _has_digit(value: str) -> bool:
    return re.search(D, value) is not None


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def looks_like_person_name(value: str) -> bool:
    words = [w for w in value.split() if w]
    if len(words) < 2:
        return False
    first, second = words[0].lower(), words[1].lower()
    first_is_name = first in NAMES
    second_is_name = second in NAMES
    first_is_surname = SURNAME_RE.search(first) is not None
    second_is_surname = SURNAME_RE.search(second) is not None
    second_is_patronymic = PATRONYMIC_RE.search(second) is not None
    first_is_patronymic = PATRONYMIC_RE.search(first) is not None
    return (
        (first_is_name and (second_is_surname or second_is_patronymic))
        or (second_is_name and (first_is_surname or first_is_patronymic))
        or (first_is_name and second_is_name)
    )


@dataclass(frozen=True)
class Rule:
    type: str
    re: re.Pattern
    group: int = 0
    validate: Callable[[str, str], bool] | None = None
    radius: int = 60
    priority: int = 0


KNOWN_PRIORITY = 20
NOT_N_BEFORE = r"(?<!\d)"  # (?<![\p{N}])
NOT_N_AFTER = r"(?!\d)"  # (?![\p{N}])
SEP = r"[\s()\-.]*"
WORD = rf"(?:{L}|-)*"  # [\p{L}\-]*
ADDR_NUM = rf"{D}+{L}?"  # \d+[\p{L}]?
NAME_WORD = "[А-ЯЁ][а-яё-]"

RULES: list[Rule] = [
    Rule("EMAIL", re.compile(rf"(?:{LN}|[._%+'-])+@(?:{LN}|[.-])+\.{L}{{2,}}", re.I)),
    Rule("ТЕЛЕФОН", re.compile(rf"{NOT_N_BEFORE}\+{D}(?:{SEP}{D}){{8,13}}{NOT_N_AFTER}")),
    Rule("ТЕЛЕФОН", re.compile(rf"{NOT_N_BEFORE}(?:\+7|8|7){SEP}{D}(?:{SEP}{D}){{9}}{NOT_N_AFTER}")),
    Rule("ТЕЛЕФОН", re.compile(rf"{NOT_N_BEFORE}{D}{{3}}[\s-]{D}{{3}}[\s-]?{D}{{2}}[\s-]?{D}{{2}}{NOT_N_AFTER}")),
    Rule(
        "ТЕЛЕФОН",
        re.compile(rf"{NOT_N_BEFORE}{D}(?:{SEP}{D}){{9}}{NOT_N_AFTER}"),
        validate=lambda v, w: PHONE_CONTEXT_RE.search(w) is not None,
        radius=40,
    ),
    Rule(
        "КАРТА",
        re.compile(rf"{NOT_N_BEFORE}(?:{D}[ \-]?){{15,18}}{D}{NOT_N_AFTER}"),
        validate=lambda v, w: (
            16 <= len(_digits(v)) <= 19 and (luhn_valid(_digits(v)) or CARD_CONTEXT_RE.search(w) is not None)
        ),
        radius=40,
    ),
    Rule(
        "СЧЁТ",
        re.compile(rf"{NOT_N_BEFORE}(?:{D}[ \-]?){{19}}{D}{NOT_N_AFTER}"),
        validate=lambda v, w: len(_digits(v)) == 20 and ACCOUNT_CONTEXT_RE.search(w) is not None,
        radius=30,
    ),
    Rule(
        "ПАСПОРТ",
        re.compile(rf"{NOT_N_BEFORE}{D}{{2}}\s?{D}{{2}}\s?(?:№\s?)?{D}{{6}}{NOT_N_AFTER}"),
        validate=lambda v, w: PASSPORT_CONTEXT_RE.search(w) is not None,
        radius=30,
    ),
    Rule("СНИЛС", re.compile(rf"{NOT_N_BEFORE}{D}{{3}}-{D}{{3}}-{D}{{3}}[\s-]?{D}{{2}}{NOT_N_AFTER}")),
    Rule(
        "ИНН",
        re.compile(rf"{NOT_N_BEFORE}(?:{D}{{12}}|{D}{{10}}){NOT_N_AFTER}"),
        validate=lambda v, w: INN_CONTEXT_RE.search(w) is not None,
        radius=30,
    ),
    Rule(
        "ОГРН",
        re.compile(rf"{NOT_N_BEFORE}(?:{D}{{15}}|{D}{{13}}){NOT_N_AFTER}"),
        validate=lambda v, w: OGRN_CONTEXT_RE.search(w) is not None,
        radius=30,
    ),
    Rule(
        "ДАТА_РОЖДЕНИЯ",
        re.compile(rf"{NOT_N_BEFORE}{D}{{1,2}}[.\-/]{D}{{1,2}}[.\-/](?:19|20){D}{{2}}{NOT_N_AFTER}"),
        validate=lambda v, w: BIRTHDATE_CONTEXT_RE.search(w) is not None,
        radius=30,
    ),
    Rule("ЛОГИН", re.compile(r"(?<!\w)@[A-Z0-9_]{3,32}(?!\w)", re.I)),
    Rule("ЛОГИН", re.compile(r"(?:t\.me|telegram\.me|wa\.me|whatsapp\.com)/[A-Z0-9_+\-./]+", re.I)),
    Rule(
        "АДРЕС",
        re.compile(r"(?:[Аа]дрес(?:\s+доставки)?)\s*[:—-]\s*([^\n;.!?]{3,120})"),
        group=1,
        validate=lambda v, w: _has_digit(v) or ADDRESS_HINT_RE.search(v) is not None,
        priority=10,
    ),
    Rule("АДРЕС", re.compile(rf"(?:[Гг]\.|[Гг]ород)\s+[А-ЯЁ]{WORD}(?:\s+[А-ЯЁ]{WORD}){{0,2}}")),
    Rule("АДРЕС", re.compile(rf"(?:[Оо]бл\.|[Оо]бласть|[Кк]рай|[Рр]есп\.|[Рр]еспублика)(?:\s+[А-ЯЁ]{WORD}){{1,3}}")),
    Rule(
        "АДРЕС",
        re.compile(rf"[А-ЯЁ]{WORD}(?:\s+[А-ЯЁ]{WORD})?\s+(?:[Оо]бл\.|[Оо]бласть|[Кк]рай|[Рр]есп\.|[Рр]еспублика)"),
    ),
    Rule(
        "АДРЕС",
        re.compile(
            r"(?:[Уу]л\.?|[Уу]лица|[Пп]р-т|[Пп]роспект|[Пп]ер\.?|[Пп]ереулок|[Бб]-р|[Бб]ульвар|[Шш]оссе|[Шш]\.|"
            r"[Нн]аб\.?|[Нн]абережная|[Пп]л\.?|[Пп]лощадь|[Мм]кр\.?|[Мм]икрорайон)\s+(?:имени\s+|им\.\s*)?"
            rf"[А-ЯЁA-Z0-9](?:{LN}|-)*(?:\s+[А-ЯЁA-Z0-9](?:{LN}|-)*){{0,3}}"
            rf"(?:\s*,\s*(?:(?:[Дд]\.?|[Дд]ом)\s*{ADDR_NUM}|"
            rf"(?:[Кк]в\.?|[Кк]вартира|[Оо]фис|[Пп]ом\.?|[Пп]омещение|[Ээ]таж)\s*{ADDR_NUM}|{ADDR_NUM}))*"
        ),
    ),
    Rule(
        "АДРЕС",
        re.compile(rf"(?:[Дд]\.|[Дд]ом)\s*{ADDR_NUM}"),
        validate=lambda v, w: ADDRESS_HINT_RE.search(w) is not None,
    ),
    Rule(
        "АДРЕС",
        re.compile(rf"(?:[Кк]в\.|[Кк]вартира|[Оо]фис|[Пп]ом\.|[Пп]омещение|[Ээ]таж)\s*{ADDR_NUM}"),
        validate=lambda v, w: ADDRESS_HINT_RE.search(w) is not None,
    ),
    Rule(
        "ИМЯ",
        re.compile(
            r"(?:[Мм]еня зовут|[Зз]овут|[Мм]о[её] имя|[Мм]оя фамилия|ФИО|[Фф]ио|[Кк]онтактное лицо|[Пп]редставлюсь|"
            r"[Оо]т имени|[Пп]рошу связаться с|[Сс]вязаться с|[Оо]бращайтесь к)\s*[:—-]?\s*"
            rf"({NAME_WORD}{{1,30}}(?:\s+{NAME_WORD}{{1,30}}){{0,2}})"
        ),
        group=1,
        priority=15,
    ),
    Rule(
        "ИМЯ",
        re.compile(rf"(?:[Ии]мя)\s*[:—-]\s*({NAME_WORD}{{1,30}}(?:\s+{NAME_WORD}{{1,30}}){{0,2}})"),
        group=1,
        priority=15,
    ),
    Rule(
        "ИМЯ",
        re.compile(
            r"(?:[Фф]амилия|[Кк]лиент|[Зз]аказчик|[Гг]осподин|[Гг]-н\.?|[Гг]ражданин|[Гг]ражданка)\s*[:—-]?\s*"
            rf"({NAME_WORD}{{2,30}})"
        ),
        group=1,
        validate=lambda v, w: (
            v.lower() not in NOT_A_SURNAME and (SURNAME_RE.search(v.lower()) is not None or v.lower() in NAMES)
        ),
        priority=8,
    ),
    Rule(
        "ИМЯ",
        re.compile(rf"(?:^|[.!?]\s+|[Яя]\s+)({NAME_WORD}{{2,30}}\s+{NAME_WORD}{{2,30}}(?:\s+{NAME_WORD}{{2,30}})?)"),
        group=1,
        validate=lambda v, w: looks_like_person_name(v),
    ),
    Rule(
        "ИМЯ",
        re.compile(rf"({NAME_WORD}{{1,30}}\s+{NAME_WORD}{{1,30}})"),
        group=1,
        validate=lambda v, w: looks_like_person_name(v),
    ),
    Rule("ИМЯ", re.compile(rf"{NAME_WORD}{{1,30}}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    Rule("ИМЯ", re.compile(rf"[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+{NAME_WORD}{{1,30}}")),
]

TOKEN_LITERAL_RE = re.compile(rf"\[\s*(?:{TOKEN_TYPE_PATTERN})\s*[_\-\s]?\s*\d+\s*\]", re.I)
TOKEN_RE = re.compile(r"^\[(.+)_(\d+)\]$")


def neutralize_token_literals(text: str) -> str:
    """Метки, которые пользователь вписал сам, не должны совпасть с нашими."""
    return TOKEN_LITERAL_RE.sub("[служебная метка]", text)


def _build_known_rules(known: Iterable[dict] | None) -> list[Rule]:
    rules: list[Rule] = []
    for item in known or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        if item.get("type") not in TOKEN_TYPES:
            continue
        full = value.strip()
        values = [full]
        if item["type"] == "ИМЯ":
            for part in full.split():
                if len(part) >= 3 and part not in values:
                    values.append(part)
        for v in values:
            rules.append(
                Rule(
                    item["type"],
                    re.compile(rf"(?<!{LN}){re.escape(v)}(?!{LN})", re.I),
                    priority=KNOWN_PRIORITY if v == full else KNOWN_PRIORITY - 1,
                )
            )
    return rules


@dataclass
class _Match:
    start: int
    end: int
    type: str
    value: str
    priority: int


def _collect_matches(text: str, rules: list[Rule]) -> list[_Match]:
    found: list[_Match] = []
    for rule in rules:
        for m in rule.re.finditer(text):
            raw = m.group(rule.group)
            if not raw:
                continue
            raw_start = m.start(rule.group)
            value = raw.strip()
            if not value:
                continue
            start = raw_start + max(raw.find(value), 0)
            end = start + len(value)
            window = text[max(0, start - rule.radius) : min(len(text), end + rule.radius)]
            if rule.validate and not rule.validate(value, window):
                continue
            found.append(_Match(start, end, rule.type, value, rule.priority))
    found.sort(key=lambda x: (x.start, -(x.end - x.start), -x.priority))
    chosen: list[_Match] = []
    last_end = -1
    for match in found:
        if match.start < last_end:
            continue
        chosen.append(match)
        last_end = match.end
    return chosen


def _type_stats(types: Iterable[str]) -> list[dict]:
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    return [{"type": t, "label": TYPE_LABELS.get(t, t), "count": c} for t, c in counts.items()]


def _mapping_key(type_: str, value: str) -> str:
    if type_ in ("ТЕЛЕФОН", "КАРТА", "СЧЁТ"):
        digits = _digits(value)
        if digits:
            return f"{type_}:{digits}"
    if type_ == "EMAIL":
        return f"{type_}:{value.lower()}"
    return f"{type_}:{' '.join(value.lower().split())}"


@dataclass
class PiiSession:
    """Таблица «метка → исходное значение», общая для нескольких текстов.

    В JS-оригинале таблица создаётся на один вызов anonymize(). Боту нужно
    маскировать историю, ответы инструментов и т. д. одной таблицей, чтобы
    одинаковые данные получали одинаковую метку во всём запросе к модели.
    """

    mapping: dict[str, dict] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    value_to_token: dict[str, str] = field(default_factory=dict)
    known: list[dict] = field(default_factory=list)
    last_passes: int = 0

    def add_token(self, type_: str, value: str) -> str:
        key = _mapping_key(type_, value)
        if key in self.value_to_token:
            return self.value_to_token[key]
        index = self.counters.get(type_, 0) + 1
        self.counters[type_] = index
        token = f"[{type_}_{index}]"
        self.mapping[token] = {"type": type_, "value": value}
        self.value_to_token[key] = token
        return token

    def anonymize(self, text: Any, known: Iterable[dict] | None = None) -> str:
        source = text if isinstance(text, str) else ""
        current = neutralize_token_literals(source)
        known_rules = _build_known_rules([*self.known, *(known or [])])
        passes = 0
        while passes < 5:
            matches = _collect_matches(current, [*known_rules, *RULES] if passes == 0 else RULES)
            if not matches:
                break
            parts: list[str] = []
            cursor = 0
            for match in matches:
                parts.append(current[cursor : match.start])
                parts.append(self.add_token(match.type, match.value))
                cursor = match.end
            parts.append(current[cursor:])
            current = "".join(parts)
            passes += 1
        self.last_passes = passes
        return current

    def restore(self, text: Any) -> Any:
        return self.restore_obj(text)

    def restore_obj(self, value: Any) -> Any:
        """Как restore_card, но незнакомые метки модели заменяются нейтральной
        фразой и при пустой таблице (в JS restoreText тогда возвращает текст как есть)."""
        if self.mapping:
            return restore_card(value, self.mapping)
        return _walk_strings(value, lambda s: TOKEN_LITERAL_RE.sub("данные клиента", s))

    @property
    def stats(self) -> dict:
        return {
            "total": len(self.mapping),
            "types": _type_stats(item["type"] for item in self.mapping.values()),
            "passes": self.last_passes,
        }


@dataclass
class AnonymizeResult:
    text: str
    mapping: dict[str, dict]
    stats: dict


def anonymize(text: Any, known: Iterable[dict] | None = None) -> AnonymizeResult:
    session = PiiSession()
    out = session.anonymize(text, known=known)
    return AnonymizeResult(text=out, mapping=session.mapping, stats=session.stats)


def find_pii(text: Any) -> dict:
    if not isinstance(text, str) or not text:
        return {"total": 0, "types": []}
    matches = _collect_matches(neutralize_token_literals(text), RULES)
    return {"total": len(matches), "types": _type_stats(m.type for m in matches)}


def restore_text(text: Any, mapping: dict[str, dict] | None) -> Any:
    if not isinstance(text, str) or not isinstance(mapping, dict) or not mapping:
        return text
    result = text
    for token, item in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        parts = TOKEN_RE.match(token)
        if not parts:
            continue
        pattern = re.compile(
            rf"(?:\[\s*)?{re.escape(parts.group(1))}\s*[_\-\s]?\s*{parts.group(2)}(?!\d)(?:\s*\])?", re.I
        )
        value = item["value"]
        result = pattern.sub(lambda _m, v=value: v, result)
    return TOKEN_LITERAL_RE.sub("данные клиента", result)


def _walk_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk_strings(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk_strings(v, fn) for v in value)
    if isinstance(value, dict):
        return {k: _walk_strings(v, fn) for k, v in value.items()}
    return value


def restore_card(card: Any, mapping: dict[str, dict] | None) -> Any:
    if isinstance(card, str):
        return restore_text(card, mapping)
    if isinstance(card, list):
        return [restore_card(v, mapping) for v in card]
    if isinstance(card, tuple):
        return tuple(restore_card(v, mapping) for v in card)
    if isinstance(card, dict):
        return {k: restore_card(v, mapping) for k, v in card.items()}
    return card
