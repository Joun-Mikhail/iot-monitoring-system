"""
Sensor simulator — publishes synthetic MQTT telemetry to AWS IoT Core.

Failure injection flags (all default to 0 / disabled):
  --drop-rate          fraction of messages silently skipped before publish
  --corrupt-rate       fraction of messages sent as malformed JSON
  --latency-jitter     max extra seconds added randomly per message (uniform 0..N)
  --device-failure-duration  if >0, simulator periodically goes silent for this many
                             seconds to simulate a device crash/reboot cycle

The distinction between --latency-jitter and the old --slow-rate:
  --latency-jitter adds a small random delay every message (network jitter model).
  There is no --slow-rate anymore — it was misleading because sleeping 6–10s in the
  publish loop would cause MQTT keepalive to expire on a 60s keepalive, so it wasn't
  actually simulating broker latency, it was simulating a broken client.
"""

import argparse
import json
import logging
import random
import signal
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

TOPIC = "iot/sensors/data"
PUBLISH_INTERVAL = (1, 3)   # seconds between messages
ANOMALY_PROB = 0.12
PUBLISH_TIMEOUT_S = 5       # how long to wait for QoS-1 ack before giving up

# approximate interval between device-failure events (seconds)
_FAILURE_CYCLE_INTERVAL = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_reading(device_id: str, inject_anomaly: bool) -> dict:
    if inject_anomaly:
        kind = random.choice(["crit_temp", "warn_temp", "low_hum", "high_hum"])
        if kind == "crit_temp":
            temp = round(random.uniform(80, 100), 2)
            hum = round(random.uniform(20, 80), 2)
        elif kind == "warn_temp":
            temp = round(random.uniform(60, 79.9), 2)
            hum = round(random.uniform(20, 80), 2)
        elif kind == "low_hum":
            temp = round(random.uniform(20, 55), 2)
            hum = round(random.uniform(0, 9.9), 2)
        else:
            temp = round(random.uniform(20, 55), 2)
            hum = round(random.uniform(95.1, 100), 2)
    else:
        temp = round(random.uniform(18, 58), 2)
        hum = round(random.uniform(10, 95), 2)

    return {"device_id": device_id, "timestamp": _now(), "temperature": temp, "humidity": hum}


def corrupt_payload(payload: dict) -> str:
    kind = random.choice(["truncated", "wrong_type", "missing_brace", "binary_noise"])
    if kind == "truncated":
        s = json.dumps(payload)
        return s[: max(1, len(s) // 2)]
    if kind == "wrong_type":
        bad = dict(payload)
        bad["temperature"] = "NaN"
        bad["humidity"] = None
        return json.dumps(bad)
    if kind == "missing_brace":
        return json.dumps(payload)[1:]
    # binary_noise: embed control characters that break json.loads
    return "\x00\x01" + json.dumps(payload)[:8]


def build_client(endpoint: str, cert: str, key: str, ca: str, client_id: str) -> mqtt.Client:
    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
    client.tls_set(ca_certs=ca, certfile=cert, keyfile=key)

    def on_connect(c, _u, _f, rc):
        if rc == 0:
            log.info("connected to %s", endpoint)
        else:
            log.error("connection refused rc=%d", rc)

    def on_disconnect(c, _u, rc):
        if rc != 0:
            log.warning("unexpected disconnect rc=%d — paho will attempt reconnect", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def _wait_ack(info) -> bool:
    deadline = time.monotonic() + PUBLISH_TIMEOUT_S
    while not info.is_published() and time.monotonic() < deadline:
        time.sleep(0.05)
    return info.is_published()


def run(args):
    client = build_client(args.endpoint, args.cert, args.key, args.ca, args.device_id)

    for attempt in range(1, 11):
        try:
            client.connect(args.endpoint, port=8883, keepalive=60)
            break
        except Exception as exc:
            log.warning("connect attempt %d/10 failed: %s — retry in 5s", attempt, exc)
            time.sleep(5)
    else:
        log.error("could not connect after 10 attempts, exiting")
        sys.exit(1)

    client.loop_start()

    stop = {"flag": False}

    def _shutdown(sig, _frame):
        log.info("signal received, shutting down")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    stats = {"published": 0, "dropped": 0, "corrupted": 0, "jittered": 0, "failed": 0, "silent": 0}
    next_failure_at = (
        time.monotonic() + _FAILURE_CYCLE_INTERVAL
        if args.device_failure_duration > 0
        else float("inf")
    )

    while not stop["flag"]:

        # --- device failure simulation: go silent for a period ---
        now = time.monotonic()
        if now >= next_failure_at:
            stats["silent"] += 1
            silence = args.device_failure_duration
            log.warning(
                "device-failure: going silent for %.0fs (simulates crash/reboot cycle #%d)",
                silence, stats["silent"],
            )
            # sleep in small increments so SIGINT is still responsive
            deadline = now + silence
            while not stop["flag"] and time.monotonic() < deadline:
                time.sleep(0.5)
            next_failure_at = time.monotonic() + _FAILURE_CYCLE_INTERVAL
            log.info("device-failure: resuming after %.0fs silence", silence)
            continue

        # --- normal message cycle ---
        inject_anomaly = random.random() < ANOMALY_PROB
        payload = generate_reading(args.device_id, inject_anomaly)

        if random.random() < args.drop_rate:
            stats["dropped"] += 1
            log.debug("drop: packet discarded before publish (total=%d)", stats["dropped"])
            time.sleep(random.uniform(*PUBLISH_INTERVAL))
            continue

        # latency jitter: small random delay before publish
        if args.latency_jitter > 0:
            jitter = random.uniform(0, args.latency_jitter)
            if jitter > 0.2:
                stats["jittered"] += 1
                log.debug("jitter: %.2fs delay before publish", jitter)
            time.sleep(jitter)

        if random.random() < args.corrupt_rate:
            stats["corrupted"] += 1
            msg = corrupt_payload(payload)
            log.warning("corrupt: sending malformed payload (total=%d)", stats["corrupted"])
        else:
            msg = json.dumps(payload)

        info = client.publish(args.topic, msg, qos=1)
        if _wait_ack(info):
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                stats["published"] += 1
                log.info(
                    "published #%d | device=%s temp=%.2f°C hum=%.2f%% anomaly=%s",
                    stats["published"], payload["device_id"],
                    payload["temperature"], payload["humidity"], inject_anomaly,
                )
            else:
                stats["failed"] += 1
                log.warning("publish error rc=%d", info.rc)
        else:
            stats["failed"] += 1
            log.warning("publish ack timed out after %ds (total failed=%d)", PUBLISH_TIMEOUT_S, stats["failed"])

        time.sleep(random.uniform(*PUBLISH_INTERVAL))

    client.loop_stop()
    client.disconnect()
    log.info(
        "stopped | published=%d dropped=%d corrupted=%d jittered=%d failed=%d silent_cycles=%d",
        stats["published"], stats["dropped"], stats["corrupted"],
        stats["jittered"], stats["failed"], stats["silent"],
    )


def parse_args():
    p = argparse.ArgumentParser(description="IoT sensor simulator with failure injection")
    p.add_argument("--endpoint", required=True, help="AWS IoT Core ATS endpoint")
    p.add_argument("--cert", required=True, help="device certificate (.pem.crt)")
    p.add_argument("--key", required=True, help="private key (.pem.key)")
    p.add_argument("--ca", required=True, help="root CA cert (AmazonRootCA1.pem)")
    p.add_argument("--device-id", default="sensor-01")
    p.add_argument("--topic", default=TOPIC)
    p.add_argument("--drop-rate", type=float, default=0.0, metavar="FRAC",
                   help="fraction of messages to drop before publish [0-1]")
    p.add_argument("--corrupt-rate", type=float, default=0.0, metavar="FRAC",
                   help="fraction of messages to corrupt [0-1]")
    p.add_argument("--latency-jitter", type=float, default=0.0, metavar="SECONDS",
                   help="max extra delay per message in seconds, uniform random [0..N]")
    p.add_argument("--device-failure-duration", type=float, default=0.0, metavar="SECONDS",
                   help="if >0, device goes silent for this many seconds every ~60s")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
