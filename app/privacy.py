"""Маскировка персональных данных (152-ФЗ).

Адаптировано из lead-speed-agent/app/privacy.py: телефоны и email заменяются
метками перед отправкой текста в модель и в каждой записи лога.
Сами контакты остаются в локальной базе.
"""

from __future__ import annotations

import logging
import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Российские номера (+7 / 8 / 7 / без кода) и международные с «+».
# Даты вида 2026-10-20 15:00 и VK ID в ссылках не захватываются.
PHONE_RE = re.compile(
    r"(?<![\w+])(?:"
    r"(?:\+7|8|7)?[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
    r"|\+\d{1,3}[\s\-(]*\d[\d\s\-()]{7,14}\d"
    r")(?!\w)"
)

PHONE_LABEL = "[телефон]"
EMAIL_LABEL = "[email]"


def mask_pii(text: str) -> str:
    """Replace emails and phone numbers with labels."""
    if not text:
        return text
    out = EMAIL_RE.sub(EMAIL_LABEL, text)
    return PHONE_RE.sub(PHONE_LABEL, out)


def extract_phone(text: str) -> str | None:
    """Return the last phone number found in text, normalized to digits (with leading +)."""
    if not text:
        return None
    matches = PHONE_RE.findall(text)
    if not matches:
        return None
    raw = matches[-1]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


class PiiLogFilter(logging.Filter):
    """logging-фильтр: маскирует email и телефоны в любых записях лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg, record.args = mask_pii(msg), ()
        return True


def install_log_filter() -> None:
    log_filter = PiiLogFilter()
    root = logging.getLogger()
    root.addFilter(log_filter)
    for handler in root.handlers:
        handler.addFilter(log_filter)
