"""Runs index.html's inline token-init script under Node against stubs of
`window`, `sessionStorage` and `history` (docs/system-design/31-phase-26-
followups-design.md §31.3, §31.7: there is no real browser here).

Skipped when `node` is not installed; GitHub's ubuntu runners have it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "src" / "viva" / "web" / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _script() -> str:
    match = re.search(r'<script id="token-init">(.*?)</script>', INDEX.read_text(encoding="utf-8"), re.S)
    assert match, "index.html lost its token-init script"
    return match.group(1)


def _run(*, embedded: str, saved: str | None, href: str, storage_broken: bool = False) -> dict:
    harness = f"""
    const store = {json.dumps({} if saved is None else {"viva_token": saved})};
    const replaced = [];
    global.window = {{
      __VIVA_TOKEN__: {json.dumps(embedded)},
      location: {{ href: {json.dumps(href)} }},
      sessionStorage: {{
        setItem(k, v) {{ if ({json.dumps(storage_broken)}) throw new Error("denied"); store[k] = v; }},
        getItem(k) {{ if ({json.dumps(storage_broken)}) throw new Error("denied"); return k in store ? store[k] : null; }},
      }},
      history: {{ replaceState(a, b, u) {{ replaced.push(u); }} }},
    }};
    {_script()}
    console.log(JSON.stringify({{ token: window.__VIVA_TOKEN__, stored: store.viva_token ?? null, replaced }}));
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_embedded_token_is_saved_and_removed_from_the_address_bar():
    result = _run(embedded="tok-123", saved=None, href="http://127.0.0.1:8000/?token=tok-123")

    assert result == {"token": "tok-123", "stored": "tok-123", "replaced": ["/"]}


def test_other_query_params_and_the_hash_survive_the_cleanup():
    result = _run(embedded="tok-123", saved=None, href="http://h:8000/?a=1&token=tok-123#live")

    assert result["replaced"] == ["/?a=1#live"]


def test_reload_without_a_token_in_the_url_uses_the_saved_one():
    result = _run(embedded="", saved="tok-saved", href="http://127.0.0.1:8000/")

    assert result == {"token": "tok-saved", "stored": "tok-saved", "replaced": []}


def test_no_embedded_and_no_saved_token_leaves_the_loopback_default_alone():
    result = _run(embedded="", saved=None, href="http://127.0.0.1:8000/")

    assert result == {"token": "", "stored": None, "replaced": []}


def test_a_new_token_in_the_url_replaces_a_stale_saved_one():
    result = _run(embedded="tok-new", saved="tok-old", href="http://h/?token=tok-new")

    assert result["token"] == "tok-new" and result["stored"] == "tok-new"


def test_broken_storage_does_not_stop_the_url_being_cleaned():
    result = _run(embedded="tok-123", saved=None, href="http://h/?token=tok-123", storage_broken=True)

    assert result["token"] == "tok-123"
    assert result["replaced"] == ["/"]
