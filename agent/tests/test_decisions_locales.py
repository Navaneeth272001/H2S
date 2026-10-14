"""Mode dégradé autonome (CDC §2.18, §5.1 ; R5-20, C-9, E182) : décisions locales §5.1 hors connexion, prix day-ahead en cache,
jamais de rejeu d'un ordre échu, énergie mesurée d'une décharge remontée à sa clôture."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from h2s_agent import local_policy
from h2s_agent.buffer import Buffer
from h2s_agent.decisions import DecisionManager

from .conftest import T0

VECTEURS_5_1 = Path(__file__).resolve().parents[3] / "data" / "demo" / "vecteurs_regle_5_1.json"
VECTEURS_MODE_MANUEL = Path(__file__).resolve().parents[3] / "data" / "demo" / "vecteurs_mode_manuel_local.json"
POLICY = {
    "auto_pilot": True,
    "surplus_min": 50,
    "peak_threshold": 300,
    "soc_reserve": 30,
    "price_low": 0.12,
    "price_high": 0.18,
    "devices": [
        {"device_id": 3, "role": "PRODUCTION"},
        {"device_id": 8, "role": "CONSUMPTION"},
    ],
}


def test_regle_5_1_identique_au_cloud_sur_les_vecteurs_communs():
    """Copie embarquée de AutoPolicy.decideCdc : mêmes décisions que Java (CdcBatteryRulesTest#sharedVectors) et data/demo/regle_5_1.py."""
    vectors = json.loads(VECTEURS_5_1.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 15
    for v in vectors:
        t = local_policy.thresholds(v["site"])
        assert local_policy.decide(
            v["soc"],
            v["surplus_w"],
            v["consumption_w"],
            t,
            v["p_charge_max_w"],
            v["p_discharge_max_w"],
            v["price"],
            v["reserved"],
        ) == (v["action"], v["power_w"]), v["name"]


def test_mode_manuel_oppose_a_l_autonomie_locale_sur_les_vecteurs_communs():
    """V2 : même prédicat que EdgeDtos.LocalPolicy#localDecisionAllowed (EdgeLocalAutonomyTest#modeManuelVecteursCommuns)."""
    vectors = json.loads(VECTEURS_MODE_MANUEL.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 6
    for v in vectors:
        policy = {
            "auto_pilot": v["auto_pilot"],
            "manual_override": v["manual_override"],
        }
        assert local_policy.local_decision_allowed(policy, v["stock_id"]) is v["allowed"], v["name"]


def measure(db, key: str, data_type: str, value: float, at: dt.datetime, source: str = "device") -> None:
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO local_measurements (site_id, device_id, device_type, data_type, value, unit, measurement_timestamp)"
            " VALUES ('4', %s, %s, %s, %s, 'W', %s) ON CONFLICT DO NOTHING",
            (key, source, data_type, value, at),
        )


def manager(db, publisher, identity, clock, alerts=None):
    return DecisionManager(
        db,
        Buffer(db, "4"),
        publisher,
        lambda: identity,
        lambda stock: "site4/commands" if stock == 1 else None,
        clock.now,
        (lambda code, sev, msg, **kw: alerts.append(code)) if alerts is not None else None,
        devices=lambda: [("device:3", "CONSUMPTION"), ("device:8", "CONSUMPTION")],
    )


def site(db, at: dt.datetime, production: float, consumption: float, soc: float) -> None:
    measure(db, "device:3", "power_w", production, at)
    measure(db, "device:8", "power_w", consumption, at)
    measure(db, "stock:1", "soc_pct", soc, at, "battery")


def reports(db, clock):
    return [i.payload for i in Buffer(db, "4").due(clock.now() + dt.timedelta(days=1)) if i.kind == "DECISION_REPORT"]


class TestDecisionsLocales:
    def test_hors_connexion_surplus_200_w_prix_connu_0_10_charge_locale(self, db, publisher, identity, clock):
        m = manager(db, publisher, identity, clock)
        m.store_policy(POLICY, [{"hour_start": "2026-09-14T08:00:00Z", "eur_per_kwh": 0.10}], T0)
        site(db, T0, 450, 250, 50)
        assert m.tick([1], online=False) == ["EXECUTED"]
        topic, cmd = publisher.sent[-1]
        assert topic == "site4/commands/charge" and cmd["power_w"] == 200 and cmd["source"] == "LOCAL_AUTO", (
            "rôle de production reçu du cloud"
        )
        assert reports(db, clock)[-1]["source"] == "LOCAL_AUTO"
        clock.advance(seconds=10)
        site(db, clock.now(), 460, 250, 50.1)
        assert m.tick([1], online=False) == [], "même action à puissance voisine : pas de nouvelle commande"
        clock.advance(minutes=6)
        site(db, clock.now(), 460, 250, 51)
        assert m.tick([1], online=False) == ["EXECUTED"], (
            "commande locale renouvelée avant la fin de sa durée (5 minutes)"
        )

    def test_sans_prix_pas_de_charge_et_alerte(self, db, publisher, identity, clock):
        alerts: list[str] = []
        m = manager(db, publisher, identity, clock, alerts)
        m.store_policy(POLICY, [{"hour_start": "2026-09-14T06:00:00Z", "eur_per_kwh": 0.05}], T0)
        site(db, T0, 450, 250, 50)
        assert m.tick([1], online=False) == []
        assert not [t for t, _ in publisher.sent if t.endswith("/charge")], (
            "prix de l'heure inconnu : condition de prix non remplie"
        )
        assert "SPOT_PRICE_UNAVAILABLE" in alerts

    def test_pic_hors_connexion_decharge_du_deficit_puis_veille_quand_le_pic_cesse(
        self, db, publisher, identity, clock
    ):
        m = manager(db, publisher, identity, clock)
        m.store_policy(POLICY, [], T0)
        site(db, T0, 0, 420, 60)
        assert m.tick([1], online=False) == ["EXECUTED"]
        assert publisher.sent[-1][0] == "site4/commands/discharge" and publisher.sent[-1][1]["power_w"] == 420, (
            "pic sans prix : décharge"
        )
        clock.advance(seconds=40)
        site(db, clock.now(), 0, 100, 59)
        assert m.tick([1], online=False) == ["EXECUTED"] and publisher.sent[-1][0] == "site4/commands/standby"

    def test_mesures_incompletes_ou_pilotage_non_active_veille(self, db, publisher, identity, clock):
        m = manager(db, publisher, identity, clock)
        m.store_policy(POLICY, [], T0)
        measure(db, "device:8", "power_w", 420, T0)
        measure(db, "stock:1", "soc_pct", 60, T0, "battery")
        assert m.tick([1], online=False) == [], "prise de production muette : aucune décision sur un bilan partiel"
        m.store_policy(dict(POLICY, auto_pilot=False), [], T0)
        site(db, T0, 0, 420, 60)
        assert m.tick([1], online=False) == [], (
            "pilotage automatique non activé pour le site : l'agent ne décide pas seul"
        )

    def test_ordre_echu_jamais_rejoue_decision_locale_a_la_place(self, db, publisher, identity, clock, cloud_keys):
        m = manager(db, publisher, identity, clock)
        m.store_policy(POLICY, [], T0)
        site(db, T0, 0, 500, 60)
        m.receive([cloud_keys.order(T0, "DISCHARGE", 200, duration=600, validity=dt.timedelta(hours=2))])
        assert m.tick([1], online=True) == ["EXECUTED"]
        clock.advance(minutes=11)
        site(db, clock.now(), 0, 100, 55)
        assert m.tick([1], online=False) == ["EXECUTED"]
        with db.tx() as cur:
            cur.execute("SELECT source, action FROM local_decisions ORDER BY decision_id")
            rows = [(r["source"], r["action"]) for r in cur.fetchall()]
        assert ("CLOUD_CACHE", "DISCHARGE") not in rows, "ordre échu (durée écoulée) : jamais rejoué"
        assert rows[-1] == ("LOCAL_AUTO", "STANDBY"), "la règle §5.1 locale prend le relais (pas de pic : veille)"

    def test_decharge_close_avec_energie_mesuree(self, db, publisher, identity, clock, cloud_keys):
        """WP3b-4 : le compte rendu qui clôt une décharge porte l'énergie mesurée (relevés de la batterie), pour la révision du cloud."""
        m = manager(db, publisher, identity, clock)
        measure(db, "stock:1", "soc_pct", 60, T0, "battery")
        order = cloud_keys.order(T0, "DISCHARGE", 500, duration=3600)
        fresh = m.receive([order])
        assert m.tick([1], online=True) == ["EXECUTED"]
        for s in range(0, 600, 10):
            measure(
                db,
                "stock:1",
                "power_discharge_w",
                400,
                T0 + dt.timedelta(seconds=s),
                "battery",
            )
        clock.advance(minutes=10)
        measure(db, "stock:1", "soc_pct", 55, clock.now(), "battery")
        m.receive([cloud_keys.order(clock.now(), "STANDBY", 0, duration=600)])
        assert m.tick([1], online=True) == ["EXECUTED"]
        closes = reports(db, clock)[-1]["closes"]
        assert closes["decision_id"] == fresh[0].decision_id and closes["action"] == "DISCHARGE"
        assert abs(closes["energy_wh"] - 66.667) < 0.01 and closes["coverage"] >= 0.8
        assert closes["ended_at"] == "2026-09-14T08:10:00Z"

    def test_capacite_reservee_pas_de_decharge_locale_sur_un_pic(self, db, publisher, identity, clock):
        """C4 : une fenêtre réservée transmise par le cloud (engagement, offre de flexibilité, ordre RTE) interdit la décharge locale."""
        m = manager(db, publisher, identity, clock)
        reserved = {
            "reserved": [
                {
                    "from": "2026-09-14T07:00:00Z",
                    "to": "2026-09-14T09:00:00Z",
                    "reason": "ENGAGEMENT",
                }
            ],
            "reserved_until": "2026-09-16T08:00:00Z",
        }
        m.store_policy(dict(POLICY, **reserved), [], T0)
        site(db, T0, 0, 420, 60)
        assert m.tick([1], online=False) == [], (
            "capacité réservée à un engagement : la batterie n'est pas vidée sur un pic local"
        )
        assert not [t for t, _ in publisher.sent if t.endswith("/discharge")]

        # Hors de la fenêtre réservée, la décharge locale reprend.
        clock.advance(hours=2)
        site(db, clock.now(), 0, 420, 60)
        assert m.tick([1], online=False) == ["EXECUTED"]
        assert publisher.sent[-1][0] == "site4/commands/discharge"

    def test_sans_information_fraiche_la_capacite_est_tenue_pour_reservee(self, db, publisher, identity, clock):
        """C4 : passé l'horizon sur lequel la liste des fenêtres fait foi, l'agent ne sait plus et s'abstient de décharger."""
        m = manager(db, publisher, identity, clock)
        m.store_policy(dict(POLICY, reserved=[], reserved_until="2026-09-14T09:00:00Z"), [], T0)
        site(db, T0, 0, 420, 60)
        assert m.tick([1], online=False) == ["EXECUTED"], (
            "dans l'horizon connu, aucune fenêtre réservée : décharge du déficit"
        )

        clock.advance(hours=3)
        site(db, clock.now(), 0, 420, 60)
        m.tick([1], online=False)
        assert publisher.sent[-1][0] == "site4/commands/standby", (
            "horizon dépassé : comportement prudent, aucune décharge"
        )

    def test_mode_manuel_du_cloud_tenu_hors_reseau(self, db, publisher, identity, clock):
        """V2 : « FORCER STOP » posé depuis la plateforme (§2.5) puis coupure du lien — l'agent ne reprend pas ses décisions §5.1."""
        m = manager(db, publisher, identity, clock)
        m.store_policy(POLICY, [], T0)
        site(db, T0, 0, 420, 60)
        assert m.tick([1], online=False) == ["EXECUTED"], "avant le mode manuel, la décharge locale du pic a lieu"
        assert publisher.sent[-1][0] == "site4/commands/discharge"

        # Le technicien pose un mode manuel sur la batterie 1 ; la politique suivante le porte, puis le lien tombe.
        m.store_policy(dict(POLICY, manual_override=[1]), [], T0)
        clock.advance(minutes=6)
        site(db, clock.now(), 0, 420, 59)
        assert m.tick([1], online=False) == ["EXECUTED"]
        assert publisher.sent[-1][0] == "site4/commands/standby", "mode manuel en cours : l'action locale s'arrête"
        clock.advance(minutes=6)
        site(db, clock.now(), 0, 420, 58)
        assert m.tick([1], online=False) == [], "et aucune nouvelle décision locale n'est prise sur cette batterie"
        assert not [t for t, _ in publisher.sent[-1:] if t.endswith("/discharge")]

        # Mode manuel levé côté plateforme : l'autonomie locale reprend.
        m.store_policy(POLICY, [], clock.now())
        clock.advance(minutes=1)
        site(db, clock.now(), 0, 420, 58)
        assert m.tick([1], online=False) == ["EXECUTED"]
        assert publisher.sent[-1][0] == "site4/commands/discharge"

    def test_le_compte_rendu_local_porte_la_duree_appliquee(self, db, publisher, identity, clock):
        """C5 : sans la durée, le cloud ne peut pas valoriser une décision prise en local (§5.4)."""
        m = manager(db, publisher, identity, clock)
        m.store_policy(POLICY, [], T0)
        site(db, T0, 0, 420, 60)
        assert m.tick([1], online=False) == ["EXECUTED"]
        report = reports(db, clock)[-1]
        assert report["source"] == "LOCAL_AUTO" and report["action"] == "DISCHARGE"
        assert report["duration_s"] == 300


class TestInstantane:
    def test_live_publisher_emits_every_5s(self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys):
        """E027 (point d'accroche du lot 5) : instantané du site composé des relevés bruts de moins de 10 s, publié toutes les 5 s par le
        transport temps réel ; sans transport, aucune tâche (rien n'est présenté comme instantané)."""
        from h2s_agent.config import AgentConfig, BatteryConf, DeviceConf
        from h2s_agent.orchestrator import Agent, save_identity

        from .conftest import DB_URL, SITE

        class Transport:
            def __init__(self):
                self.sent = []

            def publish_live(self, payload):
                self.sent.append(payload)
                return True

        save_identity(
            db,
            "http://localhost:8088",
            {
                "agent_uuid": "7b0f2c1e-6a3f-4c7b-9d5e-2f1a0b3c4d5e",
                "site_id": SITE,
                "keys": {
                    "decision_key_id": cloud_keys.key_id,
                    "decision_public_key": cloud_keys.public,
                    "gradient_public_key": cloud_keys.rsa_public,
                },
            },
        )
        config = AgentConfig(
            cloud_url="http://localhost:8088",
            db_url=DB_URL,
            data_dir=tmp_path,
            devices=(
                DeviceConf(3, "shelly_gen2", mqtt_id="shellyplug-3", role="PRODUCTION"),
                DeviceConf(8, "h2s_json", topic="site4/conso"),
            ),
            batteries=(BatteryConf(1, "site4/battery/status", "site4/commands"),),
        )
        assert "live" not in [
            t.name for t in Agent(config, db, fake_cloud, publisher, clock, require_signed_package=False).tasks
        ]
        transport = Transport()
        a = Agent(
            config,
            db,
            fake_cloud,
            publisher,
            clock,
            require_signed_package=False,
            live_transport=transport,
        )
        live = next(t for t in a.tasks if t.name == "live")
        assert live.every_s == 5
        site(db, T0 - dt.timedelta(seconds=3), 640, 250, 52)
        measure(db, "device:8", "power_w", 999, T0 - dt.timedelta(seconds=30))
        a.run_once(monotonic=1_000)
        (payload,) = transport.sent
        assert (payload["production_w"], payload["consumption_w"], payload["soc"]) == (
            640,
            250,
            {"1": 52},
        )
        assert payload["measured_at"] == "2026-09-14T07:59:57Z"
        a.run_once(monotonic=1_004)
        assert len(transport.sent) == 1, "pas avant 5 s"
        clock.advance(seconds=20)
        a.run_once(monotonic=1_005)
        assert len(transport.sent) == 1, "relevés de plus de 10 s : aucun instantané publié"


class TestAgentHorsConnexion:
    def test_regles_du_site_et_prix_recus_puis_charge_locale_hors_connexion_rendue_a_la_reconnexion(
        self, db, fake_cloud, publisher, clock, tmp_path, cloud_keys
    ):
        from h2s_agent import state
        from h2s_agent.config import AgentConfig, BatteryConf, DeviceConf
        from h2s_agent.orchestrator import Agent, save_identity

        from .conftest import DB_URL, SITE

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
        config = AgentConfig(
            cloud_url="http://localhost:8088",
            db_url=DB_URL,
            data_dir=tmp_path,
            devices=(
                DeviceConf(3, "shelly_gen2", mqtt_id="shellyplug-3", role="PRODUCTION"),
                DeviceConf(8, "h2s_json", topic="site4/conso"),
            ),
            batteries=(BatteryConf(1, "site4/battery/status", "site4/commands"),),
        )
        fake_cloud.rules_body["decision"] = dict(POLICY, devices=[])
        fake_cloud.rules_body["prices"] = [{"hour_start": "2026-09-14T08:00:00Z", "eur_per_kwh": 0.10}]
        a = Agent(config, db, fake_cloud, publisher, clock, require_signed_package=False)
        a.boot()
        assert a.decisions.price(T0) == 0.10 and a.decisions.policy()["auto_pilot"] is True
        assert a.status()["inference"]["available"] is False, "aucun modèle LightGBM embarqué : dit, pas simulé"
        clock.observe_server_time(
            T0, T0, T0
        )  # horloge recalée par le cloud avant la coupure : l'heure du prix est sûre
        fake_cloud.down = True
        for _ in range(3):
            a.heartbeat()
        assert a.states.state == state.DEGRADED
        site(db, clock.now(), 450, 250, 50)
        a.run_decisions()
        topic, cmd = [(t, c) for t, c in publisher.sent if "/commands/" in t][-1]
        assert topic == "site4/commands/charge" and cmd["source"] == "LOCAL_AUTO" and cmd["power_w"] == 200, (
            "rôle d'agent.json"
        )
        fake_cloud.down = False
        a.heartbeat()
        a.sync()
        assert any(
            r["kind"] == "DECISION_REPORT" and r["payload"]["source"] == "LOCAL_AUTO" for r in fake_cloud.received
        )
