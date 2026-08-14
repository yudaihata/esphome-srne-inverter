import json

from wizard import file_sha256, scan_result_is_reusable, yaml_string


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
