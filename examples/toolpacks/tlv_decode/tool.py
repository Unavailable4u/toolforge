"""Decode TLV (Type-Length-Value) binary format."""

from __future__ import annotations

import binascii
from typing import Any

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


def _decode_tlv(data: bytes, schema: dict) -> list[dict[str, Any]]:
    """Decode TLV data according to schema."""
    tags = schema.get("tags", {})
    big_endian = schema.get("big_endian", True)
    fields = []
    i = 0

    while i < len(data):
        if i + 1 >= len(data):
            break
        tag = data[i]
        i += 1
        if i >= len(data):
            break
        length = data[i]
        i += 1
        if i + length > len(data):
            break
        value_bytes = data[i:i + length]
        i += length

        tag_key = f"0x{tag:02X}"
        value_type = tags.get(tag_key, "unknown")

        if value_type == "string":
            value = value_bytes.decode("utf-8", errors="replace")
        elif value_type == "uint32":
            if big_endian:
                value = int.from_bytes(value_bytes, "big")
            else:
                value = int.from_bytes(value_bytes, "little")
        elif value_type == "uint16":
            if big_endian:
                value = int.from_bytes(value_bytes, "big")
            else:
                value = int.from_bytes(value_bytes, "little")
        elif value_type == "bytes":
            value = value_bytes.hex()
        else:
            value = value_bytes.hex()

        fields.append({"tag": tag_key, "type": value_type, "value": str(value)})

    return fields


def run(args: dict) -> dict:
    hex_data = args["hex_data"]
    schema = args["schema"]

    try:
        data = binascii.unhexlify(hex_data)
    except binascii.Error as e:
        raise ToolError(f"invalid hex data: {e}")

    fields = _decode_tlv(data, schema)
    return {"fields": fields}