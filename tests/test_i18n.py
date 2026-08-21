import ast
import json
from pathlib import Path

import pytest

from tools.i18n import (
    LOCALES_DIR,
    Translator,
    detect_system_language,
    validate_catalogs,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_translation_catalogs_have_matching_keys_and_placeholders():
    assert validate_catalogs() == []


def test_every_translation_key_is_used_by_the_wizard():
    tree = ast.parse((PROJECT_ROOT / "wizard.py").read_text(encoding="utf-8"))
    used = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tr"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    english = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))

    assert used == set(english)


@pytest.mark.parametrize(
    ("environment", "locale_name", "expected"),
    [
        ({"LANG": "ja_JP.UTF-8"}, "en_US", "ja"),
        ({"LC_ALL": "en_GB.UTF-8", "LANG": "ja_JP.UTF-8"}, "ja_JP", "en"),
        ({}, "ja_JP", "ja"),
        ({"LANG": "de_DE.UTF-8"}, "de_DE", "en"),
    ],
)
def test_detect_system_language(environment, locale_name, expected):
    assert detect_system_language(environment, locale_name) == expected


def test_translator_formats_values():
    translator = Translator("ja")

    assert translator.translate("discovery.using_port", port="/dev/cu.test") == (
        "使用ポート: /dev/cu.test"
    )
