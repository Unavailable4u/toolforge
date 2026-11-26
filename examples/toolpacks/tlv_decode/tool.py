"""Decode a TLV-7 message, a made-up binary format used to show how a tool parses a format from a written spec.

TLV-7, as the task would give it:

    message  = MAGIC VERSION record* END CHECKSUM
    MAGIC    = 0xA7
    VERSION  = one byte, 0x01 is the only version
    record   = TAG (1 byte) LENGTH (2 bytes, big-endian) VALUE (LENGTH bytes)
    END      = a record with TAG 0x00 and LENGTH 0, that is the three bytes 00 00 00
    CHECKSUM = one byte: the sum of every byte before it, modulo 256

    tag 0x01 text   VALUE is UTF-8
    tag 0x02 uint   VALUE is 1 to 8 bytes, big-endian, unsigned
    tag 0x03 flag   VALUE is one byte, 0 or 1
    tag 0x04 real   VALUE is 4 bytes, an IEEE 754 single, big-endian
    tag 0x05 group  VALUE is itself a list of records (no magic, no END, no checksum), nested at most 4 deep
    tag 0x06 bytes  VALUE is raw bytes, shown as hex
    any other tag   shown as type "unknown" with its bytes as hex

The caller gives the message as hex text (spaces allowed) because a tool call carries text. Anything wrong with the message is a
ToolError that says what and where, so the caller can fix the input.
"""

from __future__ import annotations

import math
import re
import struct

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


MAGIC = 0xA7
SUPPORTED_VERSION = 1
MAX_DEPTH = 4
MAX_RECORDS = 500
MAX_HEX_CHARS = 200
_HEX = re.compile(r"[0-9a-fA-F]*")


def _shown(data: bytes) -> str:
    text = data[: MAX_HEX_CHARS // 2].hex()
    return text + ("..." if len(data) > MAX_HEX_CHARS // 2 else "")


def _value(tag: int, data: bytes, offset: int, depth: int, budget: list[int]):
    if tag == 0x01:
        try:
            return "text", data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"the text record at byte {offset} is not valid UTF-8") from None
    if tag == 0x02:
        if not 1 <= len(data) <= 8:
            raise ToolError(f"the uint record at byte {offset} has {len(data)} value bytes; it needs 1 to 8")
        return "uint", int.from_bytes(data, "big")
    if tag == 0x03:
        if len(data) != 1 or data[0] > 1:
            raise ToolError(f"the flag record at byte {offset} must be one byte, 0 or 1")
        return "flag", bool(data[0])
    if tag == 0x04:
        if len(data) != 4:
            raise ToolError(f"the real record at byte {offset} has {len(data)} value bytes; it needs 4")
        number = struct.unpack(">f", data)[0]
        if not math.isfinite(number):
            raise ToolError(f"the real record at byte {offset} is not a finite number")
        return "real", number
    if tag == 0x05:
        if depth >= MAX_DEPTH:
            raise ToolError(f"groups are nested more than {MAX_DEPTH} deep at byte {offset}")
        return "group", _records(data, offset, depth + 1, budget, top=False)
    if tag == 0x06:
        return "bytes", _shown(data)
    return "unknown", _shown(data)


def _records(data: bytes, base: int, depth: int, budget: list[int], *, top: bool):
    """The records in `data`. At the top level reading stops at the END record and returns where it was; inside a group, at the end."""
    found, at = [], 0
    while at < len(data):
        if at + 3 > len(data):
            raise ToolError(f"the message ends inside a record header at byte {base + at}")
        tag = data[at]
        length = int.from_bytes(data[at + 1 : at + 3], "big")
        if top and tag == 0x00:
            if length != 0:
                raise ToolError(f"the END record at byte {base + at} must have length 0")
            return found, at + 3
        end = at + 3 + length
        if end > len(data):
            raise ToolError(
                f"the record at byte {base + at} says {length} value bytes but only {len(data) - at - 3} are left"
            )
        budget[0] += 1
        if budget[0] > MAX_RECORDS:
            raise ToolError(f"the message has more than {MAX_RECORDS} records")
        kind, value = _value(tag, data[at + 3 : end], base + at, depth, budget)
        found.append({"tag": tag, "type": kind, "value": value})
        at = end
    if top:
        raise ToolError("the message has no END record (00 00 00)")
    return found


def run(args: dict) -> dict:
    text = "".join(args["hex"].split())
    if not _HEX.fullmatch(text) or len(text) % 2:
        raise ToolError("hex must be pairs of hexadecimal digits (spaces are ignored)")
    data = bytes.fromhex(text)
    if len(data) < 6:
        raise ToolError(
            "the message is too short: it needs magic, version, END and a checksum (6 bytes at least)"
        )
    if data[0] != MAGIC:
        raise ToolError(f"the first byte is 0x{data[0]:02x}, not the magic 0xa7")
    if data[1] != SUPPORTED_VERSION:
        raise ToolError(f"version {data[1]} is not supported; only version 1 is")
    records, used = _records(data[2:], 2, 0, [0], top=True)
    end = 2 + used
    if end + 1 != len(data):
        raise ToolError(
            f"{len(data) - end - 1} unexpected bytes follow the END record"
            if end + 1 < len(data)
            else "the checksum byte is missing"
        )
    expected = sum(data[:end]) % 256
    if data[end] != expected:
        raise ToolError(f"checksum is 0x{data[end]:02x} but the bytes add up to 0x{expected:02x}")
    return {"version": data[1], "records": records, "record_count": len(records)}
