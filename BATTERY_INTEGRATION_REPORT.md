# Rapport d'Intégration de la Batterie Physique & Contrôle Indirect (Chemin B)

Ce document décrit l'intégration de la batterie physique dans l'écosystème **H2S Edge** tournant sur la passerelle **Raspberry Pi**, les modifications apportées à l'agent et au collecteur, le format du paquet de données de télémétrie de batterie, ainsi que l'étude de faisabilité du contrôle direct (Chemin A).

---

## 1. Contexte & Hypothèse Physique

### 1.1. Rôle du système cyber-physique
Sans batterie physique, H2S est un logiciel de simulation. Avec une batterie physique, H2S devient un **système cyber-physique** validant la chaîne complète :
$$\text{Détection surplus} \longrightarrow \text{Décision IA} \longrightarrow \text{Commande} \longrightarrow \text{Effet mesuré}$$

### 1.2. Schéma physique d'installation
Par contrainte matérielle sur le banc POC (absence de panneaux photovoltaïques ou d'éoliennes sur site) :
```
[ Réseau Enedis ] ──── (Alimentation sécurisée) ────▶ [ Batterie Jackery ]
                                                             │
                                                             ▼ (Sortie AC)
                                                     [ Prises Shelly EM ]
                                                             │
                                                             ▼
                                                    [ Appareils du site ]
```
- Le **réseau Enedis** est branché sur la batterie afin de sécuriser physiquement un niveau d'énergie suffisant pour tester la boucle.
- La **batterie Jackery** alimente les appareils via ses sorties.
- Les **prises et relais Shelly EM Gen2** sont intercalés entre la batterie et les appareils.
- Le **surplus d'énergie renouvelable est simulé** par le jumeau numérique. Le SoC simulé pilote les décisions énergétiques, et non la recharge directe par Enedis.

---

## 2. Architecture de Pilotage (Chemin B : Contrôle indirect via Shelly EM)

La batterie Jackery n'offrant pas de liaison de commande filaire directe avec le Raspberry Pi, le contrôle de sa charge et décharge s'effectue **indirectement** en pilotant les relais Shelly EM des appareils.

### 2.1. Classification des équipements

| Device ID | Équipement | Catégorie | Rôle lors du pilotage |
|---|---|---|---|
| **2001** | TV Salle à manger | **Prioritaire** | Allumé en `DISCHARGE` (consomme sur la batterie) |
| **2002** | Écran | **Prioritaire** | Allumé en `DISCHARGE` |
| **2003** | Téléphone / Tablette | **Prioritaire** | Allumé en `DISCHARGE` |
| **2006** | PC Portable | **Prioritaire** | Allumé en `DISCHARGE` |
| **2004** | Cafetière | **Non critique** | Éteint en `CHARGE` (baisse la charge pour absorber le surplus) |
| **2005** | Lampe Salle à manger | **Non critique** | Éteint en `CHARGE` |
| **2007** | Lampe Cuisine | **Non critique** | Éteint en `CHARGE` |
| **2008** | Lampe Chambre | **Non critique** | Éteint en `CHARGE` |

### 2.2. Matrice Décision $\rightarrow$ Action

| Décision IA | Action sur les relais Shelly | Effet sur les appareils | Effet sur la batterie |
|---|---|---|---|
| **`DISCHARGE`** | Allumage des relais prioritaires | Appareils prioritaires alimentés | La batterie se décharge pour fournir la puissance appelée |
| **`CHARGE`** | Coupure des relais non critiques | Consommation du site réduite | Simule l'absorption du surplus par la batterie |
| **`STANDBY`** | Maintien de l'état actuel | Aucun changement d'état | Puissance active nulle, maintien de charge |

---

## 3. Spécification du Paquet de Données (Battery Telemetry)

Les mesures de batterie sont systématiquement ajoutées aux paquets de données envoyés au cloud et au broker local. Si une lecture physique est disponible via Jackery/BMS, elle est utilisée ; sinon, la simulation physique dynamique la génère automatiquement.

### 3.1. Grandeurs de batterie transmises

| Grandeur | Unité | Capteur ID | Description |
|---|---|---|---|
| `soc_pct` | `%` | `battery-1-soc` | État de charge de la batterie (State of Charge, 0–100%) |
| `power_charge_w` | `W` | `battery-1-charge-power` | Puissance active de charge |
| `power_discharge_w` | `W` | `battery-1-discharge-power` | Puissance active de décharge (somme de la charge active appelée) |
| `voltage_v` | `V` | `battery-1-voltage` | Tension nominale / onduleur (230 V avec fléchissement sous charge) |
| `current_a` | `A` | `battery-1-current` | Courant calculé ($I = P / U$) |
| `temperature_c` | `°C` | `battery-1-temperature` | Température interne du pack batterie |

### 3.2. Format du paquet MQTT

#### A. Format Télémétrie Cloud (`h2s/agents/<uuid>/telemetrie`)
```json
{
  "timestamp": "2026-10-14T21:00:00Z",
  "valeur": 78.4,
  "unite": "%",
  "capteur_id": "battery-1-soc"
}
```

#### B. Format État Batterie Local (`h2s/site4/battery/status`) pour `h2s_agent`
```json
{
  "timestamp": "2026-10-14T21:00:00Z",
  "stock_id": 1,
  "soc_pct": 78.4,
  "power_charge_w": 0.0,
  "power_discharge_w": 285.5,
  "voltage_v": 229.4,
  "current_a": 1.24,
  "temperature_c": 23.6
}
```

---

## 4. Livrables et Modules Réalisés

### 4.1. Module de gestion & simulation batterie
- **Fichier** : [`H2S_Shelly_RPi/src/battery.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/H2S_Shelly_RPi/src/battery.py)
- Implémente `BatteryManager` :
  - Intégration physique ou dynamique de la batterie Jackery.
  - Calcul dynamique du SoC à chaque pas de temps ($dt = 60\text{ s}$).
  - Calcul de la puissance de décharge à partir des consommations réelles mesurées sur les Shelly prioritaires.
  - Modèle électrique (chute de tension $U = U_0 - \Delta U(P)$) et modèle thermique.

### 4.2. Module de pilotage des relais Shelly EM
- **Fichier** : [`H2S_Shelly_RPi/src/shelly_control.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/H2S_Shelly_RPi/src/shelly_control.py)
- **Fichier agent** : [`agent/h2s_agent/shelly_relay.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/agent/h2s_agent/shelly_relay.py)
- Implémente l'API JSON-RPC locale `/rpc/Switch.Set?id=0&on=true|false`.
- Exécute `execute_battery_decision(session, action)` traduisant `CHARGE`, `DISCHARGE`, `STANDBY` en bascule des relais.

### 4.3. Intégration dans le collecteur principal et base de données
- **Fichier** : [`H2S_Shelly_RPi/main_collector.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/H2S_Shelly_RPi/main_collector.py)
- **Fichier** : [`H2S_Shelly_RPi/src/database.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/H2S_Shelly_RPi/src/database.py)
- Nouvelle table `BatteryMinute` stockant les mesures de batterie minute par minute dans PostgreSQL.
- Abonnement aux ordres et traduction automatique en bascule physique.

### 4.4. Script de test de la boucle complète
- **Fichier** : [`H2S_Shelly_RPi/test_battery_loop.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/H2S_Shelly_RPi/test_battery_loop.py)
- **Fichier** : [`agent/tests/test_battery_physical_loop.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/agent/tests/test_battery_physical_loop.py)
- 5 scénarios de test validant :
  1. Dynamique de simulation (charge/décharge/veille).
  2. Formatage des paquets de télémétrie.
  3. Compatibilité avec le parseur de l'agent `h2s_agent` (`parsers.h2s_battery`).
  4. Traduction des ordres en commandes de relais prioritaires/non-critiques.
  5. Pilote API Jackery direct.

---

## 5. Étude de Faisabilité : Chemin A (Contrôle Direct Jackery via API / socketry)

### 5.1. Analyse de la méthode envisagée
Le Chemin A vise à commander directement l'onduleur ou l'état de charge de la station Jackery (ex. Explorer 1000/2000 Pro/Plus) via une bibliothèque Python telle que `socketry` ou par requête vers l'API Cloud Jackery.

### 5.2. Étapes de validation technique
1. **Bibliothèque `socketry`** :
   - Basée sur le protocole IoT mobile Jackery (MQTT TLS / HTTPS vers les serveurs cloud Jackery).
   - Dépendance d'installation : `pip install socketry` ou `uv tool install 'socketry[cli]'`.
   - Commandes disponibles :
     ```bash
     socketry login --email user@example.com --password 'secret'
     socketry get battery      # Retourne le SoC en %
     socketry get ac-power     # Retourne la puissance AC en W
     socketry set ac on        # Active la sortie 230V
     socketry set ac off       # Coupe la sortie 230V
     socketry set battery-protection eco
     ```

2. **Évaluation des limites pour la production** :
   - **Absence d'API locale officielle** : Les stations Jackery grand public ne disposent pas d'API REST locale documentée ni de port Modbus ouvert sur le réseau local. Toute commande transite obligatoirement par le cloud Jackery.
   - **Latence et dépendance Internet** : En cas de coupure de connexion internet, le Raspberry Pi ne peut plus envoyer d'ordre `set ac` à la Jackery.
   - **Non-officialité** : Les identifiants API ou jetons peuvent être révoqués lors d'une mise à jour du firmware Jackery.

### 5.3. Module d'intégration Chemin A implémenté
- **Fichier** : [`H2S_Shelly_RPi/src/jackery_api.py`](file:///Users/navaneethakrishnan/NK/projects/H2S/H2S/H2S_Shelly_RPi/src/jackery_api.py)
- Fournit la classe `JackeryClient` prête à être activée dès configuration de `JACKERY_EMAIL` et `JACKERY_PASSWORD`.
- Recommandation opérationnelle : **Déployer immédiatement le Chemin B** (100% local sur le Raspberry Pi, résilient aux coupures internet) tout en conservant le module `jackery_api.py` pour basculer sur le Chemin A dès validation des identifiants cloud.
