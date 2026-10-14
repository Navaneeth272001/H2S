"""TPM 2.0 de l'agent (E174, E175, E180, E187) : sonde, clé ECDSA non exportable, signature des requêtes, preuve d'enrôlement.

Contre ``swtpm`` (TPM logiciel, voir ``tpm_sim``) : mêmes commandes TPM 2.0 que le module du boîtier. Sans simulateur, les
tests qui en ont besoin sont ignorés avec leur motif ; la sonde « absent » et la configuration se vérifient partout.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from h2s_agent import config as config_mod
from h2s_agent import crypto, tpm

from . import tpm_sim


@pytest.fixture
def swtpm():
    reason = tpm_sim.unavailable()
    if reason:
        pytest.skip(reason)
    sim = tpm_sim.start()
    yield sim
    sim.stop()


def ecdsa_public(key_b64url: str) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), crypto.b64url_decode(key_b64url))


class TestSonde:
    def test_sans_peripherique_tpm_absent(self, tmp_path):
        state = tpm.probe(f"device:{tmp_path / 'tpmrm0'}")
        assert state["status"] == "ABSENT" and not state["simulator"], "CA-18.27 : pas de module, dit absent"
        with pytest.raises(tpm.TpmError):
            tpm.Tpm2KeyStore(f"device:{tmp_path / 'tpmrm0'}")

    def test_simulateur_operationnel_toujours_signale_comme_simulateur(self, swtpm):
        state = tpm.probe(swtpm.tcti)
        assert state["status"] == "OPERATIONAL", state
        assert state["simulator"] is True and state["family"] == "2.0" and state["ek_certificate"] is True


class TestCleTpm:
    def test_requete_signee_ecdsa_par_le_tpm_et_cle_persistante(self, swtpm):
        keys = tpm.Tpm2KeyStore(swtpm.tcti)
        try:
            assert keys.kind == "TPM2" and keys.algorithm == "ECDSA-P256"
            public = keys.public_key()
            assert len(public) == 87, "point P-256 non compressé en base64url"
            headers = crypto.signed_headers(
                keys,
                "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
                "POST",
                "/api/agent/sync/gradients",
                1789000000,
                b'{"x":1}',
            )
            canonical = crypto.canonical(
                "POST",
                "/api/agent/sync/gradients",
                1789000000,
                headers["X-H2S-Nonce"],
                b'{"x":1}',
            ).encode()
            raw = crypto.b64url_decode(headers["X-H2S-Signature"])
            assert len(raw) == 64 and len(headers["X-H2S-Signature"]) <= 100
            der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
            ecdsa_public(public).verify(der, canonical, ec.ECDSA(hashes.SHA256()))
            assert crypto.verify_agent_signature("ECDSA-P256", public, canonical, headers["X-H2S-Signature"])
            assert not crypto.verify_agent_signature(
                "ECDSA-P256", public, canonical + b"!", headers["X-H2S-Signature"]
            ), "corps altéré"
        finally:
            keys.close()
        again = tpm.Tpm2KeyStore(swtpm.tcti)
        try:
            assert again.public_key() == public, "clé persistante dans le TPM : même identité au redémarrage"
        finally:
            again.close()

    def test_cle_non_exportable(self, swtpm):
        from tpm2_pytss import TPMA_OBJECT, TPMT_PUBLIC

        keys = tpm.Tpm2KeyStore(swtpm.tcti)
        try:
            public, _ = TPMT_PUBLIC.unmarshal(base64.b64decode(keys.attestation(b"x" * 32)["key_public"]))
            attrs = public.objectAttributes
            assert (
                attrs & TPMA_OBJECT.FIXEDTPM
                and attrs & TPMA_OBJECT.FIXEDPARENT
                and attrs & TPMA_OBJECT.SENSITIVEDATAORIGIN
            )
        finally:
            keys.close()


class TestAttestation:
    def test_preuve_certify_verifiee_et_defi_du_cloud_resolu_par_le_tpm(self, swtpm):
        from tpm2_pytss import (
            TPM2B_NAME,
            TPM2B_PUBLIC,
            TPMS_ATTEST,
            TPMT_PUBLIC,
            TPMT_SIGNATURE,
        )
        from tpm2_pytss.utils import make_credential

        keys = tpm.Tpm2KeyStore(swtpm.tcti)
        try:
            nonce = hashlib.sha256(b"defi").digest()
            proof = keys.attestation(nonce)
            ek_cert = x509.load_der_x509_certificate(base64.b64decode(proof["ek_certificate"]))
            ca = x509.load_pem_x509_certificate(swtpm.ca_pem)
            ek_cert.verify_directly_issued_by(ca)
            ak, _ = TPMT_PUBLIC.unmarshal(base64.b64decode(proof["ak_public"]))
            key, _ = TPMT_PUBLIC.unmarshal(base64.b64decode(proof["key_public"]))
            info, _ = TPMS_ATTEST.unmarshal(base64.b64decode(proof["certify_info"]))
            signature, _ = TPMT_SIGNATURE.unmarshal(base64.b64decode(proof["certify_signature"]))
            assert bytes(info.extraData) == nonce, "preuve liée au défi : pas de rejeu"
            assert bytes(info.attested.certify.name) == bytes(key.get_name()), (
                "c'est bien la clé d'identité qui est certifiée"
            )
            ak_key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(bytes(ak.unique.ecc.x), "big"),
                int.from_bytes(bytes(ak.unique.ecc.y), "big"),
                ec.SECP256R1(),
            ).public_key()
            der = utils.encode_dss_signature(
                int.from_bytes(bytes(signature.signature.ecdsa.signatureR), "big"),
                int.from_bytes(bytes(signature.signature.ecdsa.signatureS), "big"),
            )
            ak_key.verify(der, base64.b64decode(proof["certify_info"]), ec.ECDSA(hashes.SHA256()))
            assert proof["simulator"] is True
            # Défi du cloud : secret chiffré pour l'EK du certificat et lié au nom de l'AK ; seul ce TPM le rend.
            from tpm2_pytss.utils import NoSuchIndex, create_ek_template

            def missing(index):
                raise NoSuchIndex(int(index))

            _, template = create_ek_template("EK-RSA2048", missing)
            template.publicArea.unique.rsa = ek_cert.public_key().public_numbers().n.to_bytes(256, "big")
            blob, secret = make_credential(
                TPM2B_PUBLIC(publicArea=template.publicArea),
                b"secret-du-cloud-32-octets-exact!",
                TPM2B_NAME(ak.get_name()),
            )
            assert keys.activate_credential(blob.marshal(), secret.marshal()) == b"secret-du-cloud-32-octets-exact!"
            other = TPMT_PUBLIC.unmarshal(base64.b64decode(proof["key_public"]))[0]
            blob2, secret2 = make_credential(
                TPM2B_PUBLIC(publicArea=template.publicArea),
                b"x" * 32,
                TPM2B_NAME(other.get_name()),
            )
            from tpm2_pytss import TSS2_Exception

            with pytest.raises(TSS2_Exception):
                keys.activate_credential(blob2.marshal(), secret2.marshal())
        finally:
            keys.close()


class TestConfiguration:
    def base(self, tmp_path: Path, **extra) -> Path:
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "cloud_url": "https://api.h2s.example",
                    "db_url": "postgresql://x@/y",
                    **extra,
                }
            )
        )
        return path

    def test_magasin_logiciel_par_defaut_tpm_exige_quand_le_client_l_active(self, tmp_path):
        assert config_mod.load(self.base(tmp_path)).key_store == "software"
        c = config_mod.load(self.base(tmp_path, key_store="tpm2"))
        assert c.key_store == "tpm2" and c.tpm_tcti == "device:/dev/tpmrm0"
        with pytest.raises(ValueError, match="tpm2"):
            config_mod.load(self.base(tmp_path, require_tpm=True))
        with pytest.raises(ValueError, match="simulateur"):
            config_mod.load(
                self.base(
                    tmp_path,
                    key_store="tpm2",
                    require_tpm=True,
                    tpm_tcti="swtpm:host=127.0.0.1,port=2321",
                )
            )
        with pytest.raises(ValueError):
            config_mod.load(self.base(tmp_path, key_store="hsm"))

    def test_magasin_logiciel_ed25519_inchange(self, tmp_path):
        keys = crypto.SoftwareKeyStore(tmp_path / "keys" / "agent.pem")
        assert keys.algorithm == "Ed25519" and keys.kind == "SOFTWARE" and len(keys.public_key()) == 43
        assert crypto.verify_agent_signature("Ed25519", keys.public_key(), b"m", keys.sign(b"m"))


class FakeEnrollmentCloud:
    """Cloud d'enrôlement : vérifie la preuve comme ``TpmAttestation.verify`` (chaîne EK, défi, clé certifiée) puis pose le défi."""

    def __init__(self, ca_pem: bytes, public_key: str) -> None:
        self.ca = x509.load_pem_x509_certificate(ca_pem)
        self.public_key = public_key
        self.secret = b"s" * 32
        self.agent = None
        self.body = None

    def register(self, code, agent_uuid, agent_version, attestation=None):
        from tpm2_pytss import TPM2B_NAME, TPM2B_PUBLIC, TPMS_ATTEST, TPMT_PUBLIC
        from tpm2_pytss.utils import NoSuchIndex, create_ek_template, make_credential

        from h2s_agent import enrollment

        self.body = attestation
        ek = x509.load_der_x509_certificate(base64.b64decode(attestation["ek_certificate"]))
        ek.verify_directly_issued_by(self.ca)
        key, _ = TPMT_PUBLIC.unmarshal(base64.b64decode(attestation["key_public"]))
        info, _ = TPMS_ATTEST.unmarshal(base64.b64decode(attestation["certify_info"]))
        assert bytes(info.extraData) == enrollment.attestation_nonce(
            agent_uuid, self.public_key, enrollment.code_hash(code)
        )
        assert bytes(info.attested.certify.name) == bytes(key.get_name())
        ak, _ = TPMT_PUBLIC.unmarshal(base64.b64decode(attestation["ak_public"]))

        def missing(index):
            raise NoSuchIndex(int(index))

        _, template = create_ek_template("EK-RSA2048", missing)
        template.publicArea.unique.rsa = ek.public_key().public_numbers().n.to_bytes(256, "big")
        blob, secret = make_credential(
            TPM2B_PUBLIC(publicArea=template.publicArea),
            self.secret,
            TPM2B_NAME(ak.get_name()),
        )
        return {
            "agent_uuid": agent_uuid,
            "site_id": 4,
            "tpm_attestation": "PENDING",
            "attestation_challenge": {
                "credential_blob": base64.b64encode(blob.marshal()).decode(),
                "encrypted_secret": base64.b64encode(secret.marshal()).decode(),
            },
            "keys": {
                "decision_key_id": "k",
                "decision_public_key": "p",
                "gradient_public_key": "g",
            },
        }

    def identify(self, agent_uuid):
        self.agent = agent_uuid

    def attest(self, secret_b64):
        return {"tpm_attestation": "ATTESTED" if base64.b64decode(secret_b64) == self.secret else "FAILED"}


class TestEnrolement:
    def test_enrolement_tpm_preuve_puis_defi_resolu(self, swtpm):
        from h2s_agent import enrollment

        keys = tpm.Tpm2KeyStore(swtpm.tcti)
        try:
            cloud = FakeEnrollmentCloud(swtpm.ca_pem, keys.public_key())
            registered = enrollment.enroll(
                cloud,
                keys,
                " h2s-abcd-efgh-jkmn-pqrs ",
                "1.1.0",
                "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
            )
            assert registered["tpm_attestation"] == "ATTESTED", "défi résolu par le TPM, renvoyé signé par la clé TPM"
            assert cloud.agent == "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e"
            assert cloud.body["format"] == "TPM2-CERTIFY-V1" and cloud.body["simulator"] is True
        finally:
            keys.close()

    def test_empreinte_du_code_et_defi_identiques_au_cloud(self):
        from h2s_agent import enrollment

        v = json.loads((Path(__file__).parent / "vectors_tpm.json").read_text())
        assert enrollment.code_hash(v["code"].lower()) == v["code_hash"], (
            "EnrollmentService.hash : code rogné et en majuscules"
        )
        assert enrollment.attestation_nonce(v["agent_uuid"], v["public_key"], v["code_hash"]).hex() == v["nonce_hex"]
