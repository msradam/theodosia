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


_hex_keys = st.binary(min_size=8, max_size=32)


@given(events=st.lists(_events, min_size=1, max_size=8), key=_hex_keys)
@settings(max_examples=100, deadline=None)
def test_keyed_chain_verifies_only_under_its_key(events: list[dict], key: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        ledger = HashChainedLedger(path, key=key)
        for event in events:
            ledger.append(event)
        ok, problems = verify_ledger(path, key=key)
        assert ok, problems
        # Unkeyed verification of a keyed chain fails with the single
        # key-mode diagnosis, not per-entry tamper noise.
        ok_unkeyed, problems_unkeyed = verify_ledger(path)
        assert not ok_unkeyed
        assert len(problems_unkeyed) == 1
        assert "THEODOSIA_LEDGER_KEY" in problems_unkeyed[0]


@given(
    events=st.lists(_events, min_size=1, max_size=8),
    binding=st.dictionaries(
        st.sampled_from(["app_id", "project", "partition_key"]),
        st.text(min_size=1, max_size=10),
        min_size=1,
        max_size=3,
    ),
)
@settings(max_examples=100, deadline=None)
def test_bound_chain_refuses_foreign_binding(events: list[dict], binding: dict) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.jsonl"
        ledger = HashChainedLedger(path, binding=binding)
        for event in events:
            ledger.append(event)
        ok, problems = verify_ledger(path, expected_binding=binding)
        assert ok, problems
        foreign = dict(binding, app_id="__some_other_session__")
        ok_foreign, problems_foreign = verify_ledger(path, expected_binding=foreign)
        assert not ok_foreign
        assert all("binding mismatch" in p for p in problems_foreign)


@given(events=st.lists(_events, min_size=1, max_size=8), cut=st.integers(min_value=0))
@settings(max_examples=100, deadline=None)
def test_single_appender_cache_agrees_with_disk(events: list[dict], cut: int) -> None:
    # Splitting the same event stream across two instances (forcing a disk
    # re-read at the seam) must produce the identical chain a single cached
    # appender produces.
    with tempfile.TemporaryDirectory() as d:
        split_path = Path(d) / "split.jsonl"
        idx = cut % len(events)
        first = HashChainedLedger(split_path)
        for event in events[:idx]:
            first.append(event)
        second = HashChainedLedger(split_path)  # re-reads the head from disk
        for event in events[idx:]:
            second.append(event)

        cached_path = Path(d) / "cached.jsonl"
        cached = HashChainedLedger(cached_path)
        for event in events:
            cached.append(event)

        assert split_path.read_text() == cached_path.read_text()
        ok, problems = verify_ledger(split_path)
        assert ok, problems
