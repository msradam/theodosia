"""Hash-chained audit ledger.

Every recorded attempt (a step or a refusal) is appended as one JSONL line
carrying ``prev`` (the previous line's hash) and ``hash`` (sha256 or
HMAC-sha256 over the previous hash plus this entry's canonical encoding).
Editing, reordering, or inserting any earlier line breaks every later hash,
so ``verify_ledger`` points at the exact line where the chain diverges.

What the chain proves and does not prove:

* **Proves**: in-place edits, reorderings, duplications, and middle-deletions
  of recorded entries (with the exact offending line called out).
* **Does not prove on its own**:
  - *Truncation* (dropping the tail-most entry leaves a chain that still
    self-verifies). Detect by external commitment of expected-length, or by
    streaming each entry to append-only storage as it is written.
  - *Whole-cloth forgery* under the default (unkeyed) mode. The hash
    function is public, so a holder of write access to ``ledger.jsonl`` can
    mint a chain from scratch. Set ``THEODOSIA_LEDGER_KEY`` (hex-encoded
    bytes) in the server's environment to switch the chain to HMAC; forgery
    then requires the key.
  - *Cross-session replay* unless the entries carry their ``app_id`` and
    ``project`` in the hashed payload. ``mount()`` binds both by default, so
    copying a ``ledger.jsonl`` from session A into session B's directory
    fails ``verify`` because the binding does not match the on-disk path.
  - *Origin*: a signature over the head would add non-repudiation; the chain
    alone does not.
  - *Existence of any particular session*: deleting a whole session
    directory is invisible to ``verify``; detect by external manifest.

For regulated audit trails, layer external commitments (RFC 3161 timestamp
authority, transparency log, append-only object storage with retention
locks) on top.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

GENESIS = "sha256:" + "0" * 64
GENESIS_HMAC = "hmac-sha256:" + "0" * 64


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)


def _resolve_key(explicit: bytes | None) -> bytes | None:
    """Return the HMAC key from ``explicit`` or ``THEODOSIA_LEDGER_KEY``.

    The env var must be hex-encoded; a non-hex value raises ``ValueError``
    so a typo cannot silently produce a different chain than the operator
    intended. ``None`` means plain SHA256.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get("THEODOSIA_LEDGER_KEY")
    if not env:
        return None
    try:
        return bytes.fromhex(env)
    except ValueError as exc:
        raise ValueError(
            "THEODOSIA_LEDGER_KEY must be hex-encoded bytes (e.g. the output "
            "of `openssl rand -hex 32`). Got a value that is not valid hex."
        ) from exc


def _digest(prev: str, entry_without_hash: dict[str, Any], key: bytes | None) -> str:
    payload = (prev + _canonical(entry_without_hash)).encode()
    if key is None:
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


class HashChainedLedger:
    """Append-only JSONL chain. One instance per ``ledger.jsonl`` file.

    ``binding`` is an optional dict of session identity fields (``app_id``,
    ``project``, ``partition_key``) that lands inside every entry's hashed
    payload. The adapter binds these by default so a ledger cannot be moved
    to a different session directory and still verify.

    ``key`` is an optional HMAC key. ``None`` (the default) uses plain
    SHA256; pass bytes (or set ``THEODOSIA_LEDGER_KEY`` in the env as a hex
    string) to switch to HMAC-SHA256, which makes forgery require the key.

    **Stability**: the on-disk format (canonical JSON, ``prev`` / ``hash`` /
    ``binding`` field names, genesis string) is stable for the v0.x series
    but considered provisional. A future major release may add a
    ``schema_version`` field and version-gated verification; older ledger
    files will continue to verify under their original schema. Do not
    serialize the ``HashChainedLedger`` class itself; treat the JSONL file
    as the contract.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        binding: dict[str, Any] | None = None,
        key: bytes | None = None,
        last_hash: str | None = None,
    ) -> None:
        """Bind the ledger to ``path``; see the class docstring for kwargs."""
        self.path = Path(path)
        self.binding = dict(binding or {})
        self.key = _resolve_key(key)
        # Optional caller-supplied last hash; lets a single-process repeat
        # appender skip the O(n) file re-read between writes. Caller is
        # responsible for keeping the cache consistent. Leave ``None`` to
        # always read from disk.
        self._cached_last_hash: str | None = last_hash

    def _genesis(self) -> str:
        return GENESIS_HMAC if self.key is not None else GENESIS

    def _last_hash(self) -> str:
        if self._cached_last_hash is not None:
            return self._cached_last_hash
        if not self.path.exists():
            return self._genesis()
        last = self._genesis()
        with self.path.open(encoding="utf-8") as fh:  # pragma: no mutate
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line).get("hash", last)
        return last

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Chain ``event`` onto the ledger and return the written entry.

        The entry is the event plus its ``prev``, ``binding``, and ``hash``.
        """
        prev = self._last_hash()
        entry = event | {"prev": prev}
        if self.binding:
            entry["binding"] = self.binding
        entry["hash"] = _digest(prev, entry, self.key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:  # pragma: no mutate
            fh.write(_canonical(entry) + "\n")
        # Keep cache consistent for the next append on this instance.
        self._cached_last_hash = entry["hash"]
        return entry


def verify_ledger(
    path: str | Path,
    *,
    expected_binding: dict[str, Any] | None = None,
    key: bytes | None = None,
    expected_min_entries: int | None = None,
) -> tuple[bool, list[str]]:
    """Recompute the chain. Returns ``(ok, problems)``.

    Each problem names the line where the recorded hash, the prev-link, the
    binding, an entry-count expectation, or JSON decoding fails. A missing
    file is vacuously valid.

    ``expected_binding`` (typically ``{"app_id": ..., "project": ...}``)
    refuses entries whose stored binding does not match; this is what makes
    cross-session replay (copying ledger.jsonl to a different app dir)
    detectable.

    ``key`` (or ``THEODOSIA_LEDGER_KEY`` env) verifies an HMAC-keyed chain;
    pass ``None`` for the default unkeyed chain.

    ``expected_min_entries`` refuses a ledger shorter than the given count;
    use it with an external claim of recorded length to detect truncation.

    Line numbers in problems are 1-based file lines. When the ledger's
    stored hash prefix and the provided key disagree on keyed-vs-unkeyed
    mode, the single problem returned names the key mismatch instead of
    flagging every entry as altered.
    """
    p = Path(path)
    if not p.exists():
        return True, []
    resolved_key = _resolve_key(key)
    stored_mode = _stored_mode(p)
    if stored_mode == "keyed" and resolved_key is None:
        return False, [
            "keyed ledger (hmac-sha256 entries) verified without a key; set "
            "THEODOSIA_LEDGER_KEY to the key this ledger was written with"
        ]
    if stored_mode == "unkeyed" and resolved_key is not None:
        return False, [
            "unkeyed ledger (sha256 entries) verified with a key; unset "
            "THEODOSIA_LEDGER_KEY, or treat this as tampering if the ledger "
            "was expected to be keyed"
        ]
    genesis = GENESIS_HMAC if resolved_key is not None else GENESIS
    problems: list[str] = []
    prev = genesis
    count = 0
    hash_mismatches = 0
    prev_seq: int | None = None
    with p.open(encoding="utf-8") as fh:  # pragma: no mutate
        for i, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                # A verifier must report corruption, not crash on it. The
                # garbled line is flagged here; the next valid entry's
                # prev-link check flags the break in the chain itself.
                problems.append(f"line {i}: not valid JSON ({exc.msg})")
                continue
            entry_problems = _check_entry(i, entry, prev, expected_binding, resolved_key)
            hash_mismatches += sum("hash mismatch" in msg for msg in entry_problems)
            problems.extend(entry_problems)
            seq = entry.get("seq")
            if isinstance(seq, int) and isinstance(prev_seq, int) and seq > prev_seq + 1:
                problems.append(
                    f"line {i}: seq gap ({prev_seq} -> {seq}); "
                    f"{seq - prev_seq - 1} entry(ies) missing"
                )
            if isinstance(seq, int):
                prev_seq = seq
            prev = entry.get("hash")
            count += 1
    if resolved_key is not None and count > 0 and hash_mismatches == count:
        problems.append(
            "every entry fails under the provided key; THEODOSIA_LEDGER_KEY "
            "likely does not match the key this ledger was written with"
        )
    if expected_min_entries is not None and count < expected_min_entries:
        problems.append(
            f"truncation: ledger has {count} entries; expected at least {expected_min_entries}"
        )
    return (not problems), problems


def _stored_mode(p: Path) -> str | None:
    """Sniff whether the on-disk chain is keyed, from the first entry's hash prefix."""
    with p.open(encoding="utf-8") as fh:  # pragma: no mutate
        for line in fh:
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                h = json.loads(line).get("hash") or ""
                if h.startswith("hmac-sha256:"):
                    return "keyed"
                if h.startswith("sha256:"):
                    return "unkeyed"
            return None
    return None


def attestation_receipt(path: str | Path, *, key: bytes | None = None) -> dict[str, Any]:
    """A portable, machine-readable proof of a ledger's chain head.

    Returns ``{ok, entries, head_hash, keyed, problems}``. Store the receipt
    next to an external audit record; re-running verification later and
    checking ``head_hash`` still matches proves no entry was added, removed,
    or altered since. A missing ledger is vacuously valid (``None`` head,
    zero entries).
    """
    p = Path(path)
    ok, problems = verify_ledger(p, key=key)
    head: str | None = None
    entries = 0
    if p.exists():
        with p.open(encoding="utf-8") as fh:  # pragma: no mutate
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entries += 1
                with contextlib.suppress(json.JSONDecodeError):
                    head = json.loads(line).get("hash", head)
    return {
        "ok": ok,
        "entries": entries,
        "head_hash": head,
        "keyed": _resolve_key(key) is not None,
        "problems": problems,
    }


def _check_entry(
    i: int,
    entry: dict[str, Any],
    prev: str,
    expected_binding: dict[str, Any] | None,
    resolved_key: bytes | None,
) -> list[str]:
    """Per-entry checks: hash, prev-link, and binding match."""
    problems: list[str] = []
    payload = {k: v for k, v in entry.items() if k != "hash"}
    recomputed = _digest(prev, payload, resolved_key)
    if entry.get("prev") != prev:
        problems.append(f"line {i}: prev-link mismatch (chain broken before here)")
    if entry.get("hash") != recomputed:
        problems.append(f"line {i}: hash mismatch (entry was altered)")
    if expected_binding is not None:
        got = entry.get("binding") or {}
        missing = [k for k, v in expected_binding.items() if got.get(k) != v]
        if missing:
            problems.append(
                f"line {i}: binding mismatch on {missing} (ledger does not belong to this session)"
            )
    return problems


def ledger_count(path: str | Path) -> int:
    """Return the number of non-empty lines in ``path``. 0 if missing."""
    p = Path(path)
    if not p.exists():
        return 0
    with p.open(encoding="utf-8") as fh:  # pragma: no mutate
        return sum(1 for line in fh if line.strip())
