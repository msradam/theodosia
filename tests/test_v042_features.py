"""v0.4.2 additions: ledger attestation receipt and `sessions tail --once`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theodosia.ledger import HashChainedLedger, attestation_receipt


def test_attestation_receipt_intact_chain(tmp_path: Path):
    p = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(p)
    led.append({"event": "step", "action": "a"})
    led.append({"event": "step", "action": "b"})
    head = led.append({"event": "step", "action": "c"})["hash"]

    r = attestation_receipt(p)
    assert r["ok"] is True
    assert r["entries"] == 3
    assert r["head_hash"] == head  # the portable proof of the chain head
    assert r["keyed"] is False
    assert r["problems"] == []


def test_attestation_receipt_missing_ledger_is_vacuous(tmp_path: Path):
    r = attestation_receipt(tmp_path / "nope.jsonl")
    assert r == {"ok": True, "entries": 0, "head_hash": None, "keyed": False, "problems": []}


def test_attestation_receipt_detects_tamper(tmp_path: Path):
    p = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(p)
    led.append({"event": "step", "action": "a"})
    led.append({"event": "step", "action": "b"})

    # Rewrite the first entry's payload but keep its old hash → chain breaks.
    lines = p.read_text().splitlines()
    first = json.loads(lines[0])
    first["action"] = "HACKED"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")

    r = attestation_receipt(p)
    assert r["ok"] is False
    assert r["problems"]


def test_attestation_receipt_keyed_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("THEODOSIA_LEDGER_KEY", "00" * 32)
    p = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(p)  # picks up the env key via _resolve_key(None)
    led.append({"event": "step"})

    r = attestation_receipt(p)
    assert r["keyed"] is True
    assert r["ok"] is True


def test_tail_once_renders_one_snapshot_and_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--once` takes the non-Live path: it prints a snapshot and returns
    instead of entering the polling loop (which would block on a pipe/CI)."""
    from theodosia.cli import sessions

    monkeypatch.setattr(sessions, "_read_steps", lambda _p: [])
    # If once=True wrongly entered the Live loop, this call would block forever.
    sessions._tail(tmp_path / "log.jsonl", project="p", app_id="a", poll_interval=0.5, once=True)
