from tools.range_builder import build_ranges


def test_legacy_32bit_result_without_pair_flag_is_retained():
    result = build_ranges([
        {
            "success": True,
            "group": "P09",
            "address": 61482,
            "name": "Total Energy",
            "data_type": "uint32",
        }
    ])

    assert result["ranges"]["P09"][0]["addresses"] == [61482]


def test_explicitly_failed_32bit_pair_is_excluded():
    result = build_ranges([
        {
            "success": True,
            "group": "P09",
            "address": 61482,
            "name": "Total Energy",
            "data_type": "uint32",
            "pair_exists": False,
        }
    ])

    assert "P09" not in result["ranges"]


def test_ranges_never_exceed_max_chunk():
    records = [
        {"success": True, "group": "P02", "address": address, "name": f"R{address}", "data_type": "uint16"}
        for address in range(512, 580)
    ]

    result = build_ranges(records, max_chunk=32)

    assert all(chunk["size"] <= 32 for chunk in result["ranges"]["P02"])
