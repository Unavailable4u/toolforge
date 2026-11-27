"""`tlv_convert` (tier 1): turn a binary tag-length-value file into JSON, following a written spec."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, rng_for

ID = "tlv_convert"
MAGIC = (b"TLV1", b"TLV2")

SPEC = """\
readings.bin starts with 4 magic bytes ({magic}), then a sequence of elements. Each element is:

    tag     1 byte
    length  2 bytes, unsigned, {endian} byte order
    value   <length> bytes

Top-level tags: 0x10 = a reading, whose value is itself a sequence of elements; 0xFF = padding, skip it. Inside a reading:
0x01 = id (unsigned 32-bit, {endian}), 0x02 = name (UTF-8 text), 0x03 = temperature in tenths of a degree (signed 16-bit, {endian}).
Any other tag, at either level, is unknown: skip it using its length."""

PROMPT = """\
You are given the binary file readings.bin (in inputs/). Its format:

{spec}

Convert it. Answer with one JSON object of exactly this shape, readings in file order:
{{"readings": [{{"id": <int>, "name": "<text>", "temp_dc": <int>}}, ...]}}"""


def _element(tag: int, value: bytes, order: str) -> bytes:
    return struct.pack(f"{order}BH", tag, len(value)) + value


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    order = "<" if sibling else ">"
    endian = "little-endian" if sibling else "big-endian"
    magic = MAGIC[1 if sibling else 0]
    body = bytearray(magic)
    readings = []
    for _ in range(rng.randint(8, 16)):
        reading = {
            "id": rng.randint(1, 2**32 - 1),
            "name": rng.choice(("boiler", "attic", "cellar", "café", "Gewächshaus", "porch"))
            + f"-{rng.randint(1, 99)}",
            "temp_dc": rng.randint(-400, 950),
        }
        parts = [
            _element(0x01, struct.pack(f"{order}I", reading["id"]), order),
            _element(0x02, reading["name"].encode("utf-8"), order),
            _element(0x03, struct.pack(f"{order}h", reading["temp_dc"]), order),
        ]
        if rng.random() < 0.4:
            parts.insert(
                rng.randint(0, 3),
                _element(rng.choice((0x04, 0x2A, 0x7E)), rng.randbytes(rng.randint(0, 12)), order),
            )
        rng.shuffle(parts)
        body += _element(0x10, b"".join(parts), order)
        readings.append(reading)
        if rng.random() < 0.25:
            body += _element(0xFF, bytes(rng.randint(0, 6)), order)
        if rng.random() < 0.15:
            body += _element(0x55, rng.randbytes(rng.randint(1, 9)), order)
    spec = SPEC.format(magic=magic.decode("ascii"), endian=endian)
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(spec=spec),
        files={"readings.bin": bytes(body)},
        expected={"readings": readings},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None or not isinstance(data.get("readings"), list):
        return Check(False, "the answer is not an object with a 'readings' list")
    expected, given = instance.expected["readings"], data["readings"]
    if len(given) != len(expected):
        return Check(False, f"expected {len(expected)} readings, got {len(given)}")
    for index, (want, got) in enumerate(zip(expected, given, strict=True)):
        if not isinstance(got, dict) or {k: got.get(k) for k in want} != want:
            return Check(False, f"reading {index} differs: expected {want}, got {got!r}"[:300])
    return Check(True, "every reading matches")


TASK = Task(
    id=ID,
    tier="1",
    title="Binary TLV to JSON from a written spec",
    capability="decode a nested binary tag-length-value format, skipping unknown tags",
    generate=generate,
    check=check,
    has_sibling=True,
)
