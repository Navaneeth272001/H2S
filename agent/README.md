# Agent local H2S (brique 18)

Programme Python qui tourne sur le boîtier du site (Raspberry Pi). Il collecte les mesures des équipements par MQTT,
les garde dans une base PostgreSQL locale tant que le cloud est injoignable, exécute les ordres de charge et de
décharge **signés** par le cloud après les avoir revalidés contre les règles de sécurité SEC1 à SEC6, et contribue à
l'apprentissage fédéré sans envoyer ses données d'apprentissage (les mesures remontent, elles, agrégées sur 5 minutes et
brutes pour la batterie). Décisions : D247 à D258 (`specs/analyse/DECISIONS.md`).

En ligne, il **exécute** les ordres du cloud. **Hors connexion** (mode dégradé, CDC §2.18), l'ordre en cours va au bout de sa
durée, un ordre échu n'est jamais rejoué, et l'agent **décide lui-même** selon la règle §5.1 (`h2s_agent/local_policy.py`, copie
de la règle du cloud vérifiée par `data/demo/vecteurs_regle_5_1.json`) : mesures de l'instant des équipements (rôle
`consumption` ou `production`, celui du cloud prévaut), SoC mesuré, dernier prix day-ahead connu de l'heure (sans prix, la
condition de prix n'est pas remplie), seuils du site reçus avec les règles ; puis SEC1-6 locales, source `LOCAL_AUTO`, rendue au
cloud à la reconnexion. Sans pilotage automatique activé pour le site, sans bilan complet ou sans SoC, rien n'est décidé et la
batterie repasse en veille (`SAFE_STANDBY`) ; de même quand le service s'arrête. Aucun modèle LightGBM n'est embarqué (modèle du
client, Q-05) : `GET /api/agent/status` le dit (`inference.available: false`).

Chaque commande publiée vers la batterie est signée par la clé d'identité de l'agent (`../deploy/README.md` §4). Vers le cloud
comme vers un broker MQTT en TLS, TLS 1.3 est exigé.

## Installation

```sh
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m h2s_agent migrate --config agent.json      # schéma local E001 à E005 (sommes de contrôle vérifiées)
venv/bin/python -m h2s_agent enroll --config agent.json        # code saisi au clavier, ou --code-file
venv/bin/python -m h2s_agent run --config agent.json
```

Le code d'enrôlement est créé par l'opérateur dans l'écran **Agents locaux** et n'est valable qu'une fois. L'agent y
génère sa clé Ed25519 (`<data_dir>/keys/agent.pem`, 0600) et reçoit la clé publique des ordres de H2S et la clé RSA des
contributions fédérées. Exemple de configuration : `../deploy/agent.example.json`. Déploiement : `../deploy/README.md`.

`cloud_url` doit être en HTTPS, sauf `localhost` en développement. Aucun secret dans `agent.json` : le mot de passe MQTT
est lu dans un fichier désigné par `password_file`.

## Clé d'identité : logicielle ou TPM 2.0

`key_store` d'agent.json choisit le magasin (WP8) :

- `software` (défaut) : Ed25519 dans `<data_dir>/keys/agent.pem` (0600) ; une copie du fichier vaut l'identité.
- `tpm2` : ECDSA P-256 créée **dans** le TPM (poignée persistante 0x81000018, `fixedTPM`, non exportable) par `tpm2-pytss`
  (`requirements-tpm.txt`, `apt install libtss2-dev`), accès par `tpm_tcti` (`device:/dev/tpmrm0`). Chaque requête au cloud
  est signée par le TPM, contributions fédérées comprises. TPM absent ou en erreur : l'agent ne démarre pas (pas de repli
  logiciel). À l'enrôlement, l'agent joint une preuve (certificat EK, AK, `TPM2_Certify`) ; le cloud la vérifie contre les AC
  des fabricants qu'il déclare (`H2S_EDGE_TPM_EK_ROOTS`) et renvoie un défi que seul ce TPM résout : agent « TPM 2.0 attesté ».
  Sans AC déclarée, il reste « TPM déclaré, non attesté ».
- `require_tpm: true` : production chez le client qui l'active ; refuse le magasin logiciel et un TPM simulé (`swtpm`, `mssim`).

`/api/agent/tpm` et le battement de cœur exposent la sonde : `OPERATIONAL`, `ABSENT` ou `ERROR`, fabricant, microcode,
certificat EK présent, et `simulator` pour un TPM logiciel — jamais présenté comme un module matériel.

Tests TPM : contre `swtpm` (`H2S_EDGE_SWTPM` ou PATH) et `tpm2-pytss` importable ; sans eux, ces tests sont ignorés avec leur
motif. `tests/gen_vectors_tpm.py` régénère `tests/vectors_tpm.json`, partagé avec `TpmAttestationTest.java`.

## Intégrité du paquet

Au démarrage **puis toutes les heures**, l'agent vérifie `h2s_agent/MANIFEST.json` avec `package_public_key` d'agent.json :
la clé de **publication** de H2S, distincte de la clé des ordres (D258). Le manifeste couvre chaque fichier du paquet, chaque
fichier des `site-packages` du venv (un `.pth` ajouté compte) et, s'il en impose, les champs de sécurité d'agent.json
(`cloud_url`, `key_store`, `tpm_tcti`, `require_tpm`) ; ces champs sont aussi comparés à leur valeur du démarrage (E176).
Sans manifeste conforme, l'agent collecte et remonte les mesures mais **n'exécute aucun ordre**, manuel compris, lève
`INTEGRITY_FAILED` et, si l'écart apparaît en cours d'exécution, met en veille les batteries sous un ordre. Déblocage :
redémarrage du service sur un paquet conforme. Signer avec le venv de référence de la version (même architecture et même
Python que les boîtiers).
`--allow-unsigned-package` lève ce blocage : développement seulement. Signature, sur le poste de publication :

```sh
venv/bin/python tools/sign_manifest.py --new-key cle-publication.seed   # une fois : affiche package_public_key
venv/bin/python tools/sign_manifest.py --seed-file cle-publication.seed
```

## Preuves locales d'effacement

Toutes les 30 minutes closes, en réseau comme en coupure, l'agent signe de sa clé d'identité (logicielle ou TPM) un relevé
de la période : énergie par équipement tirée des compteurs, SoC de début et de fin des batteries, ordres exécutés et leur
source, nombre et empreinte des relevés bruts, mode dégradé ou non, empreinte du relevé précédent (chaîne). Le relevé est
gardé en base (`local_proofs`, migration E004) et part dans le buffer en priorité 1, jamais évincé ; le cloud vérifie la
signature avec la clé enrôlée, l'agent et le site, et constate la continuité de la chaîne (`edge_local_proofs`, V99). Le
calcul de l'effacement reste au cloud (brique 11). IOTA local : non livré (réseau et SDK à verrouiller).

## API locale

`127.0.0.1:8765`, jeton porteur dans `<data_dir>/keys/local-api.token` (0600) ; `/api/agent/health` seul est libre.
Routes : `status`, `config`, `buffer`, `buffer/flush`, `decisions` (lecture et ordre manuel du technicien, soumis aux
mêmes règles), `override`, `tpm`.

## Tests

```sh
H2S_EDGE_TEST_DB_URL=postgresql://h2s:h2s@127.0.0.1:55433/h2s_edge_test venv/bin/python -m pytest -q tests
```

`tests/vectors.json` est partagé avec `backend/src/test/java/com/h2s/edge/EdgeModelTest.java` : les deux côtés signent
et vérifient les mêmes vecteurs (requête canonique, ordre signé, enveloppe chiffrée, moyenne fédérée).
