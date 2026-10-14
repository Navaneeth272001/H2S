"""TPM 2.0 logiciel pour les tests : ``swtpm`` (libtpms) démarré à la demande, EK certifié par une AC de test.

Ce n'est **pas** un TPM matériel : il exécute les mêmes commandes TPM 2.0 que le module du boîtier, sans en avoir les
protections physiques. L'agent le signale comme simulateur (TCTI ``swtpm``), et le cloud ne l'atteste que si l'AC de test
est déclarée comme racine de confiance, ce que seuls les tests font.

Binaire : ``H2S_EDGE_SWTPM`` (chemin de ``swtpm``) ou ``swtpm`` du PATH ; ``tpm2-pytss`` importable (bibliothèques tpm2-tss).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def swtpm_binary() -> str | None:
    return os.environ.get("H2S_EDGE_SWTPM") or shutil.which("swtpm")


def unavailable() -> str | None:
    """Motif d'absence du simulateur, ou ``None`` s'il est utilisable."""
    if not swtpm_binary():
        return "swtpm absent (H2S_EDGE_SWTPM ou PATH)"
    try:
        import tpm2_pytss  # noqa: F401
    except (ImportError, OSError) as exc:
        return f"tpm2-pytss non importable : {exc}"
    return None


def _free_ports() -> int:
    """Deux ports libres consécutifs : le TCTI swtpm joint le canal de contrôle sur le port suivant celui des commandes."""
    for _ in range(50):
        with socket.socket() as a:
            a.bind(("127.0.0.1", 0))
            port = a.getsockname()[1]
            if port >= 65535:
                continue
            with socket.socket() as b:
                try:
                    b.bind(("127.0.0.1", port + 1))
                except OSError:
                    continue
                return port
    raise RuntimeError("aucune paire de ports libres pour swtpm")


@dataclass
class SimulatedTpm:
    tcti: str
    ca_pem: bytes
    ca_key: rsa.RSAPrivateKey
    process: subprocess.Popen
    state: Path

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.state, ignore_errors=True)


def make_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "H2S tests"),
            x509.NameAttribute(NameOID.COMMON_NAME, "AC EK de test (swtpm)"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def start(provision_ek_certificate: bool = True) -> SimulatedTpm:
    state = Path(tempfile.mkdtemp(prefix="h2s-swtpm-"))
    port = _free_ports()
    ctrl = port + 1
    process = subprocess.Popen(
        [
            swtpm_binary(),
            "socket",
            "--tpm2",
            "--tpmstate",
            f"dir={state}",
            "--server",
            f"type=tcp,port={port},bindaddr=127.0.0.1",
            "--ctrl",
            f"type=tcp,port={ctrl},bindaddr=127.0.0.1",
            "--flags",
            "not-need-init,startup-clear",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if time.monotonic() > deadline or process.poll() is not None:
                process.kill()
                raise RuntimeError("swtpm n'a pas démarré")
            time.sleep(0.05)
    # swtpm ne sert qu'un client à la fois : la connexion de sonde doit être fermée avant la suivante.
    time.sleep(0.05)
    tcti = f"swtpm:host=127.0.0.1,port={port}"
    ca_key, ca_cert = make_ca()
    sim = SimulatedTpm(tcti, ca_cert.public_bytes(serialization.Encoding.PEM), ca_key, process, state)
    if provision_ek_certificate:
        _provision(sim, ca_key, ca_cert)
    return sim


def _provision(sim: SimulatedTpm, ca_key: rsa.RSAPrivateKey, ca_cert: x509.Certificate) -> None:
    """Ce que fait le fabricant : EK RSA 2048 du profil TCG, certificat signé par son AC, déposé à l'index NV 0x01C00002."""
    from tpm2_pytss import (
        ESAPI,
        ESYS_TR,
        TPM2_ALG,
        TPM2B_NV_PUBLIC,
        TPM2B_SENSITIVE_CREATE,
        TPMA_NV,
        TPMS_NV_PUBLIC,
    )
    from tpm2_pytss.utils import NoSuchIndex, create_ek_template

    def missing(index):
        raise NoSuchIndex(int(index))

    with ESAPI(sim.tcti) as ctx:
        _, template = create_ek_template("EK-RSA2048", missing)
        ek, public, *_ = ctx.create_primary(TPM2B_SENSITIVE_CREATE(), template, ESYS_TR.ENDORSEMENT)
        ctx.flush_context(ek)
        n = int.from_bytes(bytes(public.publicArea.unique.rsa), "big")
        ek_key = rsa.RSAPublicNumbers(65537, n).public_key()
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "swtpm EK")]))
            .issuer_name(ca_cert.subject)
            .public_key(ek_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, True, False, False, False, False, False, False),
                critical=True,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        der = cert.public_bytes(serialization.Encoding.DER)
        nv = TPM2B_NV_PUBLIC(
            nvPublic=TPMS_NV_PUBLIC(
                nvIndex=0x01C00002,
                nameAlg=TPM2_ALG.SHA256,
                attributes=TPMA_NV.OWNERWRITE | TPMA_NV.OWNERREAD | TPMA_NV.AUTHREAD | TPMA_NV.AUTHWRITE,
                dataSize=len(der),
            )
        )
        handle = ctx.nv_define_space(b"", nv, ESYS_TR.OWNER)
        for offset in range(0, len(der), 512):
            ctx.nv_write(handle, der[offset : offset + 512], offset, ESYS_TR.OWNER)
