"""Régénère ``tests/vectors_tpm.json`` (partagé avec ``backend/.../edge/TpmAttestationTest.java``) contre swtpm.

    LD_LIBRARY_PATH=… H2S_EDGE_SWTPM=… python -m tests.gen_vectors_tpm

Contenu : AC de test et preuve d'enrôlement produite par le TPM (certificat EK, AK, TPM2_Certify de la clé d'identité),
requête signée ECDSA par le TPM, défi d'activation calculé par tpm2-pytss avec une graine fixée (le cloud Java doit produire
octet pour octet le même blob). Rien de secret : clés de test jetables.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from h2s_agent import crypto, enrollment, tpm

from . import tpm_sim

OUT = Path(__file__).parent / "vectors_tpm.json"
AGENT_UUID = "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e"
CODE = "H2S-ABCD-EFGH-JKMN-PQRS"


def main() -> None:
    sim = tpm_sim.start()
    try:
        keys = tpm.Tpm2KeyStore(sim.tcti)
        public = keys.public_key()
        code_hash = enrollment.code_hash(CODE)
        nonce = enrollment.attestation_nonce(AGENT_UUID, public, code_hash)
        evidence = keys.attestation(nonce)
        body = b'{"records":[]}'
        canonical = crypto.canonical(
            "POST",
            "/api/agent/sync/buffer",
            1789000000,
            "0123456789abcdef0123456789abcdef",
            body,
        )
        signature = keys.sign(canonical.encode())
        # Preuve locale (E188) signée par le TPM : le cloud Java la vérifie avec la clé enrôlée.
        proof = json.dumps(
            {
                "v": 1,
                "type": "local_proof",
                "agent_uuid": AGENT_UUID,
                "site_id": 4,
                "period_start": "2026-09-14T08:00:00Z",
                "period_end": "2026-09-14T08:30:00Z",
                "offline": True,
                "devices": [
                    {
                        "device": "device:5",
                        "energy_wh": 580.0,
                        "power_avg_w": 1200.0,
                        "samples": 30,
                        "estimated": False,
                    }
                ],
                "batteries": [{"stock_id": 1, "soc_start": 80.0, "soc_end": 65.5, "samples": 30}],
                "decisions": [
                    {
                        "decision_ref": None,
                        "stock_id": 1,
                        "action": "DISCHARGE",
                        "power_w": 800.0,
                        "source": "CLOUD_CACHE",
                        "execution_status": "EXECUTED",
                        "executed_at": "2026-09-14T08:01:00Z",
                    }
                ],
                "measurements": {"count": 90, "sha256": "0" * 64},
                "prev_hash": None,
                "key_algorithm": "ECDSA-P256",
                "created_at": "2026-09-14T08:33:00Z",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        proof_signature = keys.sign(proof.encode())
        keys.close()
    finally:
        sim.stop()

    from tpm2_pytss import TPM2B_NAME, TPM2B_PUBLIC
    from tpm2_pytss.utils import NoSuchIndex, create_ek_template, make_credential

    def missing(index):
        raise NoSuchIndex(int(index))

    ek_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _, template = create_ek_template("EK-RSA2048", missing)
    template.publicArea.unique.rsa = ek_private.public_key().public_numbers().n.to_bytes(256, "big")
    ak_name = bytes.fromhex("000b") + hashlib.sha256(b"ak de test").digest()
    seed = bytes(range(32))
    secret = b"secret-du-cloud-32-octets-exact!"
    with mock.patch("tpm2_pytss.internal.crypto.secrets.token_bytes", lambda n: seed[:n]):
        blob, encrypted = make_credential(TPM2B_PUBLIC(publicArea=template.publicArea), secret, TPM2B_NAME(ak_name))

    OUT.write_text(
        json.dumps(
            {
                "agent_uuid": AGENT_UUID,
                "code": CODE,
                "code_hash": code_hash,
                "public_key": public,
                "nonce_hex": nonce.hex(),
                "ca_pem": sim.ca_pem.decode("ascii"),
                "other_ca_pem": tpm_sim.make_ca()[1].public_bytes(serialization.Encoding.PEM).decode("ascii"),
                "evidence": evidence,
                "request": {
                    "method": "POST",
                    "path": "/api/agent/sync/buffer",
                    "timestamp": 1789000000,
                    "nonce": "0123456789abcdef0123456789abcdef",
                    "body": body.decode(),
                    "canonical": canonical,
                    "signature": signature,
                },
                "proof": {
                    "payload": proof,
                    "signature": proof_signature,
                    "hash": crypto.sha256_hex(proof.encode()),
                },
                "credential": {
                    "ek_private_pkcs8": base64.b64encode(
                        ek_private.private_bytes(
                            serialization.Encoding.DER,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption(),
                        )
                    ).decode("ascii"),
                    "ak_name_hex": ak_name.hex(),
                    "seed_hex": seed.hex(),
                    "secret_hex": secret.hex(),
                    "credential_blob": base64.b64encode(blob.marshal()).decode("ascii"),
                    "encrypted_secret": base64.b64encode(encrypted.marshal()).decode("ascii"),
                },
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{OUT} écrit")


if __name__ == "__main__":
    main()
