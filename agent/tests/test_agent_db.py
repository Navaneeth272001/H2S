"""Agent sur PostgreSQL réel : schéma et migrations, buffer, décisions et mode dégradé, collecte, orchestration, API locale."""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from h2s_agent import state
from h2s_agent.buffer import Buffer
from h2s_agent.collector import Collector
from h2s_agent.config import AgentConfig, BatteryConf, DeviceConf, SourceConf
from h2s_agent.decisions import DecisionManager
from h2s_agent.federated import FEATURES, Federated
from h2s_agent.live import LivePublisher
from h2s_agent.local_api import LocalApi
from h2s_agent.orchestrator import Agent, save_identity
from h2s_agent.storage import MIGRATIONS, Database, migrate

from .conftest import DB_URL, SITE, T0

REFERENCE = Path(__file__).resolve().parents[3] / "db" / "edge" / "agent_local_postgres.sql"


def conf(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        cloud_url="http://localhost:8088",
        db_url=DB_URL,
        data_dir=tmp_path,
        devices=(DeviceConf(3, "shelly_gen2", mqtt_id="shellyplug-3"),),
        batteries=(BatteryConf(1, "site4/battery/status", "site4/commands"),),
        weather_topic="site4/weather/status",
    )


def soc(db: Database, value: float, at: dt.datetime, stock: int = 1) -> None:
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit, measurement_timestamp)"
            " VALUES ('4', %s, 'battery', 'soc_pct', %s, '%%', %s)",
            (f"stock:{stock}", value, at),
        )


class TestStorage:
    def test_schema_client_copie_fidele_et_migrations_rejouables(self, db):
        assert (MIGRATIONS / "E001__schema_client.sql").read_text() == REFERENCE.read_text(), (
            "E001 = db/edge/agent_local_postgres.sql"
        )
        assert migrate(db) == [], "rejouable sans effet"
        with db.tx() as cur:
            cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'")
            assert cur.fetchone()["n"] >= 16, (
                "13 tables du client, decision_cache, identité, historique des migrations (CA-18.7)"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO local_buffer (site_id, entity_type, payload, priority) VALUES ('4', 'SECRET', '{}', 2)"
                )

    def test_migration_appliquee_modifiee_refusee(self, db, tmp_path, monkeypatch):
        with db.tx() as cur:
            cur.execute("UPDATE agent_schema_history SET checksum = repeat('0', 64) WHERE version = 'E002'")
        with pytest.raises(RuntimeError):
            migrate(db)

    def test_requete_sur_sept_jours_en_moins_de_500_ms(self, db):
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit, measurement_timestamp)"
                " SELECT '4', 'device:' || d, 'device', 'power_w', random() * 1000, 'W', %s - (m * interval '1 minute')"
                " FROM generate_series(1, 3) d, generate_series(0, 10079) m",
                (T0,),
            )
        started = time.monotonic()
        with db.tx() as cur:
            cur.execute(
                "SELECT device_id, date_trunc('hour', measurement_timestamp) h, avg(value) FROM local_measurements"
                " WHERE measurement_timestamp >= %s GROUP BY 1, 2",
                (T0 - dt.timedelta(days=7),),
            )
            rows = cur.fetchall()
        assert len(rows) >= 3 * 168 and time.monotonic() - started < 0.5, "CA-18.8 : 30 240 mesures sur 7 jours"


class TestBuffer:
    def test_priorite_puis_ordre_chronologique_et_reprise_espacee(self, db, clock):
        b = Buffer(db, "4")
        b.push("METRIC", {"n": 1}, T0, T0)
        b.push("MEASUREMENT", {"n": 2}, T0, T0, priority=3)
        b.push("MEASUREMENT", {"n": 3}, T0, T0 + dt.timedelta(seconds=1), priority=2)
        b.push("DECISION_REPORT", {"n": 4}, T0, T0 + dt.timedelta(seconds=2))
        b.push("MEASUREMENT", {"n": 5}, T0, T0 + dt.timedelta(seconds=3), priority=2)
        due = b.due(T0 + dt.timedelta(seconds=10))
        assert [i.payload["n"] for i in due] == [4, 3, 5, 2, 1], (
            "critiques d'abord (CA-18.23), FIFO dans une priorité (CA-18.24)"
        )
        ids = [due[0].buffer_id]
        waits = []
        now = T0 + dt.timedelta(seconds=10)
        for _ in range(7):
            b.failed(ids, "réseau", now)
            with db.tx() as cur:
                cur.execute(
                    "SELECT next_attempt_at FROM local_buffer WHERE buffer_id = %s",
                    (ids[0],),
                )
                nxt = cur.fetchone()["next_attempt_at"]
            waits.append(int((nxt - now).total_seconds()))
            now = nxt
        assert waits == [5, 30, 120, 600, 1800, 1800, 1800], (
            "5 s, 30 s, 2 min, 10 min, 30 min puis 30 min sans abandon (CA-18.26)"
        )
        assert b.release(T0 + dt.timedelta(seconds=11)) == 1, "retour du réseau : reprise immédiate"
        b.acknowledged([i.buffer_id for i in due])
        assert b.size() == 0

    def test_eviction_basse_priorite_retention_trente_jours_sauf_contributions(self, db):
        b = Buffer(db, "4", max_items=3)
        old = T0 - dt.timedelta(days=31)
        b.push("MEASUREMENT", {"n": "vieux"}, old, old, priority=3)
        b.push("GRADIENT", {"n": "contribution"}, old, old)
        for n, p in (("m", 3), ("metric", 4), ("critique", 2), ("rapport", 1)):
            b.push("MEASUREMENT" if p < 4 else "METRIC", {"n": n}, T0, T0, priority=p)
        assert b.evict(T0) == 3
        kept = {i.payload["n"] for i in b.due(T0 + dt.timedelta(days=1))}
        assert kept == {"contribution", "critique", "rapport"}, (
            "vieux supprimé (CA-18.25) ; plafond : métrique puis mesure standard"
        )


class TestDecisions:
    def manager(self, db, publisher, identity, clock, alerts=None):
        return DecisionManager(
            db,
            Buffer(db, "4"),
            publisher,
            lambda: identity,
            lambda stock: "site4/commands" if stock == 1 else None,
            clock.now,
            (lambda code, sev, msg: alerts.append(code)) if alerts is not None else None,
        )

    def test_ordre_signe_mis_en_cache_revalide_execute_et_rendu(self, db, publisher, identity, clock, cloud_keys):
        m = self.manager(db, publisher, identity, clock)
        soc(db, 50, T0 - dt.timedelta(minutes=1))
        order = cloud_keys.order(T0, "CHARGE", 800)
        fresh = m.receive([order])
        assert len(fresh) == 1 and m.last_receive_ms < 200, "CA-18.10, CA-18.13"
        assert m.receive([order]) == [], "une fois en cache"
        assert m.tick([1], online=True) == ["EXECUTED"]
        assert m.last_execute_ms < 200, "CA-18.12"
        topic, cmd = publisher.sent[-1]
        assert topic == "site4/commands/charge" and cmd["power_w"] == 500 and cmd["source"] == "CLOUD_LIVE", (
            "SEC4 appliquée localement (CA-18.11)"
        )
        assert m.tick([1], online=True) == [], "un ordre ne s'exécute qu'une fois"
        items = Buffer(db, "4").due(T0 + dt.timedelta(minutes=1))
        report = next(i.payload for i in items if i.kind == "DECISION_REPORT")
        assert report["execution_status"] == "EXECUTED" and report["decision_id"] == fresh[0].decision_id

    def test_ordres_faux_ecartes(self, db, publisher, identity, clock, cloud_keys):
        alerts: list[str] = []
        m = self.manager(db, publisher, identity, clock, alerts)
        tampered = cloud_keys.order(T0)
        tampered["payload"] = tampered["payload"].replace('"CHARGE"', '"DISCHARGE"')
        other_site = cloud_keys.order(T0, site=SITE + 1)
        too_long = cloud_keys.order(T0, validity=dt.timedelta(days=8))
        assert m.receive([tampered, other_site, too_long, {"payload": 3}]) == []
        assert alerts.count("DECISION_SIGNATURE_INVALID") == 4

    def test_rejet_de_securite_puis_standby_sur(self, db, publisher, identity, clock, cloud_keys):
        alerts: list[str] = []
        m = self.manager(db, publisher, identity, clock, alerts)
        soc(db, 97, T0 - dt.timedelta(minutes=1))
        m.receive([cloud_keys.order(T0, "CHARGE", 300)])
        assert m.tick([1], online=True) == ["REJECTED"]
        assert publisher.sent[-1][0] == "site4/commands/standby"
        with db.tx() as cur:
            cur.execute("SELECT source, execution_status, rejection_rule FROM local_decisions ORDER BY decision_id")
            assert [(r["source"], r["execution_status"], r["rejection_rule"]) for r in cur.fetchall()] == [
                ("CLOUD_LIVE", "REJECTED", "SEC3"),
                ("SAFE_STANDBY", "EXECUTED", None),
            ]
        assert "DECISION_REJECTED_LOCALLY" in alerts and "SAFE_STANDBY" in alerts

    def test_mode_degrade_ordre_a_son_terme_jamais_rejoue_puis_standby_sans_decision_locale(
        self, db, publisher, identity, clock, cloud_keys
    ):
        """E182, D314 (amende D258) : hors réseau, l'ordre du cloud va au bout de sa durée puis n'est jamais rejoué ; sans seuils du
        site pour décider localement (aucune règle reçue), la batterie passe en veille sûre."""
        m = self.manager(db, publisher, identity, clock)
        soc(db, 40, T0)
        m.receive([cloud_keys.order(T0, "DISCHARGE", 200, duration=600, validity=dt.timedelta(hours=2))])
        assert m.tick([1], online=True) == ["EXECUTED"]
        clock.advance(minutes=5)
        soc(db, 38, clock.now())
        assert m.tick([1], online=False) == [], "hors réseau, l'action en cours continue"
        clock.advance(minutes=6)
        soc(db, 35, clock.now())
        assert m.tick([1], online=False) == ["EXECUTED"]
        assert (
            publisher.sent[-1][0] == "site4/commands/standby" and publisher.sent[-1][1]["source"] == "SAFE_STANDBY"
        ), "ordre échu : état sûr, pas de rejeu"
        assert m.tick([1], online=False) == [], "une fois"
        with db.tx() as cur:
            cur.execute("SELECT count(*) AS n FROM local_decisions WHERE source = 'CLOUD_CACHE'")
            assert cur.fetchone()["n"] == 0
        clock.advance(days=8)
        assert m.purge_cache(clock.now()) == 1, "CA-18.15"

    def test_soc_inconnu_ou_publication_impossible(self, db, publisher, identity, clock, cloud_keys):
        m = self.manager(db, publisher, identity, clock)
        m.receive([cloud_keys.order(T0, "CHARGE", 100)])
        assert m.tick([1], online=True) == ["REJECTED"], "sans mesure de SoC récente, pas de charge"
        clock.advance(minutes=1)
        soc(db, 50, clock.now())
        publisher.down = True
        order = m.latest_valid(1, clock.now())
        assert m.execute(order, "CLOUD_LIVE") == "FAILED"

    def manager_synced(self, db, publisher, identity, clock, synced, alerts=None):
        return DecisionManager(
            db,
            Buffer(db, "4"),
            publisher,
            lambda: identity,
            lambda stock: "site4/commands" if stock == 1 else None,
            clock.now,
            (lambda code, sev, msg: alerts.append(code)) if alerts is not None else None,
            lambda: synced[0],
        )

    def test_chien_de_garde_seuil_franchi_pendant_l_ordre(self, db, publisher, identity, clock, cloud_keys):
        alerts: list[str] = []
        m = self.manager(db, publisher, identity, clock, alerts)
        soc(db, 21, T0)
        m.receive([cloud_keys.order(T0, "DISCHARGE", 300, duration=86_400)])
        assert m.tick([1], online=True) == ["EXECUTED"]
        clock.advance(seconds=10)
        soc(db, 16, clock.now())
        assert m.tick([1], online=True) == [], "au-dessus du plancher, l'ordre continue"
        clock.advance(seconds=10)
        soc(db, 15.0, clock.now())
        assert m.tick([1], online=True) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/standby", (
            "SEC2 atteinte pendant l'ordre (R5-07)"
        )
        assert "BATTERY_WATCHDOG" in alerts
        clock.advance(minutes=11)
        m.receive([cloud_keys.order(clock.now(), "CHARGE", 200, duration=3600)])
        soc(db, 50, clock.now() - dt.timedelta(minutes=1))
        assert m.tick([1], online=True) == ["EXECUTED"]
        clock.advance(minutes=11)
        assert m.tick([1], online=True) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/standby", (
            "SoC muet 10 minutes : veille"
        )

    def test_chien_de_garde_charge_arretee_a_95_exactement(self, db, publisher, identity, clock, cloud_keys):
        m = self.manager(db, publisher, identity, clock)
        soc(db, 90, T0)
        m.receive([cloud_keys.order(T0, "CHARGE", 300, duration=86_400)])
        assert m.tick([1], online=True) == ["EXECUTED"]
        clock.advance(seconds=10)
        soc(db, 95.0, clock.now())
        assert m.tick([1], online=True) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/standby", (
            "SEC3 atteinte : veille (R5-07)"
        )

    def test_cache_plafonne_aux_100_dernieres_decisions_sans_perdre_l_ordre_en_vigueur(
        self, db, publisher, identity, clock, cloud_keys
    ):
        """E184 (§2.1 « cache des 100 dernières décisions ») : 120 ordres récents → 100 gardés, dont l'ordre en vigueur de chaque batterie."""
        m = self.manager(db, publisher, identity, clock)
        long_order = cloud_keys.order(
            T0 - dt.timedelta(hours=3),
            "DISCHARGE",
            200,
            duration=86_400,
            stock=2,
            validity=dt.timedelta(days=2),
        )
        m.receive([long_order])
        for i in range(119):
            clock.set(T0 - dt.timedelta(minutes=119 - i))
            m.receive(
                [
                    cloud_keys.order(
                        clock.now(),
                        "CHARGE",
                        100 + i,
                        duration=60,
                        validity=dt.timedelta(hours=1),
                    )
                ]
            )
        clock.set(T0)
        assert m.purge_cache(T0) == 20
        with db.tx() as cur:
            cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE stock_id = 2) AS en_vigueur FROM decision_cache")
            r = cur.fetchone()
        assert (r["n"], r["en_vigueur"]) == (100, 1), (
            "l'ordre de la batterie 2, le plus ancien reçu, est encore en vigueur : gardé"
        )
        assert m.latest_valid(2, T0) is not None

    def test_commande_mqtt_signee_verifiable_par_la_passerelle(
        self, db, publisher, identity, clock, cloud_keys, tmp_path
    ):
        """E183 (§4.3.5) : la commande publiée porte une signature de la clé de l'agent sur sa charge canonique, vérifiable avec la clé
        publique retenue sur h2s/site<N>/agent/pubkey ; une commande altérée ne se vérifie plus."""
        from h2s_agent import crypto

        keys = crypto.SoftwareKeyStore(tmp_path / "keys" / "agent.pem")
        m = DecisionManager(
            db,
            Buffer(db, "4"),
            publisher,
            lambda: identity,
            lambda stock: "site4/commands" if stock == 1 else None,
            clock.now,
            signer=keys,
        )
        soc(db, 50, T0)
        m.receive([cloud_keys.order(T0, "CHARGE", 300)])
        assert m.tick([1], online=True) == ["EXECUTED"]
        _topic, cmd = publisher.sent[-1]
        assert cmd["alg"] == "Ed25519" and cmd["key_id"] == crypto.key_id(keys.public_key())
        text = crypto.command_canonical(cmd)
        assert crypto.verify_agent_signature(cmd["alg"], keys.public_key(), text.encode(), cmd["signature"])
        forged = dict(cmd, power_w=1000.0)
        assert not crypto.verify_agent_signature(
            cmd["alg"],
            keys.public_key(),
            crypto.command_canonical(forged).encode(),
            cmd["signature"],
        )
        m.publish_public_key("h2s/site4/agent/pubkey")
        announced = json.loads(publisher.retained["h2s/site4/agent/pubkey"])
        assert announced == {
            "algorithm": "Ed25519",
            "public_key": keys.public_key(),
            "key_id": cmd["key_id"],
            "scheme": crypto.COMMAND_SCHEME,
        }

    def test_soc_date_dans_le_futur_ignore(self, db, publisher, identity, clock):
        m = self.manager(db, publisher, identity, clock)
        soc(db, 50, T0 + dt.timedelta(hours=23))
        soc(db, 12, T0 - dt.timedelta(minutes=1))
        assert m.soc(1, T0) == 12, "un relevé en avance ne fige pas le SoC"

    def test_ordre_le_plus_recent_seul_en_vigueur(self, db, publisher, identity, clock, cloud_keys):
        m = self.manager(db, publisher, identity, clock)
        soc(db, 50, T0)
        m.receive([cloud_keys.order(T0, "DISCHARGE", 200, validity=dt.timedelta(hours=168))])
        m.receive(
            [
                cloud_keys.order(
                    T0 + dt.timedelta(minutes=1),
                    "STANDBY",
                    0,
                    duration=600,
                    validity=dt.timedelta(hours=1),
                )
            ]
        )
        clock.set(T0 + dt.timedelta(hours=2))
        assert m.latest_valid(1, clock.now()) is None, (
            "le STANDBY le plus récent est échu : l'ancienne décharge ne revient pas"
        )

    def test_cache_non_rejoue_sur_horloge_non_fiable(self, db, publisher, identity, clock, cloud_keys):
        synced = [True]
        m = self.manager_synced(db, publisher, identity, clock, synced)
        soc(db, 50, T0)
        m.receive([cloud_keys.order(T0, "DISCHARGE", 200, duration=600, validity=dt.timedelta(hours=2))])
        assert m.tick([1], online=True) == ["EXECUTED"]
        synced[0] = False
        clock.advance(minutes=1)
        soc(db, 49, clock.now())
        assert m.tick([1], online=False) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/standby", (
            "redémarré sans heure fiable : veille"
        )
        synced[0] = True
        clock.set(T0 - dt.timedelta(hours=1))
        assert not m.cache_trusted(clock.now()), "horloge antérieure au dernier ordre signé reçu"

    def test_rejet_sec6_retente_apres_l_intervalle(self, db, publisher, identity, clock, cloud_keys):
        m = self.manager(db, publisher, identity, clock)
        soc(db, 50, T0)
        m.receive([cloud_keys.order(T0, "CHARGE", 200, duration=60)])
        assert m.tick([1], online=True) == ["EXECUTED"]
        clock.advance(seconds=70)
        soc(db, 50, clock.now())
        assert m.tick([1], online=True) == ["EXECUTED"], "fin de l'ordre : veille"
        clock.advance(seconds=20)
        m.receive([cloud_keys.order(clock.now(), "DISCHARGE", 200)])
        assert m.tick([1], online=True) == ["REJECTED"], "SEC6 : 20 s après la veille"
        assert m.tick([1], online=True) == [], "pas de nouvel essai avant l'intervalle"
        clock.advance(minutes=6)
        soc(db, 50, clock.now())
        assert m.tick([1], online=True) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/discharge", (
            "ordre retenté, pas perdu"
        )

    def test_veille_en_echec_retentee_toutes_les_5_minutes(self, db, publisher, identity, clock, cloud_keys):
        m = self.manager(db, publisher, identity, clock)
        soc(db, 50, T0)
        m.receive([cloud_keys.order(T0, "CHARGE", 200, duration=60)])
        m.tick([1], online=True)
        publisher.down = True
        clock.advance(seconds=70)
        soc(db, 50, clock.now())
        assert m.tick([1], online=True) == ["FAILED"]
        for _ in range(5):
            clock.advance(seconds=10)
            m.tick([1], online=True)
        with db.tx() as cur:
            cur.execute("SELECT count(*) AS n FROM local_decisions WHERE source = 'SAFE_STANDBY'")
            assert cur.fetchone()["n"] == 1, "broker en panne : une tentative, pas une par passage"
        clock.advance(minutes=5)
        publisher.down = False
        assert m.tick([1], online=True) == ["EXECUTED"]

    def test_veille_non_publiee_sans_alerte_de_mise_en_veille(self, db, publisher, identity, clock, cloud_keys):
        """Contrôle du 14/09/2026 (D290) : une veille que le broker n'a pas prise n'est pas annoncée « batterie mise en STANDBY »."""
        alerts: list[str] = []
        m = self.manager_synced(db, publisher, identity, clock, [True], alerts)
        soc(db, 50, T0)
        m.receive([cloud_keys.order(T0, "CHARGE", 200, duration=60)])
        m.tick([1], online=True)
        publisher.down = True
        assert m.safe_standby(1, "essai") == "FAILED"
        assert "SAFE_STANDBY" not in alerts, alerts
        publisher.down = False
        clock.advance(minutes=6)
        assert m.safe_standby(1, "essai") == "EXECUTED"
        assert "SAFE_STANDBY" in alerts

    def test_mode_manuel_borne_chien_de_garde_actif(self, db, publisher, identity, clock, cloud_keys):
        alerts: list[str] = []
        m = self.manager(db, publisher, identity, clock, alerts)
        soc(db, 50, T0)
        assert m.manual(1, "CHARGE", 200, 600)["valid"]
        m.set_manual(True)
        assert m.manual_mode and "MANUAL_MODE" in alerts
        clock.advance(seconds=10)
        soc(db, 96, clock.now())
        assert m.tick([1], online=True) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/standby", (
            "chien de garde en mode manuel"
        )
        clock.advance(hours=1)
        assert not m.manual_mode, "une heure au plus"
        with db.tx() as cur:
            cur.execute("SELECT duration_s FROM local_decisions WHERE source = 'MANUAL' ORDER BY decision_id LIMIT 1")
            assert cur.fetchone()["duration_s"] == 600, "durée de l'ordre manuel conservée"


class TestCollector:
    def test_prise_sortie_dusine_notification_rpc_jusqua_la_base(self, db, clock, tmp_path):
        """Une prise Shelly neuve ne publie QUE ``<id>/events/rpc``, et ses relevés doivent arriver.

        « Generic status update over MQTT » est décoché en sortie d'usine : sans cette case,
        ``<id>/status/switch:0`` reste muet. L'agent n'écoutait que ce sujet-là — une prise réelle,
        branchée et connectée au courtier, ne produisait donc aucun relevé, et rien ne le disait : ni
        message reçu, ni rejet. Ce test part du sujet que la prise alimente vraiment et va jusqu'à la
        ligne en base.
        """
        c = Collector(db, Buffer(db, "4"), conf(tmp_path), clock.now, lambda: True, lambda: "4")
        assert "shellyplug-3/events/rpc" in c.topics(), "le sujet des notifications est abonné"

        ecrits = c.on_message(
            "shellyplug-3/events/rpc",
            json.dumps(
                {
                    "src": "shellyplug-3",
                    "dst": "shellyplug-3/events",
                    "method": "NotifyStatus",
                    "params": {
                        "ts": clock.now().timestamp(),
                        "switch:0": {
                            "id": 0,
                            "output": True,
                            "apower": 118.4,
                            "voltage": 232.6,
                            "current": 0.512,
                            "aenergy": {"total": 48213.7, "minute_ts": int(clock.now().timestamp())},
                        },
                    },
                }
            ).encode(),
        )
        assert ecrits == 4, "puissance, tension, courant et compteur d'énergie"
        with db.tx() as cur:
            cur.execute(
                "SELECT data_type, value FROM local_measurements WHERE device_id = 'device:3' ORDER BY data_type"
            )
            assert {l["data_type"]: float(l["value"]) for l in cur.fetchall()} == {
                "current_a": 0.512,
                "energy_total_wh": 48213.7,
                "power_w": 118.4,
                "voltage_v": 232.6,
            }

        # Le trafic ordinaire de ce sujet — un changement de sys, un appui de bouton — ne doit pas
        # gonfler le compteur de rejets : sinon le seul indicateur d'erreur de l'agent devient illisible.
        avant = c.rejected
        assert c.on_message("shellyplug-3/events/rpc", b'{"method":"NotifyEvent","params":{"events":[]}}') == 0
        assert c.on_message("shellyplug-3/events/rpc", b'{"method":"NotifyStatus","params":{"sys":{"uptime":9}}}') == 0
        assert c.rejected == avant, "une notification sans mesure n'est pas un message faux"

    def test_messages_stockes_batterie_immediate_mesures_agregees(self, db, clock, tmp_path):
        b = Buffer(db, "4")
        c = Collector(db, b, conf(tmp_path), clock.now, lambda: True, lambda: "4")
        assert set(c.topics()) >= {
            "shellyplug-3/status/switch:0",
            "site4/battery/status",
            "site4/weather/status",
        }
        for i in range(10):
            clock.set(T0 + dt.timedelta(minutes=i))
            c.on_message(
                "shellyplug-3/status/switch:0",
                json.dumps(
                    {
                        "apower": 100 + i,
                        "voltage": 230,
                        "current": 0.5,
                        "aenergy": {
                            "total": 1000 + i * 2,
                            "minute_ts": int(clock.now().timestamp()),
                        },
                    }
                ).encode(),
            )
        c.on_message(
            "site4/battery/status",
            json.dumps({"soc": 64.2, "power_charge_w": 0, "power_discharge_w": 120}).encode(),
        )
        c.on_message("site4/battery/status", b'{"soc": 400}')
        assert c.rejected == 1
        assert c.on_message("inconnu/topic", b"{}") == 0
        battery = [i for i in b.due(clock.now()) if i.payload.get("source") == "battery"]
        assert len(battery) == 1 and battery[0].priority == 2
        # Contrat strict du backend agent-sync : pas de synonymes locaux
        # (soc, charge_power_w, …), ni de métadonnées dans le payload.
        assert battery[0].payload == {
            "source": "battery",
            "stock_id": 1,
            "values": {
                "soc_pct": 64.2,
                "power_charge_w": 0.0,
                "power_discharge_w": 120.0,
            },
        }
        assert c.aggregate(T0 + dt.timedelta(minutes=11)) == 1, (
            "grâce de 2 minutes : seule la première fenêtre est close"
        )
        pushed = c.aggregate(T0 + dt.timedelta(minutes=12))
        assert pushed == 1, "la seconde fenêtre l'est une fois la grâce écoulée"
        standard = [i for i in b.due(T0 + dt.timedelta(hours=1)) if i.payload.get("aggregation") == "AVG_5MIN"]
        assert standard[0].payload == {
            "source": "device",
            "device_id": 3,
            "values": {
                "power_w": 102.0,
                "voltage_v": 230.0,
                "current_a": 0.5,
                "energy_wh": 8.0,
            },
            "quality": "OK",
            "aggregation": "AVG_5MIN",
        }
        assert standard[1].payload["values"]["energy_wh"] == 10.0, (
            "écart avec la fenêtre précédente compté : +2 Wh/min sur 5 minutes"
        )
        assert sum(i.payload["values"]["energy_wh"] for i in standard) == 18.0, (
            "compteur de 1000 à 1018 : rien perdu entre deux fenêtres"
        )
        assert standard[0].priority == 3
        assert c.aggregate(T0 + dt.timedelta(minutes=12)) == 0, "fenêtre déjà agrégée"


class TestProduction:
    def test_linky_en_production_energie_de_l_index_d_injection(self, db, clock, tmp_path):
        """E181 : un compteur Linky en production remonte l'énergie injectée (index EAIT), pas l'énergie soutirée."""
        b = Buffer(db, "4")
        config = AgentConfig(
            cloud_url="http://localhost:8088",
            db_url=DB_URL,
            data_dir=tmp_path,
            devices=(DeviceConf(7, "linky_tic", options={"port": "/dev/ttyUSB0", "mode": "standard"}),),
        )
        roles = {7: "PRODUCTION"}
        c = Collector(
            db,
            b,
            config,
            clock.now,
            lambda: True,
            lambda: "4",
            role_of=lambda device_id: roles.get(device_id, "CONSUMPTION"),
        )
        from h2s_agent import parsers as p

        for minute, soutire, injecte in (
            (0, 1000, 5000),
            (3, 1010, 5020),
            (6, 1015, 5060),
        ):
            at = T0 + dt.timedelta(minutes=minute)
            c.store(
                p._readings(
                    "device",
                    7,
                    {"energy_total_wh": soutire, "energy_injected_total_wh": injecte},
                    at,
                    "OK",
                ),
                at,
            )
        c.aggregate(T0 + dt.timedelta(minutes=13))
        pushed = sorted(
            (i.at, i.payload["values"]) for i in b.due(T0 + dt.timedelta(days=1)) if i.kind == "MEASUREMENT"
        )
        assert [v.get("energy_wh") for _, v in pushed] == [20.0, 40.0], (
            "accroissements de l'index d'injection (5000 → 5020 → 5060)"
        )


class TestOrchestration:
    def agent(self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys):
        save_identity(
            db,
            "http://localhost:8088",
            {
                "agent_uuid": "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
                "site_id": SITE,
                "site_name": "Recette",
                "keys": {
                    "decision_key_id": cloud_keys.key_id,
                    "decision_public_key": cloud_keys.public,
                    "gradient_public_key": cloud_keys.rsa_public,
                },
            },
        )
        a = Agent(
            conf(tmp_path),
            db,
            fake_cloud,
            publisher,
            clock,
            require_signed_package=False,
        )
        a.boot()
        return a

    def test_seuils_recus_bornes_par_le_cahier_des_charges(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """R5-06, R5-08 : une règle reçue à 10 %, 100 % et 10 s est appliquée à 15 %, 95 % et 30 s."""
        fake_cloud.rules_body = {
            "site_id": SITE,
            "stocks": [
                {
                    "stock_id": 1,
                    "name": "Jackery",
                    "soc_min": 10,
                    "soc_max": 100,
                    "p_charge_max_w": 500,
                    "p_discharge_max_w": 1000,
                    "min_transition_interval_s": 10,
                    "capacity_wh": 1002,
                }
            ],
        }
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        t = a.decisions.rules_for(1)
        assert (t.soc_min, t.soc_max, t.min_transition_interval_s) == (15, 95, 30)
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO local_security_rules (site_id, stock_id, rule_code, rule_name, threshold_value, unit, is_active)"
                " VALUES ('4', 2, 'SEC2', 'SoC minimum', 5, '%%', TRUE)"
            )
        assert a.decisions.rules_for(2).soc_min == 15, "règle locale hors bornes (saisie ou ancienne) ramenée à 15 %"

    def test_equipement_muet_5_minutes_alerte_critique_par_equipement(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """E033 (§4.4.1 « équipement hors ligne > 5 min, Critical ») : une alerte CRITICAL par équipement muet, au-delà de 5 minutes."""
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        a._started -= 3600
        with db.tx() as cur:
            for key in ("device:3", "stock:1"):
                cur.execute(
                    "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit, measurement_timestamp)"
                    " VALUES ('4', %s, 'device', 'power_w', 10, 'W', %s)",
                    (key, T0),
                )
        clock.set(T0 + dt.timedelta(minutes=4))
        a.silent_devices()
        assert not [
            i
            for i in a.buffer.due(clock.now() + dt.timedelta(days=1))
            if i.kind == "ALERT" and i.payload["code"] == "DEVICE_SILENT"
        ]
        clock.set(T0 + dt.timedelta(minutes=5, seconds=10))
        a.silent_devices()
        alerts = [
            i.payload
            for i in a.buffer.due(clock.now() + dt.timedelta(days=1))
            if i.kind == "ALERT" and i.payload["code"] == "DEVICE_SILENT"
        ]
        assert sorted(p["device"] for p in alerts) == ["device:3", "stock:1"], (
            "une alerte par équipement, pas une liste tronquée"
        )
        assert {p["severity"] for p in alerts} == {"CRITICAL"}
        assert "Aucune mesure reçue depuis 5 minutes" in alerts[0]["message"]

    def test_perte_du_reseau_mode_degrade_buffer_puis_reconnexion_et_vidage_par_priorite(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        assert a.states.state == state.RUNNING and fake_cloud.heartbeats == 1
        fake_cloud.down = True
        for _ in range(3):
            a.heartbeat()
        assert a.states.state == state.DEGRADED, "3 échecs consécutifs (CA-18.36)"
        a.collector.on_message("site4/battery/status", b'{"soc": 55}')
        a.buffer.push("METRIC", {"cpu_pct": 12}, T0, T0)
        assert a.sync() == 0 and a.buffer.size() >= 3, "données gardées pendant la coupure (CA-18.21)"
        fake_cloud.down = False
        a.heartbeat()
        assert a.states.state == state.DEGRADED, "hystérésis : un succès ne suffit pas"
        a.heartbeat()
        assert a.states.state == state.RUNNING
        assert a.sync() >= 3 and a.buffer.size() == 0, "envoi automatique à la reconnexion (CA-18.22)"
        kinds = [r["kind"] for r in fake_cloud.received]
        assert kinds.index("STATE") < kinds.index("METRIC") and kinds.index("MEASUREMENT") < kinds.index("METRIC")
        with db.tx() as cur:
            cur.execute("SELECT status, previous_status FROM agent_status_history ORDER BY status_id")
            assert [r["status"] for r in cur.fetchall()] == [
                "INIT",
                "RUNNING",
                "DEGRADED",
                "RUNNING",
            ]

    def test_sante_base_et_broker_mqtt(self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys, monkeypatch):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        assert a.health() == (
            200,
            {
                "status": "UP",
                "state": state.RUNNING,
                "checks": {"database": "UP", "mqtt_broker": "UP"},
            },
        )
        publisher.connected = False  # propriété, comme MqttBridge.connected
        code, body = a.health()
        assert code == 503 and body["checks"] == {
            "database": "UP",
            "mqtt_broker": "DOWN",
        }, "broker local injoignable (D295)"
        publisher.connected = True
        # Pont MQTT réel jamais connecté (broker absent) : sa propriété connected rend la sonde DOWN.
        from h2s_agent.config import MqttConf
        from h2s_agent.mqtt import MqttBridge

        a.publisher = MqttBridge(MqttConf(port=1), "h2s-agent-sante", [], lambda t, p: None)
        assert a.health()[0] == 503
        a.publisher = publisher
        monkeypatch.setattr(db, "ping", lambda: (_ for _ in ()).throw(RuntimeError("base fermée")))
        code, body = a.health()
        assert code == 503 and body["checks"]["database"] == "DOWN"

    def test_sonde_tpm_remontee_au_cloud_et_exposee_localement(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """E175 / CA-18.27 : sans module, l'état dit « absent » (plus « non livré ») et part avec le battement de cœur."""
        from h2s_agent.__main__ import _routes

        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        a.config = AgentConfig(**{**a.config.__dict__, "tpm_tcti": f"device:{tmp_path / 'tpmrm0'}"})
        a.heartbeat()
        assert fake_cloud.last_tpm["status"] == "ABSENT" and fake_cloud.last_tpm["simulator"] is False
        status, view = _routes(a)[("GET", "/api/agent/tpm")](None)
        assert status == 200 and view["tpm"]["status"] == "ABSENT" and view["key_store"] == "SOFTWARE"
        assert "non livré" not in json.dumps(view, ensure_ascii=False)

    def test_quatre_sources_simultanees_shelly_powerlogic_linky_batterie(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """E172 / CA-18.3 : prise Shelly (MQTT), centrale PowerLogic (Modbus TCP simulé), Linky (TIC) et batterie, écrites ensemble."""
        import dataclasses
        import threading

        from h2s_agent import tic

        from .test_collecte_locale import (
            HISTORIQUE,
            ModbusSimulator,
            frame,
            pm5000_registers,
        )

        server = ModbusSimulator(pm5000_registers(energy_wh=1_000_000))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            devices = conf(tmp_path).devices + (
                DeviceConf(
                    12,
                    "powerlogic_modbus",
                    options={
                        "host": "127.0.0.1",
                        "port": server.server_address[1],
                        "profile": "pm5000",
                    },
                ),
                DeviceConf(
                    7,
                    "linky_tic",
                    options={"port": "/dev/ttyUSB-linky", "mode": "historique"},
                ),
            )
            save_identity(
                db,
                "http://localhost:8088",
                {
                    "agent_uuid": "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
                    "site_id": SITE,
                    "site_name": "Recette",
                    "keys": {
                        "decision_key_id": cloud_keys.key_id,
                        "decision_public_key": cloud_keys.public,
                        "gradient_public_key": cloud_keys.rsa_public,
                    },
                },
            )
            a = Agent(
                dataclasses.replace(conf(tmp_path), devices=devices),
                db,
                fake_cloud,
                publisher,
                clock,
                require_signed_package=False,
            )
            assert [t.every_s for t in a.tasks if t.name == "modbus"] == [60] and len(a.tic_readers) == 1
            for minute in range(6):
                clock.set(T0 + dt.timedelta(minutes=minute))
                a.collector.on_message(
                    "shellyplug-3/status/switch:0",
                    json.dumps({"apower": 80, "aenergy": {"total": 100 + minute}}).encode(),
                )
                server.registers.update(pm5000_registers(energy_wh=1_000_000 + 25 * minute))
                assert a.poll_modbus() == 5
                a.tic_readers[0].handle(frame(HISTORIQUE))
                a.collector.on_message("site4/battery/status", json.dumps({"soc": 60 - minute}).encode())
            with db.tx() as cur:
                cur.execute(
                    "SELECT device_id, count(DISTINCT measurement_timestamp) AS n FROM local_measurements GROUP BY device_id ORDER BY device_id"
                )
                assert {r["device_id"]: r["n"] for r in cur.fetchall()} == {
                    "device:12": 6,
                    "device:3": 6,
                    "device:7": 6,
                    "stock:1": 6,
                }
            a.collector.aggregate(T0 + dt.timedelta(minutes=8))
            pushed = {
                i.payload["device_id"]: i.payload["values"]
                for i in a.buffer.due(T0 + dt.timedelta(hours=1))
                if i.payload.get("aggregation") == "AVG_5MIN"
            }
            assert pushed[12]["power_w"] == pytest.approx(1500) and pushed[12]["energy_wh"] == 100.0, (
                "PowerLogic : énergie du compteur Modbus"
            )
            assert pushed[7] == {"current_a": 2.0, "energy_wh": 0.0}, (
                "Linky au même index : énergie nulle ; puissance apparente gardée en local"
            )
            server.shutdown()
            server.server_close()
            assert a.poll_modbus() == 0 and 12 in a.modbus_failures, (
                "centrale muette : comptée, les autres sources continuent"
            )
            assert isinstance(a.tic_readers[0], tic.TicReader)
        finally:
            server.server_close()

    def test_preuve_locale_signee_en_coupure_bufferisee_puis_remise_au_cloud(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """E188 / §8 B11 : réseau coupé pendant une décharge ; l'agent signe le relevé de la période (énergie, SoC, ordres), chaîné au
        précédent, le garde dans le buffer et le remet à la reconnexion ; le cloud le vérifie avec la clé enrôlée."""
        from h2s_agent import crypto
        from h2s_agent.crypto import SoftwareKeyStore

        keys = SoftwareKeyStore(tmp_path / "keys" / "agent.pem")
        save_identity(
            db,
            "http://localhost:8088",
            {
                "agent_uuid": "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
                "site_id": SITE,
                "site_name": "Recette",
                "keys": {
                    "decision_key_id": cloud_keys.key_id,
                    "decision_public_key": cloud_keys.public,
                    "gradient_public_key": cloud_keys.rsa_public,
                },
            },
        )
        a = Agent(
            conf(tmp_path),
            db,
            fake_cloud,
            publisher,
            clock,
            require_signed_package=False,
            keys=keys,
        )
        a.boot()
        assert [t.every_s for t in a.tasks if t.name == "proofs"] == [300]
        fake_cloud.down = True
        for _ in range(3):
            a.heartbeat()
        assert a.states.state == state.DEGRADED
        soc(db, 80, T0)
        a.decisions.receive([])
        for minute in range(30):
            clock.set(T0 + dt.timedelta(minutes=minute))
            a.collector.on_message(
                "shellyplug-3/status/switch:0",
                json.dumps({"apower": 1200, "aenergy": {"total": 5000 + 20 * minute}}).encode(),
            )
            a.collector.on_message("site4/battery/status", json.dumps({"soc": 80 - minute * 0.5}).encode())
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO local_decisions (site_id, action, power_w, source, execution_status, executed_at, stock_id, decision_ref, duration_s)"
                " VALUES ('4', 'DISCHARGE', 800, 'CLOUD_CACHE', 'EXECUTED', %s, 1, 'd-1', 1800)",
                (T0 + dt.timedelta(minutes=1),),
            )
        clock.set(T0 + dt.timedelta(minutes=33))
        assert a.build_proofs() == 1, "période 08:00–08:30 close depuis la grâce de 2 minutes"
        assert a.build_proofs() == 0, "une preuve par période"
        [item] = [i for i in a.buffer.due(clock.now() + dt.timedelta(hours=1)) if i.kind == "LOCAL_PROOF"]
        assert item.priority == 1, "preuve critique, jamais évincée comme une métrique"
        payload = json.loads(item.payload["payload"])
        assert (
            payload["offline"] is True
            and payload["agent_uuid"] == "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e"
            and payload["site_id"] == SITE
        )
        assert payload["period_start"] == "2026-09-14T08:00:00Z" and payload["period_end"] == "2026-09-14T08:30:00Z"
        [device] = payload["devices"]
        assert device["device"] == "device:3" and device["energy_wh"] == 580.0 and device["samples"] == 30
        [battery] = payload["batteries"]
        assert battery["soc_start"] == 80 and battery["soc_end"] == 65.5
        assert payload["decisions"][0]["action"] == "DISCHARGE" and payload["decisions"][0]["source"] == "CLOUD_CACHE"
        assert payload["prev_hash"] is None and payload["measurements"]["count"] == 90, (
            "30 relevés de prise (puissance, compteur), 30 de batterie"
        )
        assert crypto.verify_agent_signature(
            "Ed25519",
            keys.public_key(),
            item.payload["payload"].encode(),
            item.payload["signature"],
        )
        assert item.payload["hash"] == crypto.sha256_hex(item.payload["payload"].encode())
        clock.set(T0 + dt.timedelta(minutes=63))
        assert a.build_proofs() == 1
        second = [
            json.loads(i.payload["payload"])
            for i in a.buffer.due(clock.now() + dt.timedelta(hours=1))
            if i.kind == "LOCAL_PROOF"
        ][-1]
        assert second["prev_hash"] == item.payload["hash"], "preuves chaînées : une période retirée se voit"
        fake_cloud.down = False
        a.heartbeat()
        a.heartbeat()
        a.sync()
        received = [r for r in fake_cloud.received if r["kind"] == "LOCAL_PROOF"]
        assert len(received) == 2 and not [
            i for i in a.buffer.due(clock.now() + dt.timedelta(hours=1)) if i.kind == "LOCAL_PROOF"
        ]
        assert all(
            crypto.verify_agent_signature(
                "Ed25519",
                keys.public_key(),
                r["payload"]["payload"].encode(),
                r["payload"]["signature"],
            )
            for r in received
        )
        with db.tx() as cur:
            cur.execute("SELECT count(*) AS n FROM local_proofs")
            assert cur.fetchone()["n"] == 2, "preuves gardées en local après remise"

    def test_compteur_remis_a_zero(self, db, clock, tmp_path):
        b = Buffer(db, "4")
        c = Collector(db, b, conf(tmp_path), clock.now, lambda: True, lambda: "4")
        for i, total in enumerate([500, 510, 3, 8, 13]):
            clock.set(T0 + dt.timedelta(minutes=i))
            c.on_message(
                "shellyplug-3/status/switch:0",
                json.dumps(
                    {
                        "apower": 50,
                        "aenergy": {
                            "total": total,
                            "minute_ts": int(clock.now().timestamp()),
                        },
                    }
                ).encode(),
            )
        c.aggregate(T0 + dt.timedelta(minutes=8))
        [item] = [i for i in b.due(T0 + dt.timedelta(hours=1)) if i.payload.get("aggregation") == "AVG_5MIN"]
        assert item.payload["values"]["energy_wh"] == 23.0, "10 Wh, remise à zéro à 3 Wh, puis +5 et +5"

    def test_arret_du_service_batteries_en_veille_puis_rendu(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        a.shutdown()
        assert "site4/commands/standby" in [t for t, _ in publisher.sent], (
            "plus de surveillance : veille avant la sortie (D247)"
        )
        with db.tx() as cur:
            cur.execute("SELECT source, action FROM local_decisions ORDER BY created_at DESC LIMIT 1")
            assert dict(cur.fetchone()) == {
                "source": "SAFE_STANDBY",
                "action": "STANDBY",
            }
        assert a.states.state == state.SHUTDOWN

    def test_arret_sans_veille_pour_une_batterie_non_raccordee(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """Contrôle du 14/09/2026 (D290) : le site compte deux stockages, un seul est raccordé à cet agent. L'arrêt met en veille celui
        qu'il pilote, sans écrire ni rendre au cloud une veille « en échec » pour l'autre."""
        fake_cloud.rules_body["stocks"].append(
            {
                **fake_cloud.rules_body["stocks"][0],
                "stock_id": 2,
                "name": "Jumeau sans agent",
            }
        )
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        a.shutdown()
        with db.tx() as cur:
            cur.execute("SELECT stock_id, execution_status FROM local_decisions ORDER BY decision_id")
            assert [(r["stock_id"], r["execution_status"]) for r in cur.fetchall()] == [(1, "EXECUTED")]
        assert not [
            r
            for r in fake_cloud.received
            if r["kind"] == "DECISION_REPORT" and r["payload"].get("execution_status") == "FAILED"
        ]

    def test_redemarrage_reprend_l_ordre_interrompu(self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        soc(db, 50, clock.now())
        a.decisions.receive([cloud_keys.order(clock.now(), "CHARGE", 200, duration=7200)])
        assert a.decisions.tick([1], True) == ["EXECUTED"]
        a.shutdown()
        clock.advance(minutes=10)
        soc(db, 55, clock.now())
        b = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        with db.tx() as cur:
            cur.execute(
                "SELECT source, execution_status, rejection_rule FROM local_decisions ORDER BY decision_id DESC LIMIT 1"
            )
            last = cur.fetchone()
        assert last["source"] == "CLOUD_LIVE" and (
            last["execution_status"] == "EXECUTED" or last["rejection_rule"] == "SEC6"
        ), "reprise tentée au démarrage"
        clock.advance(minutes=6)
        soc(db, 56, clock.now())
        b.decisions.tick([1], True)
        commands = [t for t, _ in publisher.sent if "/commands/" in t]
        assert commands[-1] == "site4/commands/charge", "la charge interrompue reprend"

    def test_redemarrage_ne_relance_pas_une_veille_du_chien_de_garde(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        soc(db, 50, clock.now())
        a.decisions.receive([cloud_keys.order(clock.now(), "CHARGE", 200, duration=7200)])
        assert a.decisions.tick([1], True) == ["EXECUTED"]
        clock.advance(seconds=10)
        soc(db, 96, clock.now())
        assert a.decisions.tick([1], True) == ["EXECUTED"], "chien de garde : veille"
        a.shutdown()
        clock.advance(minutes=10)
        soc(db, 60, clock.now())
        self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        commands = [t for t, _ in publisher.sent if "/commands/" in t]
        assert commands[-1] == "site4/commands/standby", (
            "la veille décidée pour la sécurité n'est pas annulée par un redémarrage"
        )

    def test_erreur_fatale_batteries_en_veille_meme_sans_base(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys, monkeypatch
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        soc(db, 50, clock.now())
        a.decisions.receive([cloud_keys.order(clock.now(), "DISCHARGE", 200, duration=86_400)])
        assert a.decisions.tick([1], True) == ["EXECUTED"]

        def base_perdue(*_a, **_k):
            raise psycopg.OperationalError("disque plein")

        monkeypatch.setattr(a, "run_once", base_perdue)
        monkeypatch.setattr(a, "stock_ids", base_perdue)
        monkeypatch.setattr(a.decisions, "safe_standby", base_perdue)
        with pytest.raises(psycopg.OperationalError):
            a.run_forever()
        assert (
            publisher.sent[-1][0] == "site4/commands/standby" and publisher.sent[-1][1]["source"] == "SAFE_STANDBY"
        ), "erreur fatale, base indisponible : veille publiée directement"

    def test_integrite_bloquee_chien_de_garde_actif_ordre_manuel_refuse(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        from h2s_agent.__main__ import _routes

        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        soc(db, 50, clock.now())
        a.decisions.receive([cloud_keys.order(clock.now(), "CHARGE", 200, duration=3600)])
        assert a.decisions.tick([1], True) == ["EXECUTED"]
        a.execution_blocked = True
        status, body = _routes(a)[("POST", "/api/agent/decisions")]({"stock_id": 1, "action": "CHARGE", "power_w": 100})
        assert status == 409 and body["code"] == "execution_blocked"
        clock.advance(seconds=10)
        soc(db, 96, clock.now())
        a.run_decisions()
        commands = [t for t, _ in publisher.sent if "/commands/" in t]
        assert commands[-1] == "site4/commands/standby", "exécution bloquée, chien de garde actif"

    def test_integrite_reverifiee_toutes_les_heures_dependance_ou_configuration_modifiee_bloque(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        """E176 : dépendance modifiée après le démarrage, puis agent.json modifié : exécution bloquée au passage suivant, batterie en veille."""
        import dataclasses

        from h2s_agent import integrity

        pkg, site = tmp_path / "pkg", tmp_path / "site"
        pkg.mkdir()
        site.mkdir()
        (pkg / "a.py").write_text("x = 1")
        (site / "dep.py").write_text("y = 1")
        agent_json = tmp_path / "agent.json"
        agent_json.write_text(json.dumps({"cloud_url": "http://localhost:8088", "db_url": DB_URL}))
        (pkg / "MANIFEST.json").write_text(
            json.dumps(
                cloud_keys.sign(
                    integrity.manifest_payload(pkg, "1.0.0", [site], {"cloud_url": "http://localhost:8088"})
                )
            )
        )
        save_identity(
            db,
            "http://localhost:8088",
            {
                "agent_uuid": "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
                "site_id": SITE,
                "site_name": "Recette",
                "keys": {
                    "decision_key_id": cloud_keys.key_id,
                    "decision_public_key": cloud_keys.public,
                    "gradient_public_key": cloud_keys.rsa_public,
                },
            },
        )
        c = dataclasses.replace(conf(tmp_path), package_public_key=cloud_keys.public, config_path=agent_json)

        def boot():
            agent = Agent(
                c,
                db,
                fake_cloud,
                publisher,
                clock,
                require_signed_package=True,
                integrity_root=pkg,
                site_packages=[site],
            )
            agent.boot()
            return agent

        a = boot()
        assert a.integrity.ok and not a.execution_blocked, a.integrity.reason
        assert [t.every_s for t in a.tasks if t.name == "integrity"] == [3600], "re-vérification horaire"
        soc(db, 50, clock.now())
        a.decisions.receive([cloud_keys.order(clock.now(), "CHARGE", 200, duration=7200)])
        assert a.decisions.tick([1], True) == ["EXECUTED"]
        (site / "dep.py").write_text("y = 2")
        a.verify_integrity()
        assert a.execution_blocked and "dépendances" in a.integrity.reason
        assert [t for t, _ in publisher.sent if "/commands/" in t][-1] == "site4/commands/standby", (
            "code altéré : la batterie ne reste pas sous un ordre"
        )
        assert any(
            i.payload.get("code") == "INTEGRITY_FAILED"
            for i in a.buffer.due(clock.now() + dt.timedelta(minutes=1))
            if i.kind == "ALERT"
        )
        status, _ = __import__("h2s_agent.__main__", fromlist=["_routes"])._routes(a)[("POST", "/api/agent/decisions")](
            {"stock_id": 1, "action": "CHARGE"}
        )
        assert status == 409

        (site / "dep.py").write_text("y = 1")
        clock.advance(hours=2)
        b = boot()
        assert b.integrity.ok and not b.execution_blocked
        agent_json.write_text(json.dumps({"cloud_url": "http://127.0.0.1:9999", "db_url": DB_URL}))
        b.verify_integrity()
        assert b.execution_blocked and "cloud_url" in b.integrity.reason, "agent.json modifié pendant l'exécution"

    def test_differe_par_le_cloud_garde_puis_renvoye_et_curseur_persiste(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        now = clock.now()
        a.buffer.push("METRIC", {"cpu_pct": 1, "later": True}, now, now)
        a.buffer.push("METRIC", {"cpu_pct": 2}, now, now)
        a.sync()
        assert a.buffer.size() == 1, "différé par le quota du cloud : gardé, pas acquitté"
        fake_cloud.batch_cursor = "42"
        a.poll_batch()
        assert self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)._cursor == "42", (
            "curseur du lot d'ordres persisté"
        )

    def test_erreur_500_isolee_le_reste_du_buffer_part(self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        now = clock.now()
        for i in range(30):
            a.buffer.push("METRIC", {"cpu_pct": i, **({"boom": True} if i == 7 else {})}, now, now)
        a.sync()
        assert a.states.state == state.RUNNING, "une erreur 500 n'est pas une perte de réseau"
        sent = [r for r in fake_cloud.received if r["kind"] == "METRIC"]
        assert len(sent) == 29 and all(not r["payload"].get("boom") for r in sent), (
            "l'enregistrement fautif isolé, les autres acquittés"
        )

    def test_erreur_de_tache_reprise_automatique(self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)

        def boom():
            raise RuntimeError("panne")

        a.tasks = [a.tasks[0].__class__("boom", 10, boom)]
        a.run_once(monotonic=1000)
        with db.tx() as cur:
            cur.execute("SELECT status FROM agent_status_history ORDER BY status_id")
            assert [r["status"] for r in cur.fetchall()][-3:] == [
                "ERROR",
                "INIT",
                "RUNNING",
            ], "CA-18.37"
        assert any(
            r["kind"] == "ALERT"
            for r in [{"kind": i.kind} for i in a.buffer.due(clock.now() + dt.timedelta(minutes=1))]
        ), "CA-18.39"

    def test_regles_synchronisees_ordre_recu_execute_et_metriques(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        fake_cloud.rules_body["stocks"][0]["p_charge_max_w"] = 300
        a.refresh_rules()
        assert a.decisions.rules_for(1).p_charge_max_w == 300, "copie locale des règles (CA-18.34)"
        soc(db, 50, clock.now())
        fake_cloud.orders = [cloud_keys.order(clock.now(), "CHARGE", 450)]
        a.poll_decisions()
        assert publisher.sent[-1][0] == "site4/commands/charge" and publisher.sent[-1][1]["power_w"] == 300
        if not a.collect_metrics():
            a.metrics.collect(3, a.buffer.size(), 1, 0)
        with db.tx() as cur:
            cur.execute("SELECT count(DISTINCT metric_type) AS n FROM local_metrics")
            assert cur.fetchone()["n"] >= 4, "CA-18.38"

    def test_contribution_federee_puis_modele_global_verifie(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        a = self.agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO local_aggregated (site_id, device_id, data_type, avg_value, count, aggregation_period, period_start, period_end)"
                " SELECT '4', 'device:3', 'power_w', 400 + 200 * sin(h / 24.0 * 6.283), 60, '1h', %s - (h * interval '1 hour'),"
                " %s - ((h - 1) * interval '1 hour') FROM generate_series(1, 200) h",
                (clock.now(), clock.now()),
            )
        started = time.monotonic()
        a.contribute()
        assert time.monotonic() - started < 300, "CA-18.16"
        body = fake_cloud.gradient_bodies[-1]
        assert body["sample_count"] >= 48 and "delta" not in json.dumps(body), (
            "seule l'enveloppe chiffrée part (CA-18.17)"
        )
        weights = [0.1] * len(FEATURES)
        model = cloud_keys.sign(
            {
                "v": 1,
                "type": "model",
                "model_type": "consumption_1h",
                "version": "federated-1",
                "features": FEATURES,
                "weights": weights,
                "created_at": "2026-09-14T08:00:00Z",
            }
        )
        fake_cloud.model_body = model
        a.refresh_model()
        assert Federated(db, clock.now, lambda: "4").active_model() == (
            "federated-1",
            weights,
        ), "CA-18.20"
        forged = dict(model, payload=model["payload"].replace("0.1", "9.9"))
        fake_cloud.model_body = forged
        a.refresh_model()
        assert Federated(db, clock.now, lambda: "4").active_model()[0] == "federated-1", "modèle altéré écarté"


class TestLocalApi:
    def test_jeton_exige_sauf_sante(self, db):
        api = LocalApi(
            0,
            "jeton-de-test",
            {
                ("GET", "/api/agent/health"): lambda b: (200, {"ok": True}),
                ("GET", "/api/agent/status"): lambda b: (200, {"state": "RUNNING"}),
            },
        )
        api.start()
        try:
            base = f"http://127.0.0.1:{api.port}"
            assert json.load(urllib.request.urlopen(base + "/api/agent/health")) == {"ok": True}
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/api/agent/status")
            assert e.value.code == 401
            req = urllib.request.Request(
                base + "/api/agent/status",
                headers={"Authorization": "Bearer jeton-de-test"},
            )
            assert json.load(urllib.request.urlopen(req)) == {"state": "RUNNING"}
            negative = urllib.request.Request(base + "/api/agent/health", headers={"Content-Length": "-1"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(negative, timeout=5)
            assert e.value.code == 400, "longueur négative refusée, pas de lecture jusqu'à la fin de connexion"
        finally:
            api.stop()


class TestProductionTheoriqueDansLeFluxCloud:
    """Le relevé météo part au cloud AVEC la production ENR théorique (brique 18 §4.1.4).

    Ce que ce test garde n'est pas la formule — elle a ses propres tests sur ses exemples chiffrés —
    mais le CHAÎNON : le calcul est bien fait au prétraitement, et le résultat voyage dans le même
    message que la météo. Sans cela, le cloud recevrait de l'irradiance et du vent bruts, et devrait
    refaire le calcul lui-même : exactement ce que la spécification demande d'éviter.
    """

    @staticmethod
    def _conf_avec_sources(tmp_path: Path) -> AgentConfig:
        base = conf(tmp_path)
        return replace(
            base,
            sources=(
                SourceConf(source_id=1, type="solar", surface_m2=2.0, rendement_pv=0.22),
                SourceConf(source_id=2, type="wind", diametre_rotor_m=1.8, rendement_eolien=0.35),
            ),
        )

    def _agreger_meteo(self, db, clock, config, irradiance: float, vent: float) -> dict:
        b = Buffer(db, "4")
        c = Collector(db, b, config, clock.now, lambda: True, lambda: "4")
        for i in range(4):
            clock.set(T0 + dt.timedelta(minutes=i))
            c.on_message(
                "site4/weather/status",
                json.dumps({"irradiance_w_m2": irradiance, "wind_speed_m_s": vent, "temperature_c": 18.0}).encode(),
            )
        c.aggregate(T0 + dt.timedelta(minutes=12))
        meteo = [i for i in b.due(T0 + dt.timedelta(hours=1)) if i.payload.get("source") == "weather"]
        assert meteo, "aucune fenêtre météo mise en tampon"
        return meteo[0].payload["values"]

    def test_les_conditions_de_la_specification_donnent_les_chiffres_de_la_specification(self, db, clock, tmp_path):
        # G = 600 W/m², V = 5 m/s : 264 W de solaire et 68,2 W d'éolien, comme dans le document.
        valeurs = self._agreger_meteo(db, clock, self._conf_avec_sources(tmp_path), 600.0, 5.0)

        assert valeurs["irradiance_w_m2"] == 600.0, "le relevé météo reste intact"
        assert valeurs["wind_speed_m_s"] == 5.0
        assert valeurs["production_solaire_w"] == pytest.approx(264.0)
        assert valeurs["production_eolienne_w"] == pytest.approx(68.2, abs=0.1)
        assert valeurs["production_totale_w"] == pytest.approx(332.2, abs=0.1)

    def test_un_site_sans_source_declaree_envoie_trois_zeros_et_non_rien(self, db, clock, tmp_path):
        # Trois zéros se lisent ; des champs absents obligeraient le cloud à distinguer « pas calculé »
        # de « calculé à zéro », ce qu'il ne peut pas faire.
        valeurs = self._agreger_meteo(db, clock, conf(tmp_path), 600.0, 5.0)

        assert valeurs["production_solaire_w"] == 0.0
        assert valeurs["production_eolienne_w"] == 0.0
        assert valeurs["production_totale_w"] == 0.0

    def test_la_production_theorique_ne_se_melange_pas_aux_mesures_d_equipement(self, db, clock, tmp_path):
        b = Buffer(db, "4")
        c = Collector(db, b, self._conf_avec_sources(tmp_path), clock.now, lambda: True, lambda: "4")
        for i in range(4):
            clock.set(T0 + dt.timedelta(minutes=i))
            c.on_message(
                "shellyplug-3/status/switch:0",
                json.dumps(
                    {
                        "apower": 100.0,
                        "voltage": 230,
                        "aenergy": {"total": 1000 + i, "minute_ts": int(clock.now().timestamp())},
                    }
                ).encode(),
            )
            c.on_message("site4/weather/status", json.dumps({"irradiance_w_m2": 600.0, "wind_speed_m_s": 5.0}).encode())
        c.aggregate(T0 + dt.timedelta(minutes=12))
        lots = b.due(T0 + dt.timedelta(hours=1))
        appareil = [i for i in lots if i.payload.get("source") == "device"]
        assert appareil, "aucune fenêtre d'équipement"
        for item in appareil:
            assert "production_totale_w" not in item.payload["values"], (
                "la production théorique est une estimation météo, jamais une mesure d'équipement"
            )


class TestInstantaneTempsReel:
    """Le canal temps réel ne doit jamais présenter une somme PARTIELLE comme le total du site.

    Défaut mesuré le 05/10/2026 depuis le cloud : le flux poussait « consumption = 0.0 » pendant que
    l'API REST rendait « 4.34 » au même instant, douze relevés de suite. La somme ne portait que sur les
    équipements ayant publié dans les dix dernières secondes ; avec huit prises qui ne publient pas en
    même temps, ce sous-ensemble est le plus souvent minoritaire.

    À l'écran : les tuiles affichent la bonne valeur au chargement, le canal les écrase avec un zéro cinq
    secondes plus tard, et recharger la page les ramène — puisque le rechargement passe par le REST.
    C'est le signalement du client, mot pour mot.
    """

    @staticmethod
    def _mesure(db: Database, cle: str, valeur: float, at: dt.datetime, type_donnee: str = "power_w") -> None:
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit,"
                " measurement_timestamp) VALUES ('4', %s, 'device', %s, %s, 'W', %s)",
                (cle, type_donnee, valeur, at),
            )

    @staticmethod
    def _publieur(db: Database, appareils: list[tuple[str, str]]) -> LivePublisher:
        return LivePublisher(
            db,
            lambda: 4,
            lambda: appareils,
            list,
            lambda: T0,
        )

    def test_tous_les_equipements_frais_la_somme_est_envoyee(self, db):
        appareils = [("device:1", "CONSUMPTION"), ("device:2", "CONSUMPTION")]
        self._mesure(db, "device:1", 3.0, T0 - dt.timedelta(seconds=2))
        self._mesure(db, "device:2", 1.34, T0 - dt.timedelta(seconds=3))

        p = self._publieur(db, appareils).snapshot(T0)

        assert p is not None
        # L'instantané arrondit au dixième de watt (round(..., 1)) : 3,0 + 1,34 = 4,34 → 4,3.
        assert p["consumption_w"] == pytest.approx(4.3)

    def test_un_seul_equipement_frais_sur_deux_ne_donne_PAS_un_total(self, db):
        # Avant le correctif, ce cas envoyait « 3.0 » — ou « 0.0 » si la seule prise fraîche était à
        # l'arrêt — et le tableau de bord écrasait sa valeur juste avec ce chiffre faux.
        appareils = [("device:1", "CONSUMPTION"), ("device:2", "CONSUMPTION")]
        self._mesure(db, "device:1", 3.0, T0 - dt.timedelta(seconds=2))
        self._mesure(db, "device:2", 1.34, T0 - dt.timedelta(minutes=4))

        p = self._publieur(db, appareils).snapshot(T0)

        assert p is None or p["consumption_w"] is None, (
            "une somme partielle ne doit pas être présentée comme le total du site"
        )

    def test_la_prise_a_l_arret_ne_fait_pas_tomber_le_total_a_zero(self, db):
        # Le cas le plus trompeur : la seule prise fraîche consomme réellement 0 W. La somme vaut donc
        # zéro, et ce zéro est VRAI pour cette prise — mais faux pour le site.
        appareils = [("device:1", "CONSUMPTION"), ("device:2", "CONSUMPTION")]
        self._mesure(db, "device:1", 0.0, T0 - dt.timedelta(seconds=1))
        self._mesure(db, "device:2", 4.34, T0 - dt.timedelta(minutes=4))

        p = self._publieur(db, appareils).snapshot(T0)

        assert p is None or p["consumption_w"] is None

    def test_production_et_consommation_sont_jugees_separement(self, db):
        # La production complète s'envoie même si la consommation ne l'est pas : les deux rôles ne se
        # tiennent pas, et taire l'un parce que l'autre manque perdrait une information juste.
        appareils = [
            ("device:1", "PRODUCTION"),
            ("device:2", "CONSUMPTION"),
            ("device:3", "CONSUMPTION"),
        ]
        self._mesure(db, "device:1", 120.0, T0 - dt.timedelta(seconds=2))
        self._mesure(db, "device:2", 3.0, T0 - dt.timedelta(seconds=2))
        self._mesure(db, "device:3", 1.0, T0 - dt.timedelta(minutes=4))

        p = self._publieur(db, appareils).snapshot(T0)

        assert p is not None
        assert p["production_w"] == pytest.approx(120.0)
        assert p["consumption_w"] is None

    def test_rien_de_frais_aucun_instantane(self, db):
        appareils = [("device:1", "CONSUMPTION")]
        self._mesure(db, "device:1", 3.0, T0 - dt.timedelta(minutes=10))

        assert self._publieur(db, appareils).snapshot(T0) is None
