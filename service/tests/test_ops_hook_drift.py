"""Vendor drift guard — service/ops-hook/hook.py must stay byte-identical to
the fleet-canonical ops-hook in sneakyfree/windy-contracts (ops-hook/hook.py).

The canonical hook is env-parameterized so every Class-C host runs the SAME
bytes; all Search-specific facts live in deploy/ops-hook.env.example. If the
canon changes, re-vendor (see service/ops-hook/README.md) — do NOT edit the
vendored copy. This test only checks the local file is self-consistent and
carries the canonical marker; a full byte-compare against windy-contracts
runs where both repos are checked out (the doctrine lane), guarded here by
skip-if-absent so this repo's gate never depends on a sibling checkout.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

VENDORED = Path(__file__).resolve().parent.parent / "ops-hook" / "hook.py"


def test_vendored_hook_present_and_canonical():
    assert VENDORED.exists(), "service/ops-hook/hook.py missing"
    text = VENDORED.read_text()
    # Canonical markers — the vendored file is the generic fleet hook, not a fork.
    assert "windy ops-hook — the doctor that is NOT in the patient" in text
    assert 'HOOK_VERSION = "2.0.0"' in text
    assert "OPS_HOOK_TOKEN" in text and "OPS_HOOK_COMPOSE_CMD" in text
    # The generic file must be env-driven, not hardcode Search's real config.
    # (The canonical docstring names sample values illustratively; the guard
    # that matters — byte-identity with the canon — is the next test.)
    assert 'os.environ.get("OPS_HOOK_SERVICE"' in text, "service must come from env"
    assert 'os.environ.get("OPS_HOOK_IMAGE_REF"' in text, "image ref must come from env"


def test_byte_identical_to_canon_when_available():
    canon = Path.home() / "windy-contracts" / "ops-hook" / "hook.py"
    if not canon.exists():
        import pytest

        pytest.skip("windy-contracts not checked out here; CI/lane does the byte-compare")
    a = hashlib.sha256(VENDORED.read_bytes()).hexdigest()
    b = hashlib.sha256(canon.read_bytes()).hexdigest()
    assert a == b, (
        "service/ops-hook/hook.py has DRIFTED from windy-contracts/ops-hook/hook.py — "
        "re-vendor (cp) instead of editing the copy."
    )
