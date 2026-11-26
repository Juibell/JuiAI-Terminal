"""Request-scoped interface language. Persist canonical records in their original form."""

import re
from contextvars import ContextVar

from . import config
from .messages_zh_cn import MESSAGES

ENGLISH = "en-US"
CHINESE = "zh-CN"
active_language = ContextVar("jui_interface_language", default=ENGLISH)
TEMPLATES = [
    (
        re.compile(
            "(.{1,256}?)".join(re.escape(part) for part in source.split("{value}"))
        ),
        target,
    )
    for source, target in MESSAGES.items()
    if "{value}" in source
]


def saved_language():
    try:
        value = (
            (config.STATE / "ui-language.txt").read_text(encoding="utf-8-sig").strip()
        )
    except (OSError, UnicodeError):
        return ENGLISH
    return CHINESE if value == CHINESE else ENGLISH


def negotiate_language(header):
    if not header:
        return saved_language()
    best = ENGLISH
    highest = 0.0
    for item in header.split(","):
        tag, _, weight = item.strip().lower().partition(";")
        if (
            tag in {"zh", "zh-cn", "zh-sg"}
            or tag == "zh-hans"
            or tag.startswith("zh-hans-")
        ):
            supported = CHINESE
        elif tag == "*" or tag == "en" or tag.startswith("en-"):
            supported = ENGLISH
        else:
            continue
        if weight:
            name, _, value = weight.strip().partition("=")
            if name != "q":
                continue
            try:
                quality = float(value)
            except ValueError:
                continue
        else:
            quality = 1.0
        if highest < quality <= 1.0:
            highest, best = quality, supported
    return best


def translate(text, language=None):
    if (language or active_language.get()) != CHINESE or not isinstance(text, str):
        return text
    if text in MESSAGES:
        return MESSAGES[text]
    if len(text) <= 1024:
        for pattern, target in TEMPLATES:
            match = pattern.fullmatch(text)
            if match:
                return target.format(*match.groups())
    return text


def localize_error(record):
    """Translate only the service error field, never user input or model output."""
    if record.get("error"):
        return {**record, "error": translate(record["error"])}
    return record


def localize_update(record):
    record = localize_error(record)
    result = record.get("result")
    if not result or active_language.get() != CHINESE:
        return record
    return {
        **record,
        "result": {
            **result,
            "deferred": [translate(note) for note in result.get("deferred", [])],
            "failed": [localize_error(item) for item in result.get("failed", [])],
        },
    }
