from tool import ToolError, run


def test_decode_tlv_string():
    # Tag 0x01 (string), length 5, value "Hello"
    hex_data = "010548656C6C6F"
    schema = {
        "type": "TLV",
        "big_endian": True,
        "tags": {"0x01": "string"}
    }
    result = run({"hex_data": hex_data, "schema": schema})
    assert result["fields"] == [
        {"tag": "0x01", "type": "string", "value": "Hello"}
    ]


def test_decode_tlv_uint32():
    # Tag 0x02 (uint32), length 4, value 0x0000002A (42)
    hex_data = "02040000002A"
    schema = {
        "type": "TLV",
        "big_endian": True,
        "tags": {"0x02": "uint32"}
    }
    result = run({"hex_data": hex_data, "schema": schema})
    assert result["fields"] == [
        {"tag": "0x02", "type": "uint32", "value": "42"}
    ]


def test_decode_multiple_fields():
    # Tag 0x01 (string) "Hi", Tag 0x02 (uint32) 42
    hex_data = "0102486902040000002A"
    schema = {
        "type": "TLV",
        "big_endian": True,
        "tags": {"0x01": "string", "0x02": "uint32"}
    }
    result = run({"hex_data": hex_data, "schema": schema})
    assert result["fields"] == [
        {"tag": "0x01", "type": "string", "value": "Hi"},
        {"tag": "0x02", "type": "uint32", "value": "42"},
    ]


def test_decode_unknown_tag():
    hex_data = "0302ABCD"
    schema = {
        "type": "TLV",
        "big_endian": True,
        "tags": {}
    }
    result = run({"hex_data": hex_data, "schema": schema})
    assert result["fields"] == [
        {"tag": "0x03", "type": "unknown", "value": "abcd"}
    ]