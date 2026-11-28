"""The demo's operator: the human who, in real life, reads the permission diff and runs `toolforge promote`.

Nobody sits at the keyboard during a recorded replay, so this plays a fixed one after every tool call (the same idea as the benchmark's
`ScriptedOperator`, and printed on screen so the audience sees that the agent did not do it). It promotes a verified version that asks for
no network and no secret, never promotes a name in `refuse` (the hostile fixtures: a real operator would read the code and say no) and
denies any approval request, because no demo tool asks for network. Every decision is written to the registry and to the version's
`operator.jsonl` ledger with the reason below, exactly as a human's `toolforge promote` would be (the benchmark's `ScriptedOperator` writes no
ledger line; a note for a later patch).

Proven by `tests/test_demo_operator.py (it lives in operator_script.py because a module called operator would shadow the standard library when the script runs)`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

REASON = "demo operator (docs/DEMO_SCRIPT.md): verified, and asks for no network or secret"


class DemoOperator:
    def __init__(
        self,
        settings: Any,
        refuse: Collection[str] = (),
        on_action: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.refuse = set(refuse)
        self.on_action = on_action
        self.actions: list[dict[str, Any]] = []
        self._seen: set[tuple[str, int]] = set()

    def _note(self, action: dict[str, Any]) -> None:
        self.actions.append(action)
        if self.on_action is not None:
            self.on_action(action)

    def act(self) -> None:
        from toolforge import cli_run
        from toolforge.errors import ToolForgeError
        from toolforge.registry import Registry
        from toolforge.registry import lifecycle as lc
        from toolforge.services import operator_actions

        registry = Registry.from_settings(self.settings)
        try:
            for status in (lc.VERIFIED, lc.VERIFIED_BASIC):
                for record in registry.list_versions(status=status):
                    if ("promote", record.id) in self._seen:
                        continue
                    self._seen.add(("promote", record.id))
                    if record.name in self.refuse:
                        self._note({"action": "refuse", "version": record.label})
                        continue
                    if record.asks_network_or_secrets:
                        self._note({"action": "leave_pending", "version": record.label})
                        continue
                    try:
                        operator_actions.promote(
                            self.settings, registry, record.label, REASON, ledger_factory=cli_run.make_ledger
                        )
                        self._note({"action": "promote", "version": record.label, "from": status})
                    except ToolForgeError as exc:
                        self._note({"action": "promote_refused", "version": record.label, "code": exc.code})
            for approval in registry.list_approvals("pending"):
                if ("decide", approval.id) in self._seen:
                    continue
                self._seen.add(("decide", approval.id))
                try:
                    operator_actions.decide(
                        self.settings,
                        registry,
                        approval.id,
                        "deny",
                        "demo operator: the demo approves no network or secret",
                        ledger_factory=cli_run.make_ledger,
                    )
                    self._note({"action": "deny", "approval": approval.id})
                except ToolForgeError as exc:
                    self._note({"action": "deny_refused", "approval": approval.id, "code": exc.code})
        finally:
            registry.close()
