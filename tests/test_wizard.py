import json

import wizard
from wizard import (
    file_sha256,
    parse_args,
    scan_result_is_reusable,
    select_language,
    update_wizard_state,
    yaml_string,
)


def test_scan_reuse_requires_matching_catalog(tmp_path):
    catalog = tmp_path / "catalog.json"
    implemented = tmp_path / "implemented.json"
    catalog.write_text("{}", encoding="utf-8")
    implemented.write_text(json.dumps([{"success": True}]), encoding="utf-8")
    state = {
        "json_catalog_sha256": file_sha256(catalog),
        "slave_id": 1,
        "baudrate": 9600,
    }

    assert scan_result_is_reusable(implemented, state, catalog)
    catalog.write_text('{"changed": true}', encoding="utf-8")
    assert not scan_result_is_reusable(implemented, state, catalog)


def test_yaml_string_escapes_quotes_and_newlines():
    value = 'ssid "quoted"\nsecond line'

    encoded = yaml_string(value)

    assert encoded == '"ssid \\"quoted\\"\\nsecond line"'


def test_update_wizard_state_preserves_existing_values(tmp_path):
    state_path = tmp_path / "wizard_state.json"
    state_path.write_text('{"language": "ja", "port": "/dev/test"}', encoding="utf-8")

    update_wizard_state({"baudrate": 9600}, state_path)

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "language": "ja",
        "port": "/dev/test",
        "baudrate": 9600,
    }


def test_select_language_prefers_explicit_cli_value(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "WIZ_STATE_PATH", tmp_path / "missing.json")

    assert select_language("ja", interactive=False) == "ja"
    assert select_language("en", interactive=False) == "en"


def test_select_language_uses_saved_value_when_flag_is_omitted(monkeypatch, tmp_path):
    state_path = tmp_path / "wizard_state.json"
    state_path.write_text('{"language": "ja"}', encoding="utf-8")
    monkeypatch.setattr(wizard, "WIZ_STATE_PATH", state_path)

    assert select_language(None, interactive=False) == "ja"


def test_select_language_auto_ignores_saved_value(monkeypatch, tmp_path):
    state_path = tmp_path / "wizard_state.json"
    state_path.write_text('{"language": "ja"}', encoding="utf-8")
    monkeypatch.setattr(wizard, "WIZ_STATE_PATH", state_path)
    monkeypatch.setattr(wizard, "detect_system_language", lambda: "en")

    assert select_language("auto", interactive=False) == "en"


def test_select_language_prompts_on_first_interactive_run(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "WIZ_STATE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(wizard, "detect_system_language", lambda: "en")
    monkeypatch.setattr("builtins.input", lambda _: "2")

    assert select_language(None, interactive=True) == "ja"


def test_parse_args_supports_language_selection():
    assert parse_args(["--lang", "en"]).lang == "en"
    assert parse_args(["--lang", "ja"]).lang == "ja"
    assert parse_args(["--lang", "auto"]).lang == "auto"
