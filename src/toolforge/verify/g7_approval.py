"""G7, the approval gate: a human decides on a tool that asks for network hosts or secrets.

G7 is not a step the pipeline waits in. The other gates run code and answer within minutes; this one needs a person, so the
pipeline hands over and returns. After G1 to G6 have run, `ApprovalGate.request` is called for a version whose manifest has
non-empty `network` or `secrets`:

* the version moves from `verified` or `verified_basic` to `pending_approval` (a `verified_basic` one too: without that rule a
  network tool could never become active on the no-key path, because `promote` refuses it);
* a request is written (`services/approvals.py`), naming every gate that did not run first;
* the operator decides with `toolforge approvals approve|deny` (`services/operator_actions.decide`); a denial, a timeout and a
  missing broker service all end in `rejected`.

A version that asks for nothing sensitive skips G7 and waits for `toolforge promote` (nothing applies `auto_promote` yet; it never would for network or secrets).

Security properties and their tests (`tests/test_g7_approval.py`): a `verified_basic` version with a network host goes to
`pending_approval`, never to `active`, and `promote` still refuses it; nothing here can approve (it imports no decision
function; `tests/test_server_tools.py` checks the server package never imports one either).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from toolforge.config import Settings
from toolforge.registry import Registry, VersionRecord
from toolforge.services import approvals
from toolforge.services.approvals import BrokerLookup, Requested


class ApprovalGate:
    """The G7 step. One instance per pipeline; it holds no state of its own."""

    id = "G7"

    def __init__(
        self, settings: Settings, registry: Registry, *, brokers: BrokerLookup | None = None
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._brokers = brokers

    @staticmethod
    def needed(record: VersionRecord) -> bool:
        """True when the manifest declares any network host or secret."""
        return approvals.asks_for_permissions(record.manifest)

    def request(
        self,
        record: VersionRecord,
        gates: Sequence[Mapping[str, Any]],
        ledger_append: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> Requested:
        """Open the approval request of a verified version. See `approvals.open_request`."""
        return approvals.open_request(
            self._settings, self._registry, record, gates, ledger_append=ledger_append, brokers=self._brokers
        )


__all__ = ["ApprovalGate"]
