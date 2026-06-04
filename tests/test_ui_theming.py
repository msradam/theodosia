"""Themed session UI: asset patching, brand identity, and catch-all serving."""

from __future__ import annotations

from pathlib import Path

import pytest

from theodosia import _ui


def _build(tmp_path: Path, **kw) -> str:
    defaults = {
        "name": "Theodosia",
        "mark": "⊢",
        "subtitle": "session tracking",
        "credit_html": "x",
    }
    _ui.build_themed_assets(tmp_path / "build", **{**defaults, **kw})
    return (tmp_path / "build" / "index.html").read_text()


def test_credit_attribution():
    plain = _ui.burr_credit_html(powered_by_theodosia=False)
    powered = _ui.burr_credit_html(powered_by_theodosia=True)
    assert "Apache Burr" in plain and "powered by Theodosia" not in plain
    assert "Apache Burr" in powered and "powered by Theodosia" in powered


def test_brand_identity_theodosia_vs_rebrand():
    name, mark, credit = _ui.brand_identity("theodosia")
    assert (name, mark) == ("Theodosia", "⊢")
    assert "powered by Theodosia" not in credit

    name, mark, credit = _ui.brand_identity("coffee-agent")
    assert name == "Coffee-agent"
    assert mark is None  # the ⊢ is Theodosia's mark, not a rebrand's
    assert "powered by Theodosia" in credit

    # explicit overrides win
    name, mark, _ = _ui.brand_identity("coffee-agent", ui_title="Coffee Agent", ui_mark="☕")
    assert (name, mark) == ("Coffee Agent", "☕")


def test_brand_display_name_drives_console_footers():
    """The session-console footers (`status`, `sessions show`) label the link
    with this, so a rebrand points users at *its* console."""
    from dataclasses import fields

    from theodosia.cli._branding import _BRANDING, _Branding, _set_branding, brand_display_name

    snapshot = {f.name: getattr(_BRANDING, f.name) for f in fields(_BRANDING)}
    try:
        _set_branding(_Branding(prog_name="barista"))
        assert brand_display_name() == "Barista"
        _set_branding(_Branding(prog_name="loandesk", ui_title="LoanDesk"))
        assert brand_display_name() == "LoanDesk"  # ui_title overrides capitalization
        _set_branding(_Branding())
        assert brand_display_name() == "Theodosia"
    finally:
        _set_branding(_Branding(**snapshot))


def test_build_patches_index_for_rebrand(tmp_path: Path):
    name, mark, credit = _ui.brand_identity("coffee-agent")
    assert name == "Coffee-agent"  # default display name capitalizes prog_name
    html = _build(tmp_path, name=name, mark=mark, credit_html=credit)
    assert f"<title>{name}" in html and "Session Console</title>" in html
    assert "<title>Burr</title>" not in html
    assert 'id="thd-bar"' in html
    assert f">{name}<" in html
    assert "powered by Theodosia" in html
    assert "Apache Burr" in html
    assert "theodosia-kill-sidebar" in html
    assert "theodosia-theme.css" in html
    assert (tmp_path / "build" / "static" / "css" / "theodosia-theme.css").exists()


def test_build_is_idempotent_and_rebuildable(tmp_path: Path):
    _build(tmp_path, name="One")
    html = _build(tmp_path, name="Two")  # rebuild over the same dir
    assert ">Two<" in html and ">One<" not in html


def test_manifest_rebranded(tmp_path: Path):
    import json

    _build(tmp_path, name="coffee-agent")
    manifest = json.loads((tmp_path / "build" / "manifest.json").read_text())
    assert manifest["short_name"] == "coffee-agent"
    assert "coffee-agent" in manifest["name"]


def test_catch_all_serves_index_not_422(tmp_path: Path, monkeypatch):
    """Regression: `from __future__ import annotations` made FastAPI treat a
    function-local ``Request`` param as a query field, 422-ing every page.
    The root and client-side routes must serve the patched index at 200.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("burr_path", str(tmp_path / "store"))
    name, mark, credit = _ui.brand_identity("theodosia")
    _ui.build_themed_assets(
        tmp_path / "build", name=name, mark=mark, subtitle="session tracking", credit_html=credit
    )
    app = _ui.build_app(tmp_path / "build")
    client = TestClient(app)

    for path in ("/", "/project/anything", "/project/x/y/z"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        assert 'id="thd-bar"' in resp.text
