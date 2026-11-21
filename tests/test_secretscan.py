"""`verify.secretscan`: each named pattern is found, near misses and ordinary data are not, and the matched text never leaves.

Every secret-shaped value in this file is assembled from pieces at run time. A literal that looks like a token would be flagged by
secret scanners on the hosting platform, and a test must never hold something that could be mistaken for a real key.
"""

from __future__ import annotations

import dataclasses
import json
import time

import pytest

from toolforge.verify import secretscan
from toolforge.verify.secretscan import PATTERN_NAMES, Finding, scan


def aws() -> str:
    return "AKIA" + "QWERTYUIOP" + "ASDFGH"  # AKIA + 16


def github(prefix: str = "ghp") -> str:
    return prefix + "_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"  # 36 characters after the underscore


def fine_grained() -> str:
    return "github" + "_pat_" + "11ABCDEFG0" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"


def slack(kind: str = "b") -> str:
    return "xox" + kind + "-" + "123456789012" + "-" + "abcdefghijklmnopqrstuvwx"


def google() -> str:
    return "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"  # AIza + 35


def pem() -> str:
    return "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def assignment(name: str = "api_key", value: str = "Zx9Qp3Lm7Vb2Nc8Td5Hw1K") -> str:
    return f'{name} = "{value}"'


POSITIVES = {
    "aws_access_key_id": lambda: f"aws_id = '{aws()}'",
    "github_token": lambda: f"token_text = '{github()}'",
    "github_fine_grained_token": lambda: f"x = '{fine_grained()}'",
    "slack_token": lambda: f"hook = '{slack()}'",
    "google_api_key": lambda: f"maps = '{google()}'",
    "private_key_header": lambda: f"{pem()}\nMIIB...\n",
    "secret_assignment": lambda: assignment(),
}


def test_every_pattern_has_a_positive_case_here() -> None:
    assert set(POSITIVES) == set(PATTERN_NAMES)  # a new pattern needs a test in this file


@pytest.mark.parametrize("name", PATTERN_NAMES)
def test_each_pattern_is_detected(name: str) -> None:
    findings = scan(POSITIVES[name](), "tool.py")
    assert name in {f.pattern for f in findings}
    assert all(f.file == "tool.py" for f in findings)


@pytest.mark.parametrize("prefix", ["ghp", "gho", "ghu", "ghs", "ghr"])
def test_every_github_prefix_is_detected(prefix: str) -> None:
    assert [f.pattern for f in scan(github(prefix))] == ["github_token"]


@pytest.mark.parametrize("kind", list("baprs"))
def test_every_slack_kind_is_detected(kind: str) -> None:
    assert [f.pattern for f in scan(slack(kind))] == ["slack_token"]


@pytest.mark.parametrize(
    "keyword", ["key", "token", "secret", "password", "passwd", "API_KEY", "client_secret"]
)
def test_the_assignment_pattern_covers_the_named_words(keyword: str) -> None:
    assert [f.pattern for f in scan(assignment(keyword))] == ["secret_assignment"]


def test_assignment_forms_json_and_single_quotes() -> None:
    assert scan('{"auth_token": "Zx9Qp3Lm7Vb2Nc8Td5Hw1K"}')
    assert scan("password='Zx9Qp3Lm7Vb2Nc8Td5Hw1K'")
    assert scan("SECRET:'Zx9Qp3Lm7Vb2Nc8Td5Hw1K'")


NEAR_MISSES = [
    ("aws id one character short", "AKIA" + "QWERTYUIOP" + "ASDFG"),
    ("aws id one character long", aws() + "Q"),
    ("aws id inside a longer word", "x" + aws()),
    ("github token too short", "ghp_" + "a1B2c3D4e5F6"),
    ("github token with an unknown prefix", "ghx_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"),
    ("fine grained token too short", "github" + "_pat_short"),
    ("slack token with an unknown kind", "xoxz-" + "123456789012-abcdefghijkl"),
    ("slack token too short", "xoxb-" + "12345"),
    ("google key one character short", "AI" + "zaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"),
    ("a public key header", "-----BEGIN PUBLIC KEY-----"),
    ("a certificate header", "-----BEGIN CERTIFICATE-----"),
    ("value one character short", assignment(value="Zx9Qp3Lm7Vb2Nc8Td5H")),
    ("value of one character class", assignment(value="abcdefghijklmnopqrstuvwxyz")),
    ("value that repeats", assignment(value="a1" * 15)),
    ("a placeholder", assignment(value="YourApiKeyGoesHere1234567890")),
    ("the word example", assignment(value="Example1Example2Example3Example4")),
    ("no keyword in the name", assignment(name="title", value="Zx9Qp3Lm7Vb2Nc8Td5Hw1K")),
    ("a value that is not quoted", "api_key = Zx9Qp3Lm7Vb2Nc8Td5Hw1K"),
]


@pytest.mark.parametrize(("why", "text"), NEAR_MISSES, ids=[why for why, _ in NEAR_MISSES])
def test_near_misses_are_not_flagged(why: str, text: str) -> None:
    assert scan(text) == [], why


ORDINARY_DATA = {
    "an md5 digest": "d41d8cd98f00b204e9800998ecf8427e",
    "a sha1 digest": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
    "a sha256 digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "an upper-case sha256 digest": "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855",
    "a uuid": "123e4567-e89b-12d3-a456-426614174000",
    "a git commit id": "f6402bcf02a5f8cc7dcc211e278c13e63f636a4f",
}


@pytest.mark.parametrize("why", ORDINARY_DATA)
def test_ordinary_hashes_and_uuids_are_not_flagged_even_when_named_like_a_key(why: str) -> None:
    value = ORDINARY_DATA[why]
    for name in ("expected", "cache_key", "token", "secret_hash"):
        assert scan(f'{name} = "{value}"') == [], f"{why} as {name}"
    assert scan(f"the value is {value}") == []


def test_base64_test_data_is_not_flagged() -> None:
    blob = "SGVsbG8sIHdvcmxkISBUaGlzIGlzIHNvbWUgYmFzZTY0IHRlc3QgZGF0YS4="
    assert scan(f'data = "{blob}"') == []
    assert scan(f'expected_output = "{blob}"') == []
    assert scan(f"payload: {blob}\nother: {blob}") == []


def test_ordinary_source_and_prose_are_not_flagged() -> None:
    code = (
        "import csv\n\n"
        "def run(args):\n"
        "    keys = sorted(args)\n"
        "    token_count = len(args['text'].split())\n"
        "    password_rules = 'at least twelve characters, mixed case'\n"
        "    return {'keys': keys, 'tokens': token_count}\n"
    )
    assert scan(code) == []


def test_line_numbers_count_from_one_and_are_reported_in_order() -> None:
    text = "one\n\n" + f"a = '{aws()}'\n" + "four\n" + f"b = '{github()}'\n"
    assert [(f.pattern, f.line) for f in scan(text, "x.py")] == [
        ("aws_access_key_id", 3),
        ("github_token", 5),
    ]


def test_one_finding_per_pattern_and_line() -> None:
    assert len(scan(f"{aws()} {aws()} {aws()}")) == 1
    assert len(scan(f"{aws()}\n{aws()}")) == 2


def test_the_file_label_is_only_copied() -> None:
    assert scan(aws(), "tests/test_tool.py")[0].file == "tests/test_tool.py"
    assert scan(aws())[0].file == ""


def test_bytes_and_none_are_accepted() -> None:
    assert [f.pattern for f in scan(aws().encode())] == ["aws_access_key_id"]
    assert scan(None) == []
    assert scan(b"\xff\xfe not text") == []


# --- The matched text never leaves ---------------------------------------------------------------------------------------------


def test_a_finding_has_no_field_that_could_hold_the_text() -> None:
    assert [f.name for f in dataclasses.fields(Finding)] == ["pattern", "file", "line"]


@pytest.mark.parametrize("name", PATTERN_NAMES)
def test_the_matched_text_is_in_no_rendering_of_a_finding(name: str) -> None:
    text = POSITIVES[name]()
    secret = {
        "aws_access_key_id": aws(),
        "github_token": github(),
        "github_fine_grained_token": fine_grained(),
        "slack_token": slack(),
        "google_api_key": google(),
        "private_key_header": pem(),
        "secret_assignment": "Zx9Qp3Lm7Vb2Nc8Td5Hw1K",
    }[name]
    for finding in scan(text, "tool.py"):
        rendered = [
            repr(finding),
            str(finding),
            json.dumps(finding.as_dict()),
            repr(dataclasses.asdict(finding)),
        ]
        assert all(secret not in item for item in rendered)


def test_the_scan_does_not_echo_text_through_exceptions_or_logging(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"):
        scan(assignment())
    assert "Zx9Qp3Lm7Vb2Nc8Td5Hw1K" not in caplog.text


# --- Limits ----------------------------------------------------------------------------------------------------------------------


def test_a_text_full_of_tokens_gives_a_bounded_report() -> None:
    text = "\n".join(f"v{n} = '{aws()}'" for n in range(secretscan.MAX_FINDINGS * 3))
    assert len(scan(text)) == secretscan.MAX_FINDINGS


def test_hostile_text_is_scanned_in_bounded_time() -> None:
    started = time.monotonic()
    scan("key" * 400_000)
    scan("a" * 3_000_000)
    scan('key = "' + "a1B2" * 400_000)
    scan("-----BEGIN " + "A " * 300_000)
    scan("xoxb-" + "1" * 1_000_000)
    scan(("token" + " " * 50 + "\n") * 50_000)
    assert time.monotonic() - started < 30


def test_text_beyond_the_scan_limit_is_not_read() -> None:
    assert scan("x" * secretscan.MAX_SCAN_CHARS + aws()) == []


def test_the_pattern_table_lists_every_pattern() -> None:
    table = secretscan.patterns_markdown()
    assert all(f"`{name}`" in table for name in PATTERN_NAMES)
