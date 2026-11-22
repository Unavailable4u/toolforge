"""Gate G6 with a scripted sandbox: what it asks for, and how it reads every kind of audit."""

from __future__ import annotations

import copy

import pytest
from sandbox_scripts import batch_report, item, respond
from verify_helpers import valid_package

from toolforge.config import Settings
from toolforge.sandbox import FakeRunner
from toolforge.verify.g6_containment import BUDGET_S, ContainmentGate, check_hardening
from toolforge.verify.pipeline import GateContext

GOOD_HARDENING = {
    "user": "65534:65534",
    "read_only_rootfs": True,
    "cap_drop": ["ALL"],
    "cap_add": None,
    "privileged": False,
    "security_opt": ["no-new-privileges"],
    "network_mode": "none",
    "binds": [],
    "pid_mode": "",
    "ipc_mode": "private",
}
GOOD_MEASURED = {
    "incomplete": [],
    "stats": {
        "peak_memory_bytes": 40 * 1024 * 1024,
        "memory_limit_bytes": 256 * 1024 * 1024,
        "peak_pids": 3,
        "pids_limit": 64,
        "cpu_seconds": 1.5,
        "oom_killed": False,
    },
    "fs": {"rootfs_changes": 0, "rootfs_changed": [], "host_binds": [], "inside": True},
    "secrets": {"checked": True, "found": [], "inside": True},
}
SEEDED = (
    "canary.seeded",
    {"items": [{"id": "c1", "label": "AWS access key"}, {"id": "c2", "label": "SSH key"}]},
)


def answer(request):
    return batch_report([item(i, {"ok": 1}) for i in range(len(request["items"]))])


def run_gate(
    *,
    hardening=GOOD_HARDENING,
    measured=GOOD_MEASURED,
    events=(SEEDED,),
    script=answer,
    package=None,
    blob=True,
):
    package = package or valid_package()
    runner = FakeRunner(
        lambda argv, put: respond(argv, put, {"batch": script}),
        hardening=copy.deepcopy(hardening),
        measured=copy.deepcopy(measured),
        events=list(events),
    )
    ctx = GateContext(
        package=package, settings=Settings(), runner=runner, label="my_tool@0.1.0", deadline=1e12,
        blob=package.to_tar() if blob else None,
    )  # fmt: skip
    return ContainmentGate().run(ctx), runner


def with_measured(**changes):
    measured = copy.deepcopy(GOOD_MEASURED)
    for key, value in changes.items():
        if isinstance(value, dict):
            measured[key] = {**measured.get(key, {}), **value}
        else:
            measured[key] = value
    return measured


def test_a_clean_run_in_a_locked_down_sandbox_passes() -> None:
    result, runner = run_gate()
    assert result.status == "passed", (result.summary, result.hints)
    assert result.metrics["decoys_planted"] == 2 and result.metrics["decoy_events"] == 0
    assert result.metrics["hardening_confirmed"] is True
    assert "not applicable" in result.metrics["egress"]
    assert "egress is not measured" in result.summary


def test_the_sandbox_is_closed_to_the_network_and_has_decoys() -> None:
    _, runner = run_gate()
    (spec,) = runner.opened
    assert spec.network == "none" and spec.decoys is True
    assert spec.label.endswith(":G6") and runner.handles[0].closed


def test_inputs_are_the_non_live_examples_and_generated_ones() -> None:
    seen = []

    def record(request):
        seen.append(request)
        return answer(request)

    run_gate(script=record)
    request = seen[0]
    assert request["include_results"] is False and request["deadline_s"] == BUDGET_S
    assert 1 < len(request["items"]) <= 10 + len(valid_package().manifest.examples)


@pytest.mark.parametrize("kind", ["read", "exposed", "tripped", "copied"])
def test_touching_a_decoy_fails_the_gate(kind: str) -> None:
    event = (f"canary.{kind}", {"item": "c1", "label": "AWS access key"})
    result, _ = run_gate(events=[SEEDED, event])
    assert result.status == "failed"
    assert "AWS access key" in result.summary and kind in result.summary
    assert result.metrics["decoy_events"] == 1
    assert any("credentials" in h for h in result.hints)


def test_a_hostile_decoy_label_is_clipped_in_the_summary() -> None:
    event = ("canary.read", {"item": "c1", "label": "x" * 500})
    result, _ = run_gate(events=[SEEDED, event])
    assert result.status == "failed" and len(result.summary) < 400


def test_no_decoy_planted_means_skipped_never_passed() -> None:
    result, _ = run_gate(events=[])
    assert result.status == "skipped" and "no decoy was planted" in result.summary


def test_a_sandbox_that_reports_no_hardening_is_skipped() -> None:
    result, _ = run_gate(hardening={})
    assert result.status == "skipped" and "hardening" in result.summary
    assert result.metrics["hardening_confirmed"] is False


def test_an_incomplete_audit_is_skipped() -> None:
    result, _ = run_gate(measured=with_measured(incomplete=["stats"]))
    assert result.status == "skipped" and "incomplete" in result.summary


def test_no_audit_at_all_is_skipped() -> None:
    result, _ = run_gate(measured={})
    assert result.status == "skipped" and "no audit" in result.summary


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"user": "0:0"}, "unprivileged"),
        ({"read_only_rootfs": False}, "writable"),
        ({"cap_drop": []}, "capabilities"),
        ({"cap_add": ["NET_ADMIN"]}, "capability was added"),
        ({"privileged": True}, "privileged"),
        ({"security_opt": []}, "no-new-privileges"),
        ({"network_mode": "bridge"}, "network mode"),
        ({"binds": ["/:/host"]}, "host folder"),
        ({"pid_mode": "host"}, "process namespace"),
        ({"ipc_mode": "host"}, "IPC"),
    ],
)
def test_a_weaker_sandbox_than_required_fails(change: dict, text: str) -> None:
    result, _ = run_gate(hardening={**GOOD_HARDENING, **change})
    assert result.status == "failed" and text in result.summary


def test_check_hardening_names_what_is_unknown() -> None:
    wrong, unknown = check_hardening({})
    assert wrong == [] and "user" in unknown and "network_mode" in unknown


def test_changed_container_files_fail() -> None:
    measured = with_measured(fs={"rootfs_changes": 2, "rootfs_changed": [{"path": "/etc/passwd"}]})
    result, _ = run_gate(measured=measured)
    assert result.status == "failed" and "/etc/passwd" in result.summary
    assert result.metrics["rootfs_changes"] == 2


def test_a_host_bind_fails() -> None:
    result, _ = run_gate(measured=with_measured(fs={"host_binds": ["/home"]}))
    assert result.status == "failed" and "host folder" in result.summary


def test_a_real_secret_found_inside_fails() -> None:
    result, _ = run_gate(measured=with_measured(secrets={"found": [{"name": "TOKEN"}]}))
    assert result.status == "failed" and "real secret" in result.summary


def test_running_out_of_memory_fails() -> None:
    result, _ = run_gate(measured=with_measured(stats={"oom_killed": True}))
    assert result.status == "failed" and "memory" in result.summary


def test_memory_over_the_limit_fails() -> None:
    result, _ = run_gate(measured=with_measured(stats={"peak_memory_bytes": 9 * 1024**3}))
    assert result.status == "failed" and "memory use" in result.summary


def test_reaching_the_process_limit_fails() -> None:
    result, _ = run_gate(measured=with_measured(stats={"peak_pids": 64, "pids_limit": 64}))
    assert result.status == "failed" and "processes" in result.summary


def test_far_too_much_cpu_fails() -> None:
    result, _ = run_gate(measured=with_measured(stats={"cpu_seconds": 1e6}))
    assert result.status == "failed" and "CPU" in result.summary


def test_a_hostile_audit_payload_cannot_crash_or_pass_the_gate() -> None:
    hostile = {
        "fs": "nope",
        "secrets": [1],
        "stats": {"peak_memory_bytes": "lots", "peak_pids": True},
        "incomplete": [1, {"a": 1}],
    }
    result, _ = run_gate(measured=hostile)
    assert result.status in {"skipped", "failed"}


def test_egress_recorded_with_no_network_fails() -> None:
    result, _ = run_gate(events=[SEEDED, ("egress.blocked", {"host": "example.com"})])
    assert result.status == "failed" and "network traffic" in result.summary


def test_an_input_that_times_out_fails() -> None:
    def timeouts(request):
        return batch_report(
            [item(0, error=("Timeout", "too slow"))]
            + [item(i, {"ok": 1}) for i in range(1, len(request["items"]))]
        )

    result, _ = run_gate(script=timeouts)
    assert result.status == "failed" and "time limit" in result.summary


def test_a_run_that_did_not_finish_every_input_fails() -> None:
    def partial(request):
        n = len(request["items"])
        return batch_report([item(i, {"ok": 1}) for i in range(n - 1)], 1)

    result, _ = run_gate(script=partial)
    assert result.status == "failed" and "budget" in result.summary


def test_a_missing_blob_fails_without_opening_a_sandbox() -> None:
    result, runner = run_gate(blob=False)
    assert result.status == "failed" and runner.opened == []


def test_the_gate_budget_covers_the_run_and_the_grace() -> None:
    assert ContainmentGate.budget_s == BUDGET_S + 90


# --- The sandbox's own mount points (found on a real Docker daemon) -------------------------------------------------------------------

MOUNT_ONLY = {
    "rootfs_changes": 1,
    "rootfs_changed": [{"path": "/workspace", "kind": "modified"}],
    "tmpfs": ["/tmp", "/workspace"],
}


def test_docker_listing_the_workspace_mount_point_is_not_a_change_by_the_tool() -> None:
    fs = {
        **MOUNT_ONLY,
        "rootfs_changed": [{"path": "/workspace", "kind": "added"}],
    }  # what a real daemon most likely reports
    result, _ = run_gate(measured=with_measured(fs=fs))
    assert result.status == "passed", (result.summary, result.hints)
    assert result.metrics["rootfs_changes"] == 0


def test_a_mount_point_is_ignored_even_when_the_audit_does_not_list_the_tmpfs() -> None:
    fs = {
        "rootfs_changes": 2,
        "rootfs_changed": [{"path": "/workspace", "kind": "modified"}, {"path": "/tmp", "kind": "modified"}],
    }
    result, _ = run_gate(measured=with_measured(fs=fs))
    assert result.status == "passed", (result.summary, result.hints)


def test_a_real_change_next_to_a_mount_point_still_fails_and_is_the_one_named() -> None:
    fs = {
        **MOUNT_ONLY,
        "rootfs_changes": 2,
        "rootfs_changed": [*MOUNT_ONLY["rootfs_changed"], {"path": "/etc/passwd", "kind": "modified"}],
    }
    result, _ = run_gate(measured=with_measured(fs=fs))
    assert (
        result.status == "failed"
        and "1 changes" in result.summary
        and "/etc/passwd (modified)" in result.summary
    )
    assert "/workspace" not in result.summary


@pytest.mark.parametrize("kind", ["modified", "added", "deleted", "?"])
def test_a_mount_point_is_ignored_whatever_kind_docker_gave_it(kind: str) -> None:
    fs = {**MOUNT_ONLY, "rootfs_changed": [{"path": "/workspace", "kind": kind}]}
    result, _ = run_gate(measured=with_measured(fs=fs))
    assert result.status == "passed", (result.summary, result.hints)


@pytest.mark.parametrize("kind", ["modified", "added", "deleted"])
def test_a_path_that_is_not_a_mount_point_counts_whatever_its_kind(kind: str) -> None:
    fs = {**MOUNT_ONLY, "rootfs_changed": [{"path": "/workspace/evil", "kind": kind}]}
    result, _ = run_gate(measured=with_measured(fs=fs))
    assert result.status == "failed" and f"/workspace/evil ({kind})" in result.summary


def test_a_count_with_no_listed_entries_is_trusted_not_dropped() -> None:
    result, _ = run_gate(
        measured=with_measured(
            fs={"rootfs_changes": 3, "rootfs_changed": [], "tmpfs": ["/tmp", "/workspace"]}
        )
    )
    assert result.status == "failed" and "3 changes" in result.summary


def test_real_rootfs_changes_reads_garbage_as_empty() -> None:
    from toolforge.sandbox.base import real_rootfs_changes

    assert real_rootfs_changes("x", "y", 5) == (0, [], 0)
    assert real_rootfs_changes(True, [None, 3, {"path": "/a", "kind": "added"}], None) == (
        0,
        [{"path": "/a", "kind": "added"}],
        0,
    )
    assert real_rootfs_changes(1, [{"path": "/workspace", "kind": "modified"}]) == (0, [], 1)
    assert real_rootfs_changes(1, [{"path": "/workspace", "kind": "added"}]) == (0, [], 1)
