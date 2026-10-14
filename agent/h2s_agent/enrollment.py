"""Enrôlement de l'agent (D249) et preuve TPM de son identité (§8 B1, E187).

1. L'agent crée ou lit sa clé d'identité (``KeyStore``). En magasin ``tpm2``, il joint une **preuve** : certificat EK du
   fabricant, clé d'attestation (AK) et ``TPM2_Certify`` de sa clé d'identité par l'AK, sur un défi qui lie la preuve à cet
   enrôlement (``attestation_nonce`` : identifiant de l'agent, clé publique, empreinte du code).
2. Le cloud vérifie la chaîne EK contre les AC des fabricants qu'il a déclarées, la signature de l'AK et les attributs de la
   clé (non exportable), puis renvoie un **défi d'activation** chiffré pour l'EK du certificat et lié au nom de l'AK.
3. Seul le TPM qui porte cet EK et cet AK rend le secret (``TPM2_ActivateCredential``) ; l'agent le renvoie par une requête
   signée de sa nouvelle clé : le cloud passe l'agent en « TPM 2.0 attesté ».

Sans AC de fabricant configurée côté cloud, l'agent reste « TPM déclaré, non attesté » : jamais d'attestation supposée.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

from .crypto import KeyStore, sha256_hex

NONCE_PREFIX = "H2S-TPM-ENROLL-V1"


def code_hash(code: str) -> str:
    """Empreinte du code d'enrôlement, identique à ``EnrollmentService.hash`` du cloud."""
    return sha256_hex(code.strip().upper().encode("utf-8"))


def attestation_nonce(agent_uuid: str, public_key: str, enrollment_code_hash: str) -> bytes:
    return hashlib.sha256(f"{NONCE_PREFIX}\n{agent_uuid}\n{public_key}\n{enrollment_code_hash}".encode()).digest()


def enroll(cloud, keys: KeyStore, code: str, agent_version: str, agent_uuid: str | None = None) -> dict:
    """Inscrit l'agent ; en TPM, résout le défi d'activation. Renvoie la réponse d'inscription complétée de ``tpm_attestation``."""
    agent_uuid = agent_uuid or str(uuid.uuid4())
    evidence = None
    if keys.kind == "TPM2":
        evidence = keys.attestation(attestation_nonce(agent_uuid, keys.public_key(), code_hash(code)))  # type: ignore[attr-defined]
    registered = cloud.register(code.strip(), agent_uuid, agent_version, evidence)
    challenge = registered.get("attestation_challenge")
    if challenge:
        cloud.identify(agent_uuid)
        secret = keys.activate_credential(
            base64.b64decode(challenge["credential_blob"]),  # type: ignore[attr-defined]
            base64.b64decode(challenge["encrypted_secret"]),
        )
        answer = cloud.attest(base64.b64encode(secret).decode("ascii"))
        registered["tpm_attestation"] = answer.get("tpm_attestation")
    return registered
