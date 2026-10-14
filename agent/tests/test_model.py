"""Calcul pur, sans base : règles SEC1-6, lecture des équipements, cryptographie, fédéré, intégrité."""

from __future__ import annotations

import base64
import datetime as dt
import inspect
import json
import os
import pathlib
import re
import stat
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from h2s_agent import crypto, federated, integrity, parsers, rules
from h2s_agent.clock import parse_instant

T0 = dt.datetime(2026, 9, 14, 8, 0, tzinfo=dt.timezone.utc)
VECTORS = Path(__file__).parent / "vectors.json"


class TestRules:
    """Mêmes verdicts que com.h2s.battery.rules.RuleEngine (brique 6)."""

    t = rules.Thresholds()

    def test_standby_toujours_admis_et_domaines(self):
        assert rules.validate(self.t, "STANDBY", 0, None, T0, "CHARGE", T0).valid
        assert rules.validate(self.t, "CHARGE", 100, None, T0, None, None).rule == "REQUEST", (
            "SoC inconnu : pas de charge"
        )
        assert rules.validate(self.t, "CHARGE", -5, 50, T0, None, None).rule == "REQUEST"
        assert rules.validate(self.t, "BOOST", 5, 50, T0, None, None).rule == "REQUEST"

    def test_sec1_a_sec6(self):
        assert rules.validate(self.t, "CHARGE", 100, 50, T0, "DISCHARGE", None).rule == "SEC1"
        assert rules.validate(self.t, "DISCHARGE", 100, 14.9, T0, "STANDBY", None).rule == "SEC2"
        assert rules.validate(self.t, "DISCHARGE", 100, 15, T0, "STANDBY", None).rule == "SEC2", (
            "refus à la borne exacte (R5-07)"
        )
        assert rules.validate(self.t, "CHARGE", 100, 95, T0, "STANDBY", None).rule == "SEC3", (
            "refus à la borne exacte (R5-07)"
        )
        assert rules.validate(self.t, "CHARGE", 100, 95.1, T0, "STANDBY", None).rule == "SEC3"
        r = rules.validate(self.t, "CHARGE", 800, 50, T0, "STANDBY", None)
        assert r.valid and r.power_w == 500 and r.warnings[0].startswith("SEC4"), "ajustée, pas rejetée"
        assert rules.validate(self.t, "DISCHARGE", 1500, 50, T0, "STANDBY", None).power_w == 1000
        assert rules.validate(self.t, "CHARGE", 100, 50, T0, "STANDBY", T0 - dt.timedelta(seconds=10)).rule == "SEC6"
        assert rules.validate(self.t, "CHARGE", 100, 50, T0, "CHARGE", T0 - dt.timedelta(seconds=10)).valid, (
            "sans transition, pas de SEC6"
        )

    def test_vecteurs_communs(self):
        """Mêmes verdicts que RuleEngine (EdgeModelTest#reglesSecCommunes) : refus à la borne exacte (R5-07, CDC §5.2)."""
        for v in json.loads(VECTORS.read_text())["sec_rules"]["vectors"]:
            t = v["thresholds"]
            th = rules.Thresholds(
                t["soc_min"],
                t["soc_max"],
                t["p_charge_max_w"],
                t["p_discharge_max_w"],
                t["min_interval_s"],
            )
            last = None if v["elapsed_s"] is None else T0 - dt.timedelta(seconds=v["elapsed_s"])
            r = rules.validate(th, v["action"], v["power_w"], v["soc"], T0, v["current_state"], last)
            assert (r.valid, r.rule, r.power_w) == (
                v["valid"],
                v["rule"],
                v["power_w_out"],
            ), v["name"]

    def test_bornes_absolues_du_cahier_des_charges(self):
        """R5-06, R5-08 : seuils reçus ramenés à SoC 15–95 % et délai ≥ 30 s ; plus strict conservé (AutoPolicy.Thresholds.of)."""
        for v in json.loads(VECTORS.read_text())["sec_bounds"]["vectors"]:
            r = v["received"]
            t = rules.Thresholds.bounded(r["soc_min"], r["soc_max"], 500, 1000, r["min_interval_s"])
            e = v["effective"]
            assert (t.soc_min, t.soc_max, t.min_transition_interval_s) == (
                e["soc_min"],
                e["soc_max"],
                e["min_interval_s"],
            ), v["name"]

    def test_seuils_incoherents_refuses(self):
        with pytest.raises(ValueError):
            rules.Thresholds(soc_min=90, soc_max=10)


class TestCadences:
    """Les cadences du planificateur, confrontées aux délais que le cloud impose.

    Une cadence n'est juste que relativement à quelque chose : ici, la durée de vie d'une commande
    d'équipement côté cloud. Ce test lit la constante dans la source Java — pas une copie — pour que les
    deux ne puissent pas dériver en silence.
    """

    #: Fichier qui porte la durée de vie côté cloud.
    # Snapshot vendore du contrat cloud (contracts/README.md) — le fichier
    # vivant est dans le repo des services, plus dans celui-ci.
    SERVICE_JAVA = pathlib.Path(__file__).resolve().parents[3] / "contracts/edge-orchestrator/DeviceCommandService.java"

    def ttl_du_cloud(self) -> float:
        if not self.SERVICE_JAVA.exists():
            import pytest
            pytest.skip("contrat cloud DeviceCommandService.java non présent dans ce dépôt")
        texte = self.SERVICE_JAVA.read_text(encoding="utf-8")
        m = re.search(r"Duration TTL = Duration\.of(Minutes|Seconds)\((\d+)\)", texte)
        assert m, "la durée de vie d'une commande n'est plus déclarée sous la forme attendue"
        return float(m.group(2)) * (60 if m.group(1) == "Minutes" else 1)

    def tache_des_commandes(self):
        from h2s_agent.orchestrator import Agent

        source = inspect.getsource(Agent.__init__ if hasattr(Agent, "__init__") else Agent)
        m = re.search(r'Task\("commands",\s*([\d.]+)\s*(?:,\s*[\w.]+)?\s*(?:,\s*pushed_every_s=([\d.]+))?\s*\)', source)
        assert m, "la tâche « commands » n'est plus déclarée sous la forme attendue"
        return float(m.group(1)), (float(m.group(2)) if m.group(2) else None)

    def test_les_commandes_sont_relevees_avant_que_le_cloud_ne_les_expire(self):
        """D734 — le défaut qui rendait « Tester la connexion » impossible.

        Une commande poussée par MQTT n'est pas traitée à son arrivée : elle est mise en file, et c'est le
        sondage « commands » qui la relève. Espacer ce sondage à 300 s quand le canal poussé tient
        retardait donc la commande de 300 s, alors que le cloud l'expire en 120 s. Le filet se déclenchait
        toujours après la mort de la commande : aucun test de connexion ne pouvait aboutir, et l'écran
        disait « sans réponse de l'agent local dans le délai » — ce qui était vrai, et trompeur.
        """
        ttl = self.ttl_du_cloud()
        normal, pousse = self.tache_des_commandes()
        assert normal < ttl, f"sondage {normal} s contre une expiration à {ttl} s"
        assert pousse is None or pousse < ttl, (
            f"en mode poussé, la file est relevée toutes les {pousse} s alors que le cloud expire la"
            f" commande en {ttl} s : le filet ne peut jamais la rattraper"
        )
        # Deux chances au moins dans la fenêtre : une seule tentative tombe en panne avec le moindre aléa.
        assert normal * 2 <= ttl, "au moins deux relevés doivent tenir dans la durée de vie d'une commande"

    def test_la_duree_de_vie_copiee_dans_lagent_est_celle_du_cloud(self):
        """``COMMAND_TTL_S`` sert à détecter un relevé qui ne se fait plus ; il ne vaut que s'il est celui du cloud."""
        from h2s_agent import commands

        assert commands.COMMAND_TTL_S == self.ttl_du_cloud(), (
            "la durée de vie copiée dans l'agent a dérivé de celle du cloud"
        )
        assert commands.COMMANDS_STALE_AFTER_S == 2 * commands.COMMAND_TTL_S

    def test_tout_code_dalerte_de_lagent_est_accepte_par_le_cloud(self):
        """Le cloud tient une liste FERMÉE des codes d'alerte et refuse les autres (audit B18-6) : une alerte dont le
        code n'y figure pas est acquittée comme « illisible » et disparaît sans témoin. C'est précisément le mode de
        défaillance que les alertes sont censées empêcher. La liste de l'agent doit donc être incluse dans celle du cloud."""
        from h2s_agent import monitoring

        java_path = pathlib.Path(__file__).resolve().parents[3] / "contracts/agent-sync/Ingestion.java"
        if not java_path.exists():
            import pytest
            pytest.skip("contrat cloud Ingestion.java non présent dans ce dépôt")
        java = java_path.read_text(encoding="utf-8")
        m = re.search(r"ALERT_CODES = Set\.of\((.*?)\);", java, re.DOTALL)
        assert m, "la liste des codes du cloud n'est plus déclarée sous la forme attendue"
        cloud = set(re.findall(r'"([A-Z_]+)"', m.group(1)))
        manquants = set(monitoring.ALERTS) - cloud
        assert not manquants, f"codes émis par l'agent et refusés par le cloud : {sorted(manquants)}"


class TestParsers:
    def test_shelly_gen2_natif_en_moins_de_5_secondes(self):
        payload = json.dumps(
            {
                "id": 0,
                "output": True,
                "apower": 123.4,
                "voltage": 231.2,
                "current": 0.61,
                "pf": 0.98,
                "aenergy": {"total": 5432.1, "minute_ts": int(T0.timestamp()) - 30},
            }
        ).encode()
        started = time.monotonic()
        readings = parsers.shelly_gen2(3, payload, T0)
        assert time.monotonic() - started < 5, "CA-18.1"
        assert {r.data_type: r.value for r in readings} == {
            "power_w": 123.4,
            "voltage_v": 231.2,
            "current_a": 0.61,
            "energy_total_wh": 5432.1,
            "power_factor": 0.98,
        }
        assert readings[0].at == T0 and readings[0].quality == "OK", (
            "daté à la réception, pas au début de la minute du compteur (CA-18.5)"
        )
        assert parsers.shelly_gen2(3, payload, T0, clock_synced=False)[0].quality == "ESTIMATED"

    def test_notification_rpc_shelly_le_sujet_quune_prise_publie_sans_reglage(self):
        """``<id>/events/rpc`` : le seul sujet qu'une prise sortie d'usine alimente.

        « Generic status update over MQTT » est décoché par défaut sur le micrologiciel Gen2 : sans cette
        case, ``<id>/status/switch:0`` reste muet. Un agent qui n'écoutait que ce sujet ne recevait RIEN
        d'une prise qu'on vient de brancher — ni message, ni rejet, ni relevé, donc rien à diagnostiquer.
        """
        notify = {
            "src": "shellyplusplugs-08f9e0e1a2b3",
            "dst": "shellyplusplugs-08f9e0e1a2b3/events",
            "method": "NotifyStatus",
            "params": {
                "ts": T0.timestamp(),
                "switch:0": {
                    "id": 0,
                    "output": True,
                    "apower": 118.4,
                    "voltage": 232.6,
                    "current": 0.512,
                    "aenergy": {"total": 48213.7, "minute_ts": int(T0.timestamp()) - 40},
                },
            },
        }
        readings = parsers.shelly_gen2_rpc(12, json.dumps(notify).encode(), T0)
        assert {r.data_type: r.value for r in readings} == {
            "power_w": 118.4,
            "voltage_v": 232.6,
            "current_a": 0.512,
            "energy_total_wh": 48213.7,
        }
        assert readings[0].at == T0 and readings[0].quality == "OK"
        notify["method"] = "NotifyFullStatus"
        assert len(parsers.shelly_gen2_rpc(12, json.dumps(notify).encode(), T0)) == 4

    @pytest.mark.parametrize(
        "charge, pourquoi",
        [
            (
                {"method": "NotifyStatus", "params": {"ts": 1.0, "sys": {"uptime": 42}}},
                "une notification de sys ne porte aucune mesure",
            ),
            (
                {"method": "NotifyEvent", "params": {"events": [{"event": "btn_down"}]}},
                "un appui sur le bouton n'est pas un relevé",
            ),
            (
                {"method": "NotifyStatus", "params": {"switch:0": {"id": 0, "output": False}}},
                "un composant peut n'annoncer que son état",
            ),
            ({"method": "NotifyStatus", "params": "pas un objet"}, "params mal formé"),
        ],
    )
    def test_notification_rpc_sans_mesure_nest_pas_un_rejet(self, charge, pourquoi):
        """Liste vide, et non ``ParseError`` : c'est le trafic NORMAL de ce sujet.

        Le compter comme rejet ferait monter le compteur d'erreurs de l'agent et noierait les vrais
        messages faux dans un avertissement par appui de bouton.
        """
        assert parsers.shelly_gen2_rpc(12, json.dumps(charge).encode(), T0) == [], pourquoi

    def test_notification_rpc_compteur_triphase(self):
        notify = {
            "method": "NotifyStatus",
            "params": {"em:0": {"act_power": 2210.5, "voltage": 231.0, "current": 9.6}},
        }
        readings = parsers.shelly_gen2_rpc(7, json.dumps(notify).encode(), T0)
        assert {r.data_type: r.value for r in readings} == {
            "power_w": 2210.5,
            "voltage_v": 231.0,
            "current_a": 9.6,
        }

    def test_date_absente_ou_incoherente_et_horloge_non_synchronisee(self):
        r = parsers.h2s_battery(1, b'{"soc": 55.5, "timestamp": "1999-01-01T00:00:00Z"}', T0)
        assert r[0].at == T0 and r[0].quality == "ESTIMATED"
        assert parsers.h2s_battery(1, b'{"soc": 55.5}', T0, clock_synced=False)[0].quality == "ESTIMATED"

    @pytest.mark.parametrize(
        "payload",
        [
            b"pas du json",
            b"[1,2]",
            b'{"soc": 140}',
            b'{"soc": "NaN"}',
            b"{}",
            b'{"apower": 1e12}',
        ],
    )
    def test_messages_ecartes(self, payload):
        with pytest.raises(parsers.ParseError):
            parsers.h2s_battery(1, payload, T0) if b"apower" not in payload else parsers.shelly_gen2(3, payload, T0)

    def test_meteo_et_passerelle(self):
        w = parsers.h2s_weather(b'{"irradiance_w_m2": 640, "temperature_c": 21.5}', T0)
        assert {x.device_key for x in w} == {"weather"}
        d = parsers.h2s_device(7, b'{"power_w": 1500, "energy_wh": 125}', T0)
        assert {x.data_type for x in d} == {"power_w", "energy_wh"} and d[0].device_key == "device:7"


class TestCrypto:
    def test_signature_de_requete_identique_au_cloud_java(self):
        """Vecteur partagé avec com.h2s.edge.EdgeModelTest : même clé, même chaîne, même signature (Ed25519 est déterministe)."""
        v = json.loads(VECTORS.read_text())
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v["agent_seed_hex"]))
        pub = crypto.b64url(
            key.public_key().public_bytes(crypto.serialization.Encoding.Raw, crypto.serialization.PublicFormat.Raw)
        )
        assert pub == v["agent_public_key"]
        text = crypto.canonical(v["method"], v["path"], v["timestamp"], v["nonce"], v["body"].encode())
        assert text == v["canonical"]
        assert crypto.b64url(key.sign(text.encode())) == v["signature"]

    def test_charge_utile_du_cloud_verifiee(self, cloud_keys):
        signed = cloud_keys.sign({"type": "decision"})
        assert crypto.verify_signed_payload(cloud_keys.public, signed["payload"], signed["signature"]) == {
            "type": "decision"
        }
        with pytest.raises(ValueError):
            crypto.verify_signed_payload(
                cloud_keys.public,
                signed["payload"].replace("decision", "model"),
                signed["signature"],
            )

    def test_contribution_chiffree_pour_le_cloud(self, cloud_keys):
        env = crypto.encrypt_for_cloud(cloud_keys.rsa_public, b'{"delta":[1]}')
        key = cloud_keys.rsa.decrypt(
            base64.b64decode(env["encrypted_key"]),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        assert (
            AESGCM(key).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ciphertext"]), None)
            == b'{"delta":[1]}'
        )
        assert env["alg"] == "RSA-OAEP-256+A256GCM"

    def test_cle_logicielle_0600_et_refus_si_lisible(self, tmp_path):
        path = tmp_path / "keys" / "agent.pem"
        ks = crypto.SoftwareKeyStore(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert crypto.SoftwareKeyStore(path).public_key() == ks.public_key(), "clé stable au redémarrage"
        os.chmod(path, 0o644)
        with pytest.raises(PermissionError):
            crypto.SoftwareKeyStore(path)

    def test_instant_java_a_neuf_decimales(self):
        assert parse_instant("2026-09-14T08:00:00.123456789Z") == dt.datetime(
            2026, 9, 14, 8, 0, 0, 123456, tzinfo=dt.timezone.utc
        )


def _certificat(tmp_path: Path) -> tuple[Path, Path]:
    """Certificat autosigné pour 127.0.0.1 (serveur TLS de test)."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.x509.oid import NameOID

    key = _ec.generate_private_key(_ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(crypto.serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            crypto.serialization.Encoding.PEM,
            crypto.serialization.PrivateFormat.PKCS8,
            crypto.serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _serveur_https(tmp_path: Path, tls_max):
    """Serveur HTTPS local qui répond {"ok": true}, limité à la version TLS maximale donnée."""
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cert, key = _certificat(tmp_path)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.maximum_version = tls_max
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, cert


class TestTls:
    """C-2, E186 (CDC §3.3 « TLS 1.3 ») : l'agent n'accepte que TLS 1.3, vers le cloud comme vers un broker MQTT en TLS."""

    def test_cloud_refuse_un_serveur_tls_1_2_et_accepte_tls_1_3(self, tmp_path):
        import ssl

        from h2s_agent.clock import Clock
        from h2s_agent.cloud import CloudClient, CloudUnavailable

        (tmp_path / "a").mkdir()
        old, cert = _serveur_https(tmp_path / "a", ssl.TLSVersion.TLSv1_2)
        try:
            client = CloudClient(
                f"https://127.0.0.1:{old.server_address[1]}",
                None,
                Clock(),
                timeout_s=5,
                ca_file=str(cert),
            )
            with pytest.raises(CloudUnavailable):
                client._call("GET", "/api/agent/health", signed=False)
        finally:
            old.shutdown()
        (tmp_path / "b").mkdir()
        new, cert = _serveur_https(tmp_path / "b", ssl.TLSVersion.TLSv1_3)
        try:
            client = CloudClient(
                f"https://127.0.0.1:{new.server_address[1]}",
                None,
                Clock(),
                timeout_s=5,
                ca_file=str(cert),
            )
            assert client._call("GET", "/api/agent/health", signed=False).body == {"ok": True}
        finally:
            new.shutdown()

    def test_contexte_mqtt_tls_1_3_minimum(self, tmp_path):
        import socket
        import ssl
        import threading

        from h2s_agent import tls

        ctx = tls.client_context()
        assert (
            ctx.minimum_version == ssl.TLSVersion.TLSv1_3
            and ctx.verify_mode == ssl.CERT_REQUIRED
            and ctx.check_hostname
        )
        cert, key = _certificat(tmp_path)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        server_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        listener = socket.create_server(("127.0.0.1", 0))

        def accept():
            conn, _ = listener.accept()
            try:
                server_ctx.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                pass

        threading.Thread(target=accept, daemon=True).start()
        with (
            socket.create_connection(listener.getsockname(), timeout=5) as raw,
            pytest.raises(ssl.SSLError),
        ):
            tls.client_context(str(cert)).wrap_socket(raw, server_hostname="127.0.0.1")
        listener.close()


class TestFederated:
    def test_ridge_retrouve_une_relation_connue_et_reste_proche_du_global(self):
        truth = [0.2, 0.1, -0.05, 0.0, 0.0, -0.1, 0.6, 0.3]
        samples = []
        for h in range(24 * 7):
            at = T0 + dt.timedelta(hours=h)
            x = federated.features(at, 1 + (h % 5) * 0.3, 2 - (h % 7) * 0.2)
            samples.append((x, sum(a * b for a, b in zip(truth, x))))
        w, mse = federated.train(samples, [0.0] * 8, lam=1e-6)
        assert mse < 1e-6
        assert all(abs(a - b) < 1e-3 for a, b in zip(w, truth))
        w_strong, _ = federated.train(samples, [0.0] * 8, lam=1e6)
        assert max(abs(v) for v in w_strong) < 0.05, "rappel fort vers le modèle global"

    def test_systeme_singulier_refuse(self):
        with pytest.raises(ValueError):
            federated.solve([[1, 2], [2, 4]], [1, 2])


class TestClock:
    def test_decalage_remis_a_zero_apres_correction_ntp(self):
        from h2s_agent.clock import Clock, utcnow

        c = Clock()
        now = utcnow()
        c.observe_server_time(now + dt.timedelta(minutes=4), now, now)
        assert abs((c.now() - utcnow()).total_seconds() - 240) < 2
        now = utcnow()
        c.observe_server_time(now, now, now)
        assert abs((c.now() - utcnow()).total_seconds()) < 2, "heure système juste : plus de décalage périmé"

    def test_horodatage_en_avance_date_a_la_reception(self):
        readings = parsers.h2s_battery(
            1,
            json.dumps({"soc": 50, "timestamp": (T0 + dt.timedelta(hours=23)).isoformat()}).encode(),
            T0,
        )
        assert readings[0].at == T0 and readings[0].quality == "ESTIMATED"


class TestIntegrity:
    def test_manifeste_signe_puis_fichier_modifie(self, tmp_path, cloud_keys):
        (tmp_path / "a.py").write_text("print(1)")
        payload = integrity.manifest_payload(tmp_path, "1.0.0")
        (tmp_path / "MANIFEST.json").write_text(json.dumps(cloud_keys.sign(payload)))
        assert integrity.verify(cloud_keys.public, tmp_path).ok
        (tmp_path / "a.py").write_text("print(2)")
        verdict = integrity.verify(cloud_keys.public, tmp_path)
        assert not verdict.ok and "a.py" in verdict.reason
        assert not integrity.verify(cloud_keys.public, tmp_path / "vide").ok

    def test_dependances_et_politique_de_configuration_couvertes(self, tmp_path, cloud_keys):
        """E176 : un fichier de site-packages modifié ou ajouté (.pth), un champ de sécurité contraire au manifeste : refusés."""
        pkg, site = tmp_path / "pkg", tmp_path / "site"
        (site / "lib-1.0.dist-info").mkdir(parents=True)
        pkg.mkdir()
        (pkg / "a.py").write_text("x = 1")
        (site / "lib.py").write_text("y = 1")
        (site / "lib-1.0.dist-info" / "RECORD").write_text("propre à l'installation")
        policy = {
            "cloud_url": "https://api.h2s.example",
            "key_store": "tpm2",
            "require_tpm": True,
        }
        (pkg / "MANIFEST.json").write_text(
            json.dumps(cloud_keys.sign(integrity.manifest_payload(pkg, "1.0.0", [site], policy)))
        )
        good = integrity.config_fields(
            {
                "cloud_url": "https://api.h2s.example",
                "key_store": "tpm2",
                "require_tpm": True,
            }
        )
        assert integrity.verify(cloud_keys.public, pkg, [site], good, good).ok
        (site / "lib-1.0.dist-info" / "RECORD").write_text("réinstallé")
        assert integrity.verify(cloud_keys.public, pkg, [site], good, good).ok, (
            "métadonnées d'installation de pip non couvertes"
        )
        (site / "evil.pth").write_text("import os")
        verdict = integrity.verify(cloud_keys.public, pkg, [site], good, good)
        assert not verdict.ok and "evil.pth" in verdict.reason, "fichier .pth ajouté aux dépendances"
        (site / "evil.pth").unlink()
        (site / "lib.py").write_text("y = 2")
        assert not integrity.verify(cloud_keys.public, pkg, [site], good, good).ok, "dépendance modifiée"
        (site / "lib.py").write_text("y = 1")
        soft = integrity.config_fields({"cloud_url": "https://api.h2s.example", "key_store": "software"})
        verdict = integrity.verify(cloud_keys.public, pkg, [site], soft, soft)
        assert not verdict.ok and "key_store" in verdict.reason, "agent.json contraire à la politique signée"
        moved = dict(good, cloud_url="https://pirate.example")
        verdict = integrity.verify(cloud_keys.public, pkg, [site], moved, good)
        assert not verdict.ok and "cloud_url" in verdict.reason, "agent.json modifié depuis le démarrage"
        (pkg / "MANIFEST.json").write_text(json.dumps(cloud_keys.sign(integrity.manifest_payload(pkg, "1.0.0"))))
        assert not integrity.verify(cloud_keys.public, pkg, [site]).ok, (
            "manifeste sans dépendances refusé quand l'environnement est vérifié"
        )

    def test_outil_de_signature_verifie_par_la_cle_du_cloud(self, tmp_path):
        """tools/sign_manifest.py signe avec la clé de publication ; l'agent vérifie avec package_public_key, jamais avec la clé des ordres."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import sign_manifest
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        seed = os.urandom(32)
        public = crypto.b64url(
            ed25519.Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        (tmp_path / "b.py").write_text("x = 1")
        (tmp_path / "MANIFEST.json").write_text(
            json.dumps(sign_manifest.sign(base64.b64encode(seed).decode(), tmp_path, "1.0.0"))
        )
        assert integrity.verify(public, tmp_path).ok
        assert sign_manifest.public_key(base64.b64encode(seed).decode()) == public
        assert not integrity.verify(None, tmp_path).ok, "sans clé de publication configurée, rien n'est exécuté"
        with pytest.raises(ValueError):
            sign_manifest.sign(base64.b64encode(b"court").decode(), tmp_path)
