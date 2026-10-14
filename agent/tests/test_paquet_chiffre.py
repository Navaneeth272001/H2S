"""Paquet chiffré déchiffré dynamiquement (E177, CA-18.30) : publié chiffré, clé ouverte par la clé de code du boîtier (fichier
0600 ou TPM 2.0), code importé depuis la mémoire, aucune écriture en clair (contrôlé par un crochet d'audit de l'interpréteur)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from h2s_agent import __version__ as AGENT_VERSION
from h2s_boot import keys, package

from . import tpm_sim
from .test_tpm import swtpm  # noqa: F401 - fixture

AGENT = Path(__file__).resolve().parents[1]


def publication() -> tuple[bytes, str]:
    seed = os.urandom(32)
    public = (
        ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return seed, base64.urlsafe_b64encode(public).decode().rstrip("=")


def install(tmp_path: Path) -> tuple[Path, Path, str]:
    """Poste de publication puis boîtier : amorce en clair, paquet chiffré, clé du paquet pour la clé de code de ce boîtier."""
    seed, public = publication()
    app, data = tmp_path / "app", tmp_path / "data"
    shutil.copytree(
        AGENT / "h2s_boot",
        app / "h2s_boot",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    envelope, cek = package.encrypt(AGENT / "h2s_agent", "h2s_agent", "1.0.0", seed)
    (app / "h2s_agent.enc").write_text(json.dumps(envelope))
    wrapped = package.wrap_key(cek, keys.software_public(data / "keys" / "code.pem"))
    (data / "keys" / "package.key").write_text(base64.b64encode(wrapped).decode())
    config = tmp_path / "agent.json"
    config.write_text(
        json.dumps(
            {
                "cloud_url": "https://api.h2s.example",
                "db_url": "postgresql://x@/y",
                "data_dir": str(data),
                "package_public_key": public,
            }
        )
    )
    return app, config, public


PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
written = []
def audit(event, args):
    if event == "open" and isinstance(args[0], str):
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        if (flags & 0o3) or any(c in (mode or "") for c in "wax+") or args[0].endswith((".py", ".pyc")) and "h2s_agent" in args[0]:
            written.append(args[0])
sys.addaudithook(audit)
from h2s_boot.__main__ import _settings, load
header = load(_settings(sys.argv[2]), __import__("pathlib").Path(sys.argv[1]))
import inspect, h2s_agent, h2s_agent.orchestrator, h2s_agent.storage, h2s_agent.__main__ as m
migrations = sorted(f.name for f in h2s_agent.storage.MIGRATIONS.iterdir())
try:
    inspect.getsource(h2s_agent.orchestrator)
    source = True
except (OSError, TypeError):
    source = False
code = m.main(["version"])
during = list(written)
with open(sys.argv[3], "w") as control:  # témoin : le crochet voit bien une écriture
    control.write("x")
print(json.dumps({"header": header["version"], "file": h2s_agent.orchestrator.__file__, "migrations": migrations, "source": source,
                  "written": during, "control": sys.argv[3] in written, "code": code,
                  "modules": sorted(k for k in sys.modules if k.startswith("h2s_agent"))}))
"""


class TestPaquetChiffre:
    def test_execute_depuis_la_memoire_sans_ecriture_en_clair(self, tmp_path):
        app, config, _ = install(tmp_path)
        assert not list(app.rglob("h2s_agent/*.py")) and not (app / "h2s_agent").exists(), (
            "aucune source de l'agent dans l'installation"
        )
        out = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                PROBE,
                str(app),
                str(config),
                str(tmp_path / "temoin.txt"),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp_path,
            check=False,
        )
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["control"] is True, "crochet d'audit opérant"
        assert result["written"] == [], "aucune écriture pendant le déchiffrement, l'import et l'exécution"
        assert result["file"].startswith("/h2s-enc/") and not Path(result["file"]).exists(), (
            "module importé depuis la mémoire"
        )
        assert result["source"] is False, "le chargeur ne rend pas le source"
        assert {"E001__schema_client.sql", "E004__preuves_locales.sql"} <= set(result["migrations"]), (
            "migrations lues dans l'archive en mémoire"
        )
        assert result["code"] == 0 and AGENT_VERSION in out.stdout and "h2s_agent.decisions" in result["modules"]
        assert not list(tmp_path.rglob("__pycache__")), "pas de bytecode écrit"

    def test_paquet_altere_signature_fausse_ou_autre_boitier_refuses(self, tmp_path):
        app, config, public = install(tmp_path)
        envelope = json.loads((app / "h2s_agent.enc").read_text())
        data = tmp_path / "data" / "keys"
        wrapped = base64.b64decode((data / "package.key").read_text())
        unwrap = lambda w: keys.software_unwrap(data / "code.pem", w)
        header, plain = package.decrypt(envelope, public, unwrap, wrapped)
        assert header["package"] == "h2s_agent" and plain[:2] == b"PK"
        raw = bytearray(base64.b64decode(envelope["ciphertext"]))
        raw[100] ^= 1
        with pytest.raises(ValueError, match="chiffré modifié"):
            package.decrypt(
                dict(envelope, ciphertext=base64.b64encode(bytes(raw)).decode()),
                public,
                unwrap,
                wrapped,
            )
        with pytest.raises(ValueError, match="signature"):
            package.decrypt(envelope, publication()[1], unwrap, wrapped)
        forged = json.loads(envelope["payload"])
        forged["version"] = "9.9.9"
        with pytest.raises(ValueError, match="signature"):
            package.decrypt(dict(envelope, payload=json.dumps(forged)), public, unwrap, wrapped)
        with pytest.raises(ValueError, match="indéchiffrable"):
            package.decrypt(
                envelope,
                public,
                lambda w: keys.software_unwrap(tmp_path / "autre" / "code.pem", w),
                wrapped,
            )
        boot = f"import sys; sys.path.insert(0, {str(app)!r}); from h2s_boot.__main__ import main; sys.exit(main(sys.argv[1:]))"
        out = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                boot,
                "--config",
                str(config),
                "--app",
                str(app),
                "version",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp_path,
            check=False,
        )
        assert out.returncode == 0 and out.stdout.strip() == AGENT_VERSION, out.stderr
        (app / "h2s_agent.enc").write_text(json.dumps(dict(envelope, ciphertext=base64.b64encode(bytes(raw)).decode())))
        out = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                boot,
                "--config",
                str(config),
                "--app",
                str(app),
                "version",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp_path,
            check=False,
        )
        assert out.returncode == 4 and "chiffré modifié" in out.stderr, "paquet altéré : l'agent ne démarre pas"

    def test_cle_du_paquet_ouverte_par_le_tpm(self, swtpm):  # noqa: F811
        from h2s_agent import tpm

        seed, public = publication()
        envelope, cek = package.encrypt(AGENT / "h2s_agent", "h2s_agent", "1.0.0", seed)
        store = tpm.Tpm2KeyStore(swtpm.tcti)
        try:
            code_public = store.code_public_key()
            wrapped = package.wrap_key(cek, code_public)
            header, _plain = package.decrypt(envelope, public, store.unwrap_key, wrapped)
            assert header["version"] == "1.0.0" and store.code_public_key() == code_public, (
                "clé de code persistante dans le TPM"
            )
            with pytest.raises(ValueError):
                store.unwrap_key(
                    package.wrap_key(
                        cek,
                        keys.software_public(Path(tpm_sim.tempfile.mkdtemp()) / "code.pem"),
                    )[:-1]
                    + b"\x00"
                )
        finally:
            store.close()
