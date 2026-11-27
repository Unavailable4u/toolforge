"""`yaml_diff` (tier 1): a semantic diff of two YAML files under a written rule set.

The files use a small YAML subset (block mappings, flow lists, double-quoted strings, integers, `true`/`false`, comments) written by this
module, so the checker never parses YAML: it compares the data the files were rendered from.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, rng_for

ID = "yaml_diff"
KEYS = (
    ("server", "cache", "auth", "queue", "logging", "limits"),
    ("db", "mailer", "storage", "scheduler", "metrics", "features"),
)
LEAVES = ("host", "port", "enabled", "timeout_s", "mode", "retries", "tags_set", "order", "level", "owner")

RULES = """\
1. Compare by key path: keys joined with '.', for example server.port. Key order and comments do not matter.
2. A path only in new.yaml is added; only in old.yaml is removed; in both with different values is changed.
3. When a whole mapping exists in only one file, report its own path once (do not list the keys inside it).
4. Lists are compared in order as one value, except under a key whose name ends in '_set': those compare as sets (order and
   repeated items do not matter).
5. Ignore everything under the top-level key 'metadata'.
6. Values of different types are different: 1 and "1" differ, true and "true" differ."""

PROMPT = """\
You are given old.yaml and new.yaml (in inputs/). Diff them under these rules:

{rules}

Answer with one JSON object of exactly this shape, each list sorted alphabetically:
{{"added": ["<path>", ...], "removed": ["<path>", ...], "changed": ["<path>", ...]}}"""


def _scalar(rng, leaf: str) -> Any:
    if leaf.endswith("_set") or leaf == "order":
        return [rng.choice(("a", "b", "c", "d", "e", "f")) for _ in range(rng.randint(2, 4))]
    if leaf in ("enabled",):
        return rng.choice((True, False))
    if leaf in ("port", "timeout_s", "retries"):
        return rng.randint(1, 9000)
    return rng.choice(("alpha", "beta", "gamma", "on", "yes", "1.0", "prod", "dev"))


def _tree(rng, top: tuple[str, ...]) -> dict[str, Any]:
    data: dict[str, Any] = {"metadata": {"generated": rng.randint(1, 10**6), "by": "exporter"}}
    for key in rng.sample(top, k=4):
        section = {leaf: _scalar(rng, leaf) for leaf in rng.sample(LEAVES, k=rng.randint(3, 5))}
        if rng.random() < 0.4:
            section["nested"] = {leaf: _scalar(rng, leaf) for leaf in rng.sample(LEAVES, k=2)}
        data[key] = section
    return data


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(data: Mapping[str, Any], rng, indent: int = 0) -> list[str]:
    lines: list[str] = []
    keys = list(data)
    rng.shuffle(keys)
    for key in keys:
        value = data[key]
        if rng.random() < 0.1:
            lines.append(" " * indent + f"# {rng.choice(('note', 'todo: review', 'set by ops'))}")
        if isinstance(value, dict):
            lines.append(" " * indent + f"{key}:")
            lines += render(value, rng, indent + 2)
        else:
            lines.append(" " * indent + f"{key}: {_render_value(value)}")
    return lines


def _same(path: str, old: Any, new: Any) -> bool:
    if type(old) is not type(new):
        return False
    if isinstance(old, list) and path.rsplit(".", 1)[-1].endswith("_set"):
        return set(map(_render_value, old)) == set(map(_render_value, new))
    return old == new


def diff(old: Mapping[str, Any], new: Mapping[str, Any], prefix: str = "") -> dict[str, list[str]]:
    """The reference implementation of the rules above."""
    out: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}
    for key in sorted(set(old) | set(new)):
        if not prefix and key == "metadata":
            continue
        path = f"{prefix}{key}"
        if key not in old:
            out["added"].append(path)
        elif key not in new:
            out["removed"].append(path)
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            inner = diff(old[key], new[key], path + ".")
            for kind in out:
                out[kind] += inner[kind]
        elif not _same(path, old[key], new[key]):
            out["changed"].append(path)
    return {kind: sorted(paths) for kind, paths in out.items()}


def _mutate(rng, data: dict[str, Any], top: tuple[str, ...]) -> dict[str, Any]:
    new = copy.deepcopy(data)
    new["metadata"]["generated"] += rng.randint(1, 999)  # ignored by rule 5
    sections = [k for k in new if k != "metadata"]
    for key in sections:
        section = new[key]
        for leaf in list(section):
            roll = rng.random()
            value = section[leaf]
            if isinstance(value, dict):
                continue
            if roll < 0.12:
                del section[leaf]
            elif roll < 0.22 and isinstance(value, int) and not isinstance(value, bool):
                section[leaf] = str(value)  # same digits, different type
            elif roll < 0.32:
                section[leaf] = _scalar(rng, leaf)
            elif roll < 0.45 and isinstance(value, list):
                rng.shuffle(value)  # a change only when the key does not end in _set
        if rng.random() < 0.3:
            spare = [leaf for leaf in LEAVES if leaf not in section]
            if spare:
                leaf = rng.choice(spare)
                section[leaf] = _scalar(rng, leaf)
    unused = [k for k in top if k not in new]
    if unused and rng.random() < 0.7:
        new[rng.choice(unused)] = {"enabled": True, "mode": "new"}
    if rng.random() < 0.5:
        del new[rng.choice(sections)]
    return new


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    top = KEYS[1 if sibling else 0]
    old = _tree(rng, top)
    new = _mutate(rng, old, top)
    files = {
        "old.yaml": ("\n".join(render(old, rng)) + "\n").encode("utf-8"),
        "new.yaml": ("\n".join(render(new, rng)) + "\n").encode("utf-8"),
    }
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(rules=RULES),
        files=files,
        expected=diff(old, new),
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None:
        return Check(False, "the answer is not a JSON object")
    for kind, want in instance.expected.items():
        got = data.get(kind)
        if not isinstance(got, list) or not all(isinstance(p, str) for p in got):
            return Check(False, f"{kind}: not a list of paths")
        if sorted(got) != want:
            return Check(False, f"{kind}: expected {want}, got {sorted(got)}"[:300])
    return Check(True, "added, removed and changed match")


TASK = Task(
    id=ID,
    tier="1",
    title="A semantic YAML diff under written rules",
    capability="parse two YAML files and diff them by key path with set, type and ignore rules",
    generate=generate,
    check=check,
    has_sibling=True,
)
