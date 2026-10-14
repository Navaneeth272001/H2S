"""Paquet sans .py (E178, CA-18.31) : compile ``h2s_agent`` et l'amorce ``h2s_boot`` en modules natifs avec Nuitka.

    python tools/build_native.py --out dist/native        # sur l'architecture et le Python des boîtiers (arm64, 3.11)

Produit ``<out>/h2s_agent.<abi>.so``, ``<out>/h2s_boot.<abi>.so`` et ``<out>/h2s_agent/migrations/*.sql`` : aucun fichier .py ni
bytecode. Le manifeste signé se calcule ensuite sur ces binaires (``tools/sign_manifest.py --root <out>``), le service systemd
lance ``python -I -c "from h2s_agent.__main__ import main; main()"`` depuis ``<out>``. Le paquet compilé peut aussi être
chiffré (``tools/publish_package.py build --source …``) : les modules natifs se chargent alors par ``memfd`` (hors prototype E177).

Nuitka (licence Apache 2.0) remplace PyArmor (licence commerciale) : même résultat pour CA-18.31. Ce n'est pas une protection
contre un attaquant maître du boîtier : un module natif se désassemble ; la carte chiffrée (LUKS lié au TPM) et Secure Boot
restent nécessaires (analyse 18 §7.6).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
PACKAGES = ("h2s_agent", "h2s_boot")


def nuitka_available() -> str | None:
    """Motif d'indisponibilité, ou ``None`` si Nuitka et un compilateur C sont présents."""
    try:
        import nuitka  # noqa: F401
    except ImportError:
        return "Nuitka absent : pip install -r requirements-build.txt"
    if not (shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")):
        return "compilateur C absent (gcc)"
    include = Path(sysconfig.get_paths()["include"]) / "Python.h"
    if not include.exists():
        return f"en-têtes Python absents ({include}) : paquet python3-dev"
    return None


def build(out: Path, jobs: int = 4) -> Path:
    reason = nuitka_available()
    if reason:
        raise RuntimeError(reason)
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".build"
    for name in PACKAGES:
        cmd = [
            sys.executable,
            "-m",
            "nuitka",
            "--mode=module",
            str(AGENT / name),
            f"--include-package={name}",
            f"--output-dir={work}",
            "--remove-output",
            "--no-pyi-file",
            f"--jobs={jobs}",
            "--assume-yes-for-downloads",
            "--quiet",
            "--python-flag=no_docstrings",
        ]
        subprocess.run(cmd, check=True, cwd=AGENT)
        for so in work.glob(f"{name}.*.so"):
            shutil.move(str(so), out / so.name)
    shutil.rmtree(work, ignore_errors=True)
    migrations = out / "h2s_agent" / "migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    for sql in (AGENT / "h2s_agent" / "migrations").glob("*.sql"):
        shutil.copy2(sql, migrations / sql.name)
    leftovers = [str(p) for p in out.rglob("*") if p.suffix in (".py", ".pyc", ".pyi")]
    if leftovers:
        raise RuntimeError(f"fichiers Python dans l'artefact : {leftovers[:5]}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile l'agent H2S en modules natifs (Nuitka)")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        out = build(args.out, args.jobs)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"compilation impossible : {exc}", file=sys.stderr)
        return 1
    print(f"{out} : {', '.join(sorted(p.name for p in out.glob('*.so')))} (aucun .py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
