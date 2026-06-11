"""Branch-level pins for the hash-chained ledger (2026-06 audit, ISSUE-002).

Deterministic unit tests that assert exact digests, genesis constants,
message texts, and key-resolution rules, so a mutated prefix, separator,
or comparison cannot survive `mutmut run`. Kept free of asyncio / FastMCP
so mutmut's fork-based workers never inherit live threads (forking a
threaded process segfaults on macOS); the end-to-end async ledger tests
live in test_ledger.py.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json

import pytest

from theodosia.ledger import (
    GENESIS,
    GENESIS_HMAC,
    HashChainedLedger,
    _canonical,
    _digest,
    _resolve_key,
    attestation_receipt,
    ledger_count,
    verify_ledger,
)


def _recompute_unkeyed(prev: str, payload: dict) -> str:
    encoded = (
        prev + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_genesis_constants_exact():
    assert GENESIS == "sha256:" + "0" * 64
    assert GENESIS_HMAC == "hmac-sha256:" + "0" * 64


def test_digest_matches_independent_recomputation(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    entry = led.append({"seq": 0, "who": "auditor"})
    payload = {k: v for k, v in entry.items() if k != "hash"}
    assert entry["hash"] == _recompute_unkeyed(GENESIS, payload)


def test_canonical_is_key_order_independent_and_compact():
    a = _canonical({"b": 1, "a": [None, True]})
    b = _canonical({"a": [None, True], "b": 1})
    assert a == b == '{"a":[null,true],"b":1}'


def test_canonical_stringifies_non_json_values(tmp_path):
    # default=str: a Path value must not crash the chain.
    led = HashChainedLedger(tmp_path / "ledger.jsonl")
    led.append({"artifact": tmp_path / "report.txt"})
    ok, problems = verify_ledger(tmp_path / "ledger.jsonl")
    assert ok, problems


def test_resolve_key_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("THEODOSIA_LEDGER_KEY", "aa" * 32)
    assert _resolve_key(b"explicit-wins") == b"explicit-wins"


def test_resolve_key_env_hex_decoded(monkeypatch):
    monkeypatch.setenv("THEODOSIA_LEDGER_KEY", "deadbeef")
    assert _resolve_key(None) == bytes.fromhex("deadbeef")


def test_resolve_key_absent_and_empty_env_mean_unkeyed(monkeypatch):
    monkeypatch.delenv("THEODOSIA_LEDGER_KEY", raising=False)
    assert _resolve_key(None) is None
    monkeypatch.setenv("THEODOSIA_LEDGER_KEY", "")
    assert _resolve_key(None) is None


def test_resolve_key_non_hex_env_raises(monkeypatch):
    monkeypatch.setenv("THEODOSIA_LEDGER_KEY", "not-hex!")
    with pytest.raises(ValueError, match="hex-encoded"):
        _resolve_key(None)


def test_keyed_chain_uses_hmac_and_prefix(tmp_path):
    key = b"\x01" * 32
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path, key=key)
    entry = led.append({"seq": 0})
    assert entry["prev"] == GENESIS_HMAC
    payload = {k: v for k, v in entry.items() if k != "hash"}
    encoded = (GENESIS_HMAC + _canonical(payload)).encode()
    assert entry["hash"] == "hmac-sha256:" + hmac_mod.new(key, encoded, hashlib.sha256).hexdigest()
    ok, problems = verify_ledger(path, key=key)
    assert ok, problems
    # The same file must NOT verify unkeyed or under a different key.
    assert not verify_ledger(path)[0]
    assert not verify_ledger(path, key=b"\x02" * 32)[0]


def test_digest_keyed_and_unkeyed_disagree():
    assert _digest(GENESIS, {"a": 1}, None) != _digest(GENESIS_HMAC, {"a": 1}, b"k")


def test_last_hash_reread_from_disk_continues_chain(tmp_path):
    # A fresh instance over an existing file must pick up the head hash.
    path = tmp_path / "ledger.jsonl"
    HashChainedLedger(path).append({"seq": 0})
    second = HashChainedLedger(path).append({"seq": 1})
    first = json.loads(path.read_text().splitlines()[0])
    assert second["prev"] == first["hash"]
    ok, problems = verify_ledger(path)
    assert ok, problems


def test_last_hash_skips_blank_lines_and_hashless_entries(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    head = json.loads(path.read_text().splitlines()[-1])["hash"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")
        fh.write('{"note":"no hash field"}\n')
    assert HashChainedLedger(path)._last_hash() == head


def test_cached_last_hash_skips_disk(tmp_path):
    path = tmp_path / "ledger.jsonl"
    head = HashChainedLedger(path).append({"seq": 0})["hash"]
    led = HashChainedLedger(path, last_hash=head)
    # Garble the file: the cache must win, no disk read happens.
    path.write_text("not json\n")
    assert led._last_hash() == head


def test_append_creates_parent_dirs_and_single_line(tmp_path):
    path = tmp_path / "deep" / "nested" / "ledger.jsonl"
    entry = HashChainedLedger(path).append({"seq": 0})
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == entry


def test_append_includes_binding_only_when_set(tmp_path):
    bare = HashChainedLedger(tmp_path / "a.jsonl").append({"seq": 0})
    assert "binding" not in bare
    bound = HashChainedLedger(tmp_path / "b.jsonl", binding={"app_id": "A", "project": "P"}).append(
        {"seq": 0}
    )
    assert bound["binding"] == {"app_id": "A", "project": "P"}


def test_binding_is_copied_not_aliased(tmp_path):
    binding = {"app_id": "A"}
    led = HashChainedLedger(tmp_path / "ledger.jsonl", binding=binding)
    binding["app_id"] = "B"  # mutate the caller's dict after construction
    assert led.binding == {"app_id": "A"}


def test_verify_binding_mismatch_and_match(tmp_path):
    path = tmp_path / "ledger.jsonl"
    HashChainedLedger(path, binding={"app_id": "A", "project": "P"}).append({"seq": 0})
    ok, problems = verify_ledger(path, expected_binding={"app_id": "A", "project": "P"})
    assert ok, problems
    ok, problems = verify_ledger(path, expected_binding={"app_id": "OTHER", "project": "P"})
    assert not ok
    assert any("binding mismatch" in p and "app_id" in p for p in problems)


def test_verify_binding_against_unbound_ledger_fails(tmp_path):
    path = tmp_path / "ledger.jsonl"
    HashChainedLedger(path).append({"seq": 0})
    ok, problems = verify_ledger(path, expected_binding={"app_id": "A"})
    assert not ok
    assert any("binding mismatch" in p for p in problems)


def test_verify_min_entries_boundary(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    assert verify_ledger(path, expected_min_entries=2)[0]
    ok, problems = verify_ledger(path, expected_min_entries=3)
    assert not ok
    assert problems == ["truncation: ledger has 2 entries; expected at least 3"]


def test_verify_skips_blank_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n\n   \n" + lines[1] + "\n")
    ok, problems = verify_ledger(path, expected_min_entries=2)
    assert ok, problems


def test_verify_reports_corrupt_line_instead_of_raising(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n{garbled\n" + lines[1] + "\n")
    ok, problems = verify_ledger(path)
    assert not ok
    assert any("line 1" in p and "not valid JSON" in p for p in problems)


def test_reordered_entries_detected(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    lines = path.read_text().splitlines()
    path.write_text("\n".join(reversed(lines)) + "\n")
    ok, problems = verify_ledger(path)
    assert not ok
    assert any("prev-link mismatch" in p for p in problems)


def test_ledger_count_missing_blank_and_real_lines(tmp_path):
    assert ledger_count(tmp_path / "nope.jsonl") == 0
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n  \n")
    assert ledger_count(path) == 2


def test_attestation_receipt_intact_and_missing(tmp_path):
    missing = attestation_receipt(tmp_path / "nope.jsonl")
    assert missing == {
        "ok": True,
        "entries": 0,
        "head_hash": None,
        "keyed": False,
        "problems": [],
    }
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    head = led.append({"seq": 1})["hash"]
    receipt = attestation_receipt(path)
    assert receipt["ok"] is True
    assert receipt["entries"] == 2
    assert receipt["head_hash"] == head
    assert receipt["keyed"] is False


def test_attestation_receipt_survives_corrupt_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    head = led.append({"seq": 0})["hash"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n  \n")  # blank lines are skipped, not counted
        fh.write("{garbled\n")
    receipt = attestation_receipt(path)
    assert receipt["ok"] is False  # verify flags the corruption...
    assert receipt["head_hash"] == head  # ...but the head is still reported
    assert receipt["entries"] == 2  # the garbled line still counts as an entry


# ── Targeted mutant killers (round 2) ──────────────────────────────────────


def test_resolve_key_error_message_exact(monkeypatch):
    monkeypatch.setenv("THEODOSIA_LEDGER_KEY", "zz-not-hex")
    with pytest.raises(ValueError) as excinfo:
        _resolve_key(None)
    assert str(excinfo.value) == (
        "THEODOSIA_LEDGER_KEY must be hex-encoded bytes (e.g. the output "
        "of `openssl rand -hex 32`). Got a value that is not valid hex."
    )


def test_last_hash_of_blank_only_file_is_genesis(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n   \n")
    assert HashChainedLedger(path)._last_hash() == GENESIS


def test_append_refreshes_cache_from_written_entry(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    head = led.append({"seq": 0})["hash"]
    # The instance must trust its own cache from the append; if it re-read
    # the (now garbled) file instead, this would raise.
    path.write_text("{garbled\n")
    assert led._last_hash() == head


def test_corrupt_line_does_not_stop_verification_of_later_entries(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n{garbled\n" + lines[1] + "\n")
    ok, problems = verify_ledger(path, expected_min_entries=2)
    assert not ok
    # Exactly the JSON problem: the two real entries still verify (so no
    # prev-link or truncation problems pile on after the corrupt line).
    assert len(problems) == 1
    assert problems[0].startswith("line 1: not valid JSON")


def test_hash_mismatch_message_names_line_and_cause(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0, "refused": False})
    entry = json.loads(path.read_text())
    entry["refused"] = True  # tamper but keep the recorded hash
    path.write_text(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    ok, problems = verify_ledger(path)
    assert not ok
    assert problems == ["line 0: hash mismatch (entry was altered)"]


def test_prev_link_message_names_line_and_cause(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = HashChainedLedger(path)
    led.append({"seq": 0})
    led.append({"seq": 1})
    lines = path.read_text().splitlines()
    path.write_text(lines[1] + "\n")  # drop the first entry: prev points nowhere
    ok, problems = verify_ledger(path)
    assert not ok
    assert "line 0: prev-link mismatch (chain broken before here)" in problems


def test_attestation_receipt_with_explicit_key(tmp_path):
    key = b"\x07" * 16
    path = tmp_path / "ledger.jsonl"
    head = HashChainedLedger(path, key=key).append({"seq": 0})["hash"]
    receipt = attestation_receipt(path, key=key)
    assert receipt["ok"] is True
    assert receipt["head_hash"] == head
    assert receipt["keyed"] is True


def test_attestation_head_survives_trailing_hashless_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    head = HashChainedLedger(path).append({"seq": 0})["hash"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"note":"valid json, no hash field"}\n')
    receipt = attestation_receipt(path)
    assert receipt["head_hash"] == head  # falls back to the previous head
    assert receipt["entries"] == 2
