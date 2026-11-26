import struct

from tool import ToolError, run


def record(tag, value):
    return bytes([tag]) + len(value).to_bytes(2, "big") + value


def message(*records, version=1):
    body = bytes([0xA7, version]) + b"".join(records) + bytes([0, 0, 0])
    return body + bytes([sum(body) % 256])


def decode(data):
    return run({"hex": data.hex()})


def refused(data, needle):
    try:
        decode(data) if isinstance(data, bytes) else run(data)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def test_decodes_every_known_type():
    data = message(
        record(1, "héllo".encode()),
        record(2, (258).to_bytes(2, "big")),
        record(3, b"\x01"),
        record(4, struct.pack(">f", 21.5)),
        record(6, b"\xde\xad"),
    )
    assert decode(data) == {
        "version": 1,
        "record_count": 5,
        "records": [
            {"tag": 1, "type": "text", "value": "héllo"},
            {"tag": 2, "type": "uint", "value": 258},
            {"tag": 3, "type": "flag", "value": True},
            {"tag": 4, "type": "real", "value": 21.5},
            {"tag": 6, "type": "bytes", "value": "dead"},
        ],
    }


def test_groups_nest_and_unknown_tags_are_kept_as_hex():
    inner = record(1, b"a") + record(0x7F, b"\x01\x02")
    result = decode(message(record(5, inner)))
    assert result["records"] == [
        {
            "tag": 5,
            "type": "group",
            "value": [
                {"tag": 1, "type": "text", "value": "a"},
                {"tag": 127, "type": "unknown", "value": "0102"},
            ],
        }
    ]


def test_spaces_in_the_hex_are_ignored_and_an_empty_message_has_no_records():
    data = message()
    assert run({"hex": " ".join(f"{b:02X}" for b in data)})["records"] == []


def test_a_bad_checksum_names_both_values():
    data = bytearray(message(record(1, b"x")))
    data[-1] ^= 0xFF
    refused(bytes(data), "checksum")


def test_wrong_magic_wrong_version_and_truncation_are_refused_with_a_position():
    refused(b"\x00" + message()[1:], "magic")
    refused(message(version=2), "version 2")
    short = message(record(1, b"hello"))
    refused(short[:6], "value bytes")
    refused(bytes([0xA7, 1, 1, 0, 0, 5, 0]), "ends inside")
    refused(bytes([0xA7, 1, 1, 0, 9, 0, 0, 0]), "says 9 value bytes but only 3")


def test_bad_values_are_refused():
    refused(message(record(3, b"\x02")), "flag")
    refused(message(record(2, b"")), "uint")
    refused(message(record(4, struct.pack(">f", float("inf")))), "finite")
    refused(message(record(1, b"\xff\xfe")), "UTF-8")


def test_bad_hex_is_refused():
    refused({"hex": "zz"}, "pairs")
    refused({"hex": "abc"}, "pairs")
