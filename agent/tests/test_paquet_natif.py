"""Paquet sans .py (E178, CA-18.31) : chaîne de publication Nuitka, test de fumée de l'artefact.

Compilation longue (plusieurs minutes) : lancée seulement avec ``H2S_EDGE_NATIVE_BUILD=1`` et Nuitka installé
(``requirements-build.txt``) ; sinon ignorée avec son motif.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / "tools"))

import build_native


def test_artefact_sans_py_demarrable_et_signe(tmp_path):
    if os.environ.get("H2S_EDGE_NATIVE_BUILD") != "1":
        pytest.skip("H2S_EDGE_NATIVE_BUILD=1 absent : compilation Nuitka non lancée")
    reason = build_native.nuitka_available()
    if reason:
        pytest.skip(reason)
    out = build_native.build(tmp_path / "native", jobs=4)
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert not [n for n in names if n.endswith((".py", ".pyc", ".pyi"))], names
    assert any(n.startswith("h2s_agent.") and n.endswith(".so") for n in names) and any(
        n.startswith("h2s_boot.") for n in names
    )
    probe = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); import h2s_agent, h2s_agent.orchestrator, h2s_agent.storage as s;"
        " from h2s_agent.__main__ import main; code = main(['version']);"
        " print(json.dumps({'code': code, 'compiled': '__compiled__' in dir(h2s_agent), 'file': h2s_agent.orchestrator.__file__,"
        " 'migrations': sorted(f.name for f in s.MIGRATIONS.iterdir())}))"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe, str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["code"] == 0 and "1.0.0" in run.stdout, "agent démarrable depuis les modules natifs"
    assert not Path(result["file"]).exists(), "aucun source derrière le module"
    assert "E004__preuves_locales.sql" in result["migrations"]
    import sign_manifest

    from h2s_agent import integrity

    seed = os.urandom(32)
    (out / "MANIFEST.json").write_text(json.dumps(sign_manifest.sign(base64.b64encode(seed).decode(), out, "1.0.0")))
    assert integrity.verify(sign_manifest.public_key(base64.b64encode(seed).decode()), out).ok, (
        "manifeste signé calculé sur les binaires"
    )
