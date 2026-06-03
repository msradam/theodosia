"""Property-based invariants for the hash-chained audit ledger.

The chain's only job is integrity, so it is a natural fit for property testing:
for ANY sequence of recorded events the verifier must agree with the writer,
ANY in-place edit of a recorded entry must be detected, and truncation must
behave exactly as the security model documents (self-verifies, but is caught by
an external length claim).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from theodosia.ledger import HashChainedLedger, verify_ledger

# Reserved keys the ledger writes itself; an event must not collide with them.
_RESERVED = {"prev", "hash", "binding"}
_keys = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8).filter(
    lambda k: k not in _RESERVED
)
_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4),
    max_leaves=6,
)
_events = st.dictionaries(_keys, _values, min_size=1, max_size=5)


def _write_chain(events: list[dict], path: Path) -> None:
    ledger = HashChainedLedger(path)
    for event in events:
        ledger.append(event)


@given(events=st.lists(_events, min_size=1, max_size=12))
@settings(max_examples=200, deadline=None)
def test_any_chain_self_verifies(events: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        _write_chain(events, path)
        ok, problems = verify_ledger(path)
        assert ok, problems


@given(events=st.lists(_events, min_size=1, max_size=12), pick=st.integers(min_value=0))
@settings(max_examples=200, deadline=None)
def test_inplace_edit_is_always_detected(events: list[dict], pick: int) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        _write_chain(events, path)
        lines = path.read_text().splitlines()
        idx = pick % len(lines)
        entry = json.loads(lines[idx])
        # Change one content field while keeping the recorded hash: the verifier
        # recomputes the hash from the tampered content and must catch the
        # mismatch. Events are non-empty, so a content key always exists.
        content_keys = [k for k in entry if k not in _RESERVED]
        key = content_keys[pick % len(content_keys)]
        entry[key] = ["__TAMPERED__", entry[key]]
        lines[idx] = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        path.write_text("\n".join(lines) + "\n")
        ok, problems = verify_ledger(path)
        assert not ok
        assert problems


@given(events=st.lists(_events, min_size=2, max_size=12))
@settings(max_examples=150, deadline=None)
def test_truncation_self_verifies_but_min_entries_catches_it(events: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        _write_chain(events, path)
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n")  # drop the tail entry
        ok_self, _ = verify_ledger(path)
        assert ok_self  # documented: truncation alone still self-verifies
        ok_claim, problems = verify_ledger(path, expected_min_entries=len(events))
        assert not ok_claim
        assert any("truncation" in p for p in problems)
