"""Canal temps réel agent ↔ cloud (lot 5 ; E173, E027, E039).

Deux familles :

* **sur un courtier de test** (``amqtt``, en mémoire) : l'agent publie vraiment, s'abonne vraiment et reçoit vraiment ses
  ordres — signature comprise ;
* **sur la base locale** : relevés bruts publiés au fil de l'eau, repli sur le sondage HTTP, absence de double exécution.

Les règles d'accès du courtier (un agent ne lit ni n'écrit les sujets d'un autre) se prouvent sur un EMQX réel :
``specs/controle/scripts/perf/mqtt_acl.py`` (11 cas) ; ``amqtt`` n'applique pas les règles d'EMQX.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import pathlib
import socket
import threading
import time
import uuid

import paho.mqtt.client as paho
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from h2s_agent import crypto
from h2s_agent.cloud_mqtt import CloudMqtt, FallbackCommandChannel, RawForwarder
from h2s_agent.config import CloudMqttConf
from h2s_agent.crypto import SoftwareKeyStore

from .conftest import SITE, T0

amqtt_broker = pytest.importorskip("amqtt.broker", reason="courtier de test amqtt absent")


def port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def courtier():
    """Courtier MQTT en mémoire : un vrai échange réseau sur la boucle locale, sans conteneur."""
    port = port_libre()
    pret = threading.Event()
    boucle: list[asyncio.AbstractEventLoop] = []
    instance: list = []

    def servir() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        boucle.append(loop)

        async def demarrer() -> None:
            broker = amqtt_broker.Broker(
                {
                    "listeners": {
                        "default": {
                            "type": "tcp",
                            "bind": f"127.0.0.1:{port}",
                            "max_connections": 20,
                        }
                    },
                    "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
                }
            )
            await broker.start()
            instance.append(broker)
            pret.set()

        loop.run_until_complete(demarrer())
        loop.run_forever()

    fil = threading.Thread(target=servir, daemon=True)
    fil.start()
    assert pret.wait(20), "courtier de test non démarré"
    yield port
    # Arrêt propre du courtier avant celui de sa boucle : sinon ses tâches de diffusion meurent sans boucle (avertissements).
    if instance and boucle:
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(instance[0].shutdown(), boucle[0]).result(10)
    if boucle:
        boucle[0].call_soon_threadsafe(boucle[0].stop)


@pytest.fixture
def agent_keys(tmp_path):
    return SoftwareKeyStore(tmp_path / "agent.pem")


def cloud_mqtt(port: int, keys, agent_uuid: str, clock, **kw) -> CloudMqtt:
    conf = CloudMqttConf(host="127.0.0.1", port=port, tls=False, publish_timeout_s=5.0)
    return CloudMqtt(conf, keys, agent_uuid, clock, **kw)


class Ecoute:
    """Le « cloud » du banc : abonné à tous les sujets des agents, il garde ce qu'il reçoit."""

    def __init__(self, port: int) -> None:
        self.messages: list[tuple[str, dict]] = []
        self._pret = threading.Event()
        self._client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id="essai-cloud",
            clean_session=True,
        )
        self._client.on_connect = lambda *_: self._pret.set()
        self._client.on_message = lambda c, u, m: self.messages.append((m.topic, json.loads(m.payload)))
        self._client.connect("127.0.0.1", port, 30)
        self._client.loop_start()
        assert self._pret.wait(10)
        self._client.subscribe("h2s/agents/+/telemetrie", qos=1)
        self._client.subscribe("h2s/agents/+/comptes-rendus", qos=1)
        self._client.subscribe("h2s/agents/+/etat", qos=1)
        time.sleep(0.4)

    def publier_ordre(self, agent_uuid: str, signe: dict, kind: str = "decision") -> None:
        charge = {
            "v": 1,
            "type": kind,
            "payload": signe["payload"],
            "signature": signe["signature"],
            "key_id": signe["key_id"],
        }
        self._client.publish(f"h2s/agents/{agent_uuid}/ordres", json.dumps(charge), qos=1).wait_for_publish(timeout=5)

    def attendre(self, n: int = 1, delai: float = 5.0) -> list[tuple[str, dict]]:
        limite = time.monotonic() + delai
        while len(self.messages) < n and time.monotonic() < limite:
            time.sleep(0.05)
        return self.messages

    def fermer(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


@contextlib.contextmanager
def canal(port: int, keys, agent_uuid: str, clock, **kw):
    ecoute = Ecoute(port)
    mqtt = cloud_mqtt(port, keys, agent_uuid, clock, **kw)
    mqtt.start()
    assert mqtt.wait(10), "canal non connecté au courtier de test"
    time.sleep(0.3)
    try:
        yield mqtt, ecoute
    finally:
        mqtt.stop()
        ecoute.fermer()


class TestMessagesSignes:
    """Tout ce que l'agent publie porte la signature de ses requêtes (schéma H2S-AGENT-V1, méthode MQTT, sujet en chemin)."""

    def test_instantane_publie_signe_et_horodate(self, courtier, agent_keys, clock):
        agent_uuid = str(uuid.uuid4())
        with canal(courtier, agent_keys, agent_uuid, clock) as (mqtt, ecoute):
            assert mqtt.publish_live({"v": 1, "type": "live", "site_id": SITE, "production_w": 120.0})
            recus = ecoute.attendre(1)
        assert len(recus) == 1
        sujet, enveloppe = recus[0]
        assert sujet == f"h2s/agents/{agent_uuid}/telemetrie"
        assert enveloppe["agent"] == agent_uuid and enveloppe["v"] == 1
        assert enveloppe["ts"] == int(T0.timestamp())
        # Signature vérifiable sur la chaîne canonique exacte : sujet, horodatage, nonce, empreinte de la charge utile.
        attendu = crypto.canonical(
            "MQTT",
            sujet,
            enveloppe["ts"],
            enveloppe["nonce"],
            enveloppe["payload"].encode(),
        )
        assert crypto.verify_agent_signature("Ed25519", agent_keys.public_key(), attendu.encode(), enveloppe["sig"])
        assert json.loads(enveloppe["payload"])["type"] == "live"

    def test_une_signature_ne_vaut_pas_pour_un_autre_sujet(self, courtier, agent_keys, clock):
        """La chaîne signée contient le sujet : un message rejoué sur un autre sujet ne se vérifie plus."""
        agent_uuid = str(uuid.uuid4())
        with canal(courtier, agent_keys, agent_uuid, clock) as (mqtt, ecoute):
            mqtt.publish_live({"v": 1, "type": "live", "site_id": SITE})
            enveloppe = ecoute.attendre(1)[0][1]
        autre = f"h2s/agents/{agent_uuid}/comptes-rendus"
        chaine = crypto.canonical(
            "MQTT",
            autre,
            enveloppe["ts"],
            enveloppe["nonce"],
            enveloppe["payload"].encode(),
        )
        assert not crypto.verify_agent_signature("Ed25519", agent_keys.public_key(), chaine.encode(), enveloppe["sig"])

    def test_comptes_rendus_et_etat_publies(self, courtier, agent_keys, clock):
        agent_uuid = str(uuid.uuid4())
        with canal(courtier, agent_keys, agent_uuid, clock) as (mqtt, ecoute):
            assert mqtt.publish_records(
                [
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "DECISION_REPORT",
                        "at": "2026-09-14T08:00:00Z",
                        "payload": {},
                    }
                ]
            )
            assert mqtt.publish_state("RUNNING", "1.0.0", 0, True, False)
            recus = ecoute.attendre(2)
        feuilles = {s.rsplit("/", 1)[1] for s, _ in recus}
        assert feuilles == {"comptes-rendus", "etat"}

    def test_canal_coupe_ne_publie_rien(self, agent_keys, clock):
        """Courtier injoignable : la publication rend False, jamais une exception ni un message perdu en silence."""
        mqtt = cloud_mqtt(port_libre(), agent_keys, str(uuid.uuid4()), clock)
        assert not mqtt.connected
        assert mqtt.publish_live({"v": 1, "type": "live", "site_id": SITE}) is False
        assert (
            mqtt.publish_records(
                [
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "ALERT",
                        "at": "2026-09-14T08:00:00Z",
                        "payload": {},
                    }
                ]
            )
            is False
        )


class TestVecteurCommun:
    """Le cloud (Java) et l'agent (Python) composent la même chaîne signée : vecteur partagé ``tests/vectors.json``."""

    def test_chaine_canonique_du_message_mqtt(self):
        vecteurs = json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text(encoding="utf-8"))
        v = vecteurs["mqtt_message"]
        chaine = crypto.canonical("MQTT", v["topic"], v["timestamp"], v["nonce"], v["payload"].encode("utf-8"))
        assert chaine == v["canonical"]
        cle = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(vecteurs["agent_seed_hex"]))
        assert crypto.b64url(cle.sign(chaine.encode("utf-8"))) == v["signature"]
        assert chaine.splitlines()[:3] == [crypto.SCHEME, "MQTT", v["topic"]]


class TestOrdresPousses:
    """CA-18.10 : un ordre émis par le cloud arrive à l'agent sans attendre le sondage de 60 secondes."""

    def test_ordre_pousse_recu_et_verifie(self, courtier, agent_keys, clock, cloud_keys):
        agent_uuid = str(uuid.uuid4())
        recus: list[list[dict]] = []
        with canal(courtier, agent_keys, agent_uuid, clock, on_decisions=recus.append) as (_mqtt, ecoute):
            ordre = cloud_keys.order(T0)
            ecoute.publier_ordre(agent_uuid, ordre)
            limite = time.monotonic() + 5
            while not recus and time.monotonic() < limite:
                time.sleep(0.05)
        assert recus, "ordre poussé jamais remis à l'agent"
        assert recus[0][0]["payload"] == ordre["payload"] and recus[0][0]["signature"] == ordre["signature"]

    def test_commande_poussee_mise_en_file(self, courtier, agent_keys, clock, cloud_keys):
        agent_uuid = str(uuid.uuid4())
        commande = cloud_keys.sign(
            {
                "v": 1,
                "type": "device_command",
                "command_id": str(uuid.uuid4()),
                "site_id": SITE,
                "command": "PING",
            }
        )
        with canal(courtier, agent_keys, agent_uuid, clock) as (mqtt, ecoute):
            ecoute.publier_ordre(agent_uuid, commande, kind="command")
            limite = time.monotonic() + 5
            while not mqtt._commands and time.monotonic() < limite:
                time.sleep(0.05)
            prises = mqtt.take_commands()
        assert len(prises) == 1 and prises[0]["payload"] == commande["payload"]
        assert mqtt.take_commands() == [], "une commande prise ne doit pas être rendue deux fois"

    def test_message_mal_forme_ecarte(self, courtier, agent_keys, clock):
        agent_uuid = str(uuid.uuid4())
        recus: list[list[dict]] = []
        with canal(courtier, agent_keys, agent_uuid, clock, on_decisions=recus.append) as (mqtt, ecoute):
            ecoute._client.publish(f"h2s/agents/{agent_uuid}/ordres", b"pas du json", qos=1).wait_for_publish(timeout=5)
            time.sleep(0.5)
        assert recus == [] and mqtt.refused >= 1


class Canal:
    """Canal double : connecté ou non à la demande, sans courtier (repli et relevés bruts)."""

    def __init__(self, connected: bool = True, commandes: list[dict] | None = None) -> None:
        self.connected = connected
        self.live: list[dict] = []
        self.raw: list[dict] = []
        self.records: list[list[dict]] = []
        self.results: list[tuple[str, dict]] = []
        self.commandes = commandes or []

    def take_commands(self) -> list[dict]:
        out, self.commandes = self.commandes, []
        return out

    def publish_live(self, payload: dict) -> bool:
        if not self.connected:
            return False
        self.live.append(payload)
        return True

    def publish_raw(self, site_id: int, readings: list[dict], now) -> bool:
        if not self.connected:
            return False
        self.raw.append({"site_id": site_id, "readings": readings})
        return True

    def publish_records(self, records: list[dict]) -> bool:
        if not self.connected:
            return False
        self.records.append(records)
        return True

    def publish_command_result(self, command_id: str, body: dict) -> bool:
        if not self.connected:
            return False
        self.results.append((command_id, body))
        return True


class CanalHttp:
    """Sondage signé du banc : ce que ``HttpCommandChannel`` rend."""

    def __init__(self, commandes: list[dict] | None = None) -> None:
        self.commandes = commandes or []
        self.rendus: list[tuple[str, dict]] = []
        self.appels = 0

    def fetch(self) -> list[dict]:
        self.appels += 1
        out, self.commandes = self.commandes, []
        return out

    def report(self, command_id: str, body: dict) -> None:
        self.rendus.append((command_id, body))


class TestRepli:
    """Canal poussé d'abord, sondage en filet de sécurité — y compris quand le canal poussé tient."""

    def test_sondage_maintenu_meme_quand_le_canal_tient(self):
        """Le sondage est espacé par la cadence de la tâche appelante, jamais supprimé.

        Le supprimer laissait les commandes mourir en silence : toute la chaîne de publication est en échec ouvert,
        et cette voie ne marque jamais la commande « remise ». Un boîtier connecté au courtier mais que la
        publication n'atteint pas ne recevait plus rien, sans trace nulle part. Constaté en clientèle.
        """
        canal_mqtt, http = Canal(), CanalHttp([{"payload": "http"}])
        channel = FallbackCommandChannel(canal_mqtt, http, lambda: canal_mqtt.connected)
        assert channel.fetch() == [{"payload": "http"}], "le sondage reste le filet de sécurité"
        assert http.appels == 1

    def test_les_deux_chemins_se_completent_sans_doublon_a_l_execution(self):
        canal_mqtt, http = Canal(connected=False), CanalHttp([{"payload": "http"}])
        channel = FallbackCommandChannel(canal_mqtt, http, lambda: canal_mqtt.connected)
        assert channel.fetch() == [{"payload": "http"}], "canal coupé : seul le sondage rapporte"
        canal_mqtt.connected = True
        http.commandes = [{"payload": "http2"}]
        # Rétabli, le canal poussé prend la tête, mais le sondage continue de rapporter ce qu'il voit : la même
        # commande arrivant par les deux chemins n'est exécutée qu'une fois (DeviceCommands._verify, jeu des
        # identifiants déjà vus sous verrou).
        assert channel.fetch() == [{"payload": "http2"}]
        assert http.appels == 2

    def test_resultat_rendu_par_le_sondage_si_le_canal_tombe_entre_temps(self):
        canal_mqtt, http = Canal(), CanalHttp()
        channel = FallbackCommandChannel(canal_mqtt, http, lambda: True)
        canal_mqtt.connected = False  # courtier tombé après réception de la commande
        channel.report("c1", {"status": "SUCCEEDED"})
        assert http.rendus == [("c1", {"status": "SUCCEEDED"})]
        assert canal_mqtt.results == []


class TestRelevesBruts:
    """E039 : relevés publiés bruts, au fil de l'eau, sans jamais republier deux fois les mêmes."""

    def _mesures(self, db, n: int, base: dt.datetime) -> None:
        with db.tx() as cur:
            for i in range(n):
                cur.execute(
                    "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit, measurement_timestamp,"
                    " received_timestamp) VALUES (%s, %s, 'shelly', 'power_w', %s, 'W', %s, %s)",
                    (
                        str(SITE),
                        "device:1",
                        100.0 + i,
                        base + dt.timedelta(seconds=i),
                        base + dt.timedelta(seconds=i),
                    ),
                )

    def test_releves_publies_puis_curseur_avance(self, db, clock):
        self._mesures(db, 3, T0 - dt.timedelta(seconds=30))
        canal_mqtt = Canal()
        forwarder = RawForwarder(db, lambda: SITE, clock.now, canal_mqtt)
        assert forwarder.tick() == 3
        assert canal_mqtt.raw[0]["site_id"] == SITE
        assert [r["value"] for r in canal_mqtt.raw[0]["readings"]] == [
            100.0,
            101.0,
            102.0,
        ]
        assert forwarder.tick() == 0, "un relevé déjà publié ne doit jamais repartir"

    def test_canal_coupe_rien_publie_et_curseur_immobile(self, db, clock):
        self._mesures(db, 2, T0 - dt.timedelta(seconds=20))
        canal_mqtt = Canal(connected=False)
        forwarder = RawForwarder(db, lambda: SITE, clock.now, canal_mqtt)
        assert forwarder.tick() == 0
        assert forwarder.cursor() == 0, "le curseur ne doit pas sauter des relevés jamais publiés"
        canal_mqtt.connected = True
        assert forwarder.tick() == 2, "les relevés repartent au rétablissement du canal"

    def test_releve_trop_ancien_jamais_publie(self, db, clock):
        self._mesures(db, 2, T0 - dt.timedelta(hours=6))
        canal_mqtt = Canal()
        forwarder = RawForwarder(db, lambda: SITE, clock.now, canal_mqtt)
        assert forwarder.tick() == 0
        assert canal_mqtt.raw == [], "le brut n'est pas un rattrapage d'historique (le buffer s'en charge)"
        assert forwarder.cursor() > 0, "le curseur passe outre, sinon il resterait bloqué"
