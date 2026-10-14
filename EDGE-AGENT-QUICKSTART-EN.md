# H2S Edge Agent — Quickstart (Raspberry Pi)

The `h2s_agent` is a Python package that runs **on your Pi**. It is not a cloud service and not
a precompiled binary. It collects sensor readings from the Pi's **local** Mosquitto broker,
buffers them in a local PostgreSQL, signs every cloud-bound message (Ed25519, or TPM 2.0 when
enabled) and publishes them to the H2S cloud MQTT broker over TLS 1.3.

```
Shelly / sensors ──MQTT──▶ Mosquitto (local, on the Pi)
                              │  h2s_agent subscribes here
                              ▼
                     h2s_agent (collect → buffer → sign)
                              │ MQTT / TLS 1.3
                              ▼
                     EMQX cloud broker: mqtt.dev.h2splug.com:8883
```

Raw JSON published directly to the cloud broker is **rejected by design** — only signed
envelopes from enrolled agents are accepted.

## 1. Prerequisites

- Raspberry Pi OS 64-bit, Python 3.10+, internet egress on TCP 8883 only.
- PostgreSQL 16 local: `apt install postgresql-16` then
  `sudo -u postgres createuser h2s_agent && sudo -u postgres createdb -O h2s_agent h2s_agent`
- Mosquitto local broker for your collectors (`deploy/mosquitto.conf` + `mosquitto.acl`
  provided as a starting point — set `tls_version tlsv1.3`).
- **NTP/chrony running** — messages outside a ±5 min window are rejected.

## 2. Install

```sh
tar -xzf h2s-agent-edge.tar.gz && cd edge
python3 -m venv venv && venv/bin/pip install -r agent/requirements.txt
cp deploy/agent.example.json /etc/h2s-agent/agent.json   # edit: devices, topics, dirs
```

Key settings in `agent.json`:

- `"key_store": "software"` — file-based Ed25519 key, **use this for now**
  (real TPM later: `pip install -r agent/requirements-tpm.txt` and `"key_store": "tpm2"`,
  same flow, the agent manages provisioning internally — no persistent handle to pass).
- `cloud_mqtt.host` / `port` — already templated for `mqtt.dev.h2splug.com:8883`.
- `password_file` — path to a `0600` file holding the MQTT password (never inline).

## 3. Enroll (once per box)

We send you a **one-time enrollment code** (minted by our platform operator for your site).
Then:

```sh
venv/bin/python -m h2s_agent migrate --config /etc/h2s-agent/agent.json
venv/bin/python -m h2s_agent enroll --config /etc/h2s-agent/agent.json   # enter the code
venv/bin/python -m h2s_agent run --config /etc/h2s-agent/agent.json
```

At `enroll` the agent **generates its own Ed25519 identity key and a new agent UUID** and
registers both with the cloud — do **not** submit a public key yourself (we use Ed25519, not
P-256). After enrollment we provision the MQTT account for the new UUID on our side.

A hardened systemd unit is provided: `deploy/h2s-agent.service`.

## 4. What changes for your collector

Keep your Shelly/weather collector publishing to the **local** Mosquitto — the agent subscribes
to the configured topics, aggregates, signs and forwards. Nothing else changes in your code;
remove only the direct publish to `mqtt.dev.h2splug.com`.

## 5. Wire format (reference)

Envelope on `h2s/agents/<uuid>/telemetrie|comptes-rendus|etat`, QoS 1, `retain=false`:

```json
{"v":1,"agent":"<uuid>","ts":1758000000,"nonce":"<32 hex>","payload":"<exact JSON string>","sig":"<base64url Ed25519>"}
```

Orders arrive on `h2s/agents/<uuid>/ordres` (signed by the cloud). The agent's ACL only ever
sees its own subtree. Full protocol details: `ONBOARDING.md` (FR) — ask us for details anytime.

## 6. Notes

- Without a signed package manifest the agent collects and sends telemetry normally but
  refuses remote commands (`INTEGRITY_FAILED`) — expected on a test box; for dev runs you may
  add `--allow-unsigned-package`.
- Test vector / spec reference: `agent/README.md`, `deploy/README.md` (French — ping us if you
  need anything translated).
