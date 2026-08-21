"""Dependency-free localization support for the interactive wizard."""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from string import Formatter
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_ROOT / "locales"
SUPPORTED_LANGUAGES = ("en", "ja")
DEFAULT_LANGUAGE = "en"


def load_messages(language: str, locales_dir: Path = LOCALES_DIR) -> dict[str, str]:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    path = locales_dir / f"{language}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError(f"Invalid translation catalog: {path}")
    return data


def format_fields(message: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in Formatter().parse(message)
        if field_name is not None
    }


def validate_catalogs(locales_dir: Path = LOCALES_DIR) -> list[str]:
    catalogs = {language: load_messages(language, locales_dir) for language in SUPPORTED_LANGUAGES}
    reference = catalogs[DEFAULT_LANGUAGE]
    errors: list[str] = []
    for language, messages in catalogs.items():
        missing = sorted(set(reference) - set(messages))
        extra = sorted(set(messages) - set(reference))
        if missing:
            errors.append(f"{language}: missing keys: {', '.join(missing)}")
        if extra:
            errors.append(f"{language}: extra keys: {', '.join(extra)}")
        for key in sorted(set(reference) & set(messages)):
            expected = format_fields(reference[key])
            actual = format_fields(messages[key])
            if actual != expected:
                errors.append(
                    f"{language}: placeholder mismatch for {key}: "
                    f"expected {sorted(expected)}, got {sorted(actual)}"
                )
    return errors


def detect_system_language(
    env: Mapping[str, str] | None = None,
    locale_name: str | None = None,
) -> str:
    environment = os.environ if env is None else env
    candidates = [
        environment.get("LC_ALL", ""),
        environment.get("LC_MESSAGES", ""),
        environment.get("LANG", ""),
    ]
    if locale_name is None:
        try:
            locale_name = locale.getlocale()[0] or ""
        except (ValueError, TypeError):
            locale_name = ""
    candidates.append(locale_name or "")
    for candidate in candidates:
        normalized = candidate.lower().replace("-", "_")
        if normalized.startswith("ja"):
            return "ja"
        if normalized.startswith("en"):
            return "en"
    return DEFAULT_LANGUAGE


class Translator:
    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self.language = language
        self.messages = load_messages(language)

    def translate(self, key: str, **values: object) -> str:
        try:
            message = self.messages[key]
        except KeyError as exc:
            raise KeyError(f"Missing translation key {key!r} for {self.language}") from exc
        return message.format(**values)


_translator = Translator()


def set_language(language: str) -> None:
    global _translator
    _translator = Translator(language)


def get_language() -> str:
    return _translator.language


def tr(key: str, **values: object) -> str:
    return _translator.translate(key, **values)
