# Recipe: a tool that reads a file format

This is documentation written by the operator of this ToolForge, not output from a tool. Read `package-format` first. The runtime image
has `struct`, `csv`, `json`, `zipfile`, `base64` (standard library) and `PyYAML` (`yaml`), `python-dateutil` (`dateutil`),
`beautifulsoup4` with `lxml`, and `Pillow` (`PIL`). Call `forge_runtime_info` for the versions. You never list them in `requirements`.

## Work from the spec, not from guesses

A parser written from a spec is a table of rules; write the spec first, as the docstring at the top of `tool.py`, and make each rule
one small function with one test. When the task gives you a written format (header, field widths, byte order, checksum), copy its
numbers exactly; when it gives only sample data, say so in the tool's `description` and refuse what the samples do not show.

## Rules

1. **A tool call carries text.** Take binary data as hex (`bytes.fromhex(text)`; ignore spaces) or base64 (`base64.b64decode(text,
   validate=True)`), and say which in the input schema's description. A binary *file* arrives only through `permissions.inputs`.
2. **Never trust a length.** Check that `offset + length <= len(data)` before slicing; a record that says it has 60000 bytes when 3 are
   left is a `ToolError` that names the byte offset, not an `IndexError`.
3. **Name the byte order.** `struct.unpack(">H", ...)` for big-endian, `"<"` for little-endian, and say so in the spec. Do not use native
   order (`"@"`).
4. **Verify what the format lets you verify** (magic, version, checksum) and refuse a mismatch with both values in the message.
5. **Cap everything**: input size (`maxLength` in the schema), number of records, nesting depth, and the length of any text you return.
6. **JSON only comes out.** Bytes you do not decode are returned as hex (cut at some length); a float that is `inf` or `nan` is an error
   or text, never a number.
7. **Parse untrusted text safely.** `yaml.safe_load`, never `yaml.load`. `zipfile` reads without extracting (`ZipFile.read`), and check
   `file_size` of every member before reading it (a zip bomb). Give `BeautifulSoup` the parser name (`"html.parser"` or `"lxml"`).

## A complete small package: decode a frame

The format, as the task would give it: `FR` (2 bytes, ASCII), version (1 byte, must be 1), count (2 bytes, big-endian), then `count`
readings of 5 bytes each: a channel number (1 byte) and a value (4 bytes, IEEE 754 single, big-endian). Nothing follows the readings.

```json manifest
{
  "name": "frame_decode",
  "version": "1.0.0",
  "kind": "stateless",
  "description": "Decode an FR frame given as hex: magic FR, version 1, a big-endian count, then count readings of channel and float32 value.",
  "when_to_use": "The task gives a hex dump of an FR frame and asks for the readings in it.",
  "entrypoint": "tool.py:run",
  "input_schema": {
    "type": "object",
    "properties": {"hex": {"type": "string", "maxLength": 4000}},
    "required": ["hex"],
    "additionalProperties": false
  },
  "examples": [
    {"input": {"hex": "4652 01 0002 07 41a80000 09 c0000000"}, "output": {"readings": [{"channel": 7, "value": 21.0}, {"channel": 9, "value": -2.0}]}}
  ]
}
```

```python tool.py
import math
import struct

from toolforge_sdk import ToolError


def run(args):
    text = "".join(args["hex"].split())
    try:
        data = bytes.fromhex(text)
    except ValueError:
        raise ToolError("hex must be pairs of hexadecimal digits") from None
    if len(data) < 5:
        raise ToolError("a frame has at least 5 bytes: FR, version and count")
    if data[:2] != b"FR":
        raise ToolError("the frame must start with the bytes FR")
    if data[2] != 1:
        raise ToolError(f"version {data[2]} is not supported; only 1 is")
    (count,) = struct.unpack(">H", data[3:5])
    if len(data) != 5 + count * 5:
        raise ToolError(f"the count says {count} readings ({5 + count * 5} bytes) but the frame has {len(data)} bytes")
    readings = []
    for index in range(count):
        at = 5 + index * 5
        channel, value = struct.unpack(">Bf", data[at : at + 5])
        if not math.isfinite(value):
            raise ToolError(f"the reading at byte {at} is not a finite number")
        readings.append({"channel": channel, "value": value})
    return {"readings": readings}
```

```python tests/test_frame_decode.py
import struct

from tool import ToolError, run


def frame(*readings, count=None):
    body = b"FR\x01" + struct.pack(">H", len(readings) if count is None else count)
    return (body + b"".join(struct.pack(">Bf", c, v) for c, v in readings)).hex()


def refused(hex_text, needle):
    try:
        run({"hex": hex_text})
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def test_decodes_readings():
    assert run({"hex": frame((1, 1.5), (2, -4.0))}) == {
        "readings": [{"channel": 1, "value": 1.5}, {"channel": 2, "value": -4.0}]
    }


def test_a_wrong_count_names_both_sizes():
    refused(frame((1, 1.5), count=3), "3 readings")


def test_bad_magic_version_and_non_finite_values_are_refused():
    refused("4a52" + frame()[4:], "start with")
    refused("465202" + frame()[6:], "version 2")
    refused(frame((1, float("inf"))), "finite")
```
