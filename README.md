# IoT Sensor Monitoring System

**Status:** prototype — not production-hardened  
**Region:** eu-central-1  
**Validated:** unit tests pass; LocalStack integration available; real AWS requires manual deploy

---

## What this is and what it isn't

This is an end-to-end IoT telemetry pipeline prototype. Sensors publish temperature and
humidity over MQTT/TLS. AWS IoT Core forwards messages via rule engine to a Lambda function
that validates, classifies, and emits structured logs and CloudWatch metrics.

It is NOT:
- Multi-tenant (one IoT policy, one topic, one Lambda)
- HA (no retry queue, no DLQ)
- Observable at the MQTT broker layer (IoT Core metrics are coarse)
- Proven under load beyond the simulator's 1–3 second rate

If you're evaluating this for production use: read the limitations section before
making any architectural decisions based on it.

---

## System architecture
<img width="1353" height="1162" alt="9763496d-044d-4795-9e42-0c37110de822" src="https://github.com/user-attachments/assets/a5e5a1bb-8580-4626-a6b3-32a4767f1b57" />

```
[simulator/publisher.py]
        │  MQTT/TLS port 8883, QoS 1
        │  client_id must match Thing name (policy enforces this)
        ▼
[AWS IoT Core — Device Gateway]
        │  X.509 certificate auth
        │  Policy: connect as thing name, publish to iot/sensors/data only
        │
        │  IoT Rule: SELECT * FROM 'iot/sensors/data'
        ▼
[Lambda: process_sensor_data]
        │  imports from core/ (bundled at build time, no layer)
        │  validate_payload() → classify() → emit metrics + log
        ▼
[CloudWatch Logs]        [CloudWatch Metrics — namespace: IoTSensors]
                                    │
                          Metrics emitted per invocation:
                          - normal_count (device_id dimension)
                          - anomaly_count (device_id dimension)
                          - validation_error_count (device_id dimension)
                          - processing_duration_ms (device_id dimension)
                                    │
                         [CloudWatch Alarm: anomaly_count >= 3 in 5 min]
                                    │
                              [SNS → email]
```

**Dependency direction** (one-way, no cycles):

```
simulator  →  AWS IoT Core  →  Lambda  →  core/  →  CloudWatch
                                              ↑
                                  (single source of truth)
```

`core/anomaly.py` and `core/validator.py` are the only place where business logic lives.
Lambda imports from them. The simulator does not import from core — it generates payloads
independently and relies on Lambda to reject bad ones.

---

## Data contract

All MQTT messages must follow this schema exactly:

```json
{
  "device_id": "string (non-empty, must match IoT Thing name)",
  "timestamp": "ISO-8601 string (e.g. 2024-06-07T12:00:00+00:00)",
  "temperature": "float (°C, no enforced range — anomaly thresholds applied in Lambda)",
  "humidity": "float (0–100 inclusive)"
}
```

Extra fields from IoT Rule's `SELECT *` (e.g. `clientid`, `timestamp` injected by IoT Core)
pass through validation without error.

---

## Anomaly classification

Implemented in `core/anomaly.py`. Temperature takes priority over humidity.

| Condition | Status |
|---|---|
| temperature >= 80°C | CRITICAL |
| 60°C <= temperature < 80°C | WARNING |
| humidity < 10% | CRITICAL |
| humidity > 95% | CRITICAL |
| otherwise | NORMAL |

These are hard-coded thresholds. There is no per-device configuration, no moving average,
no trend detection. A reading of 79.9°C is WARNING; 80.0°C is CRITICAL. This is intentional
for simplicity and testability. If you need drift detection or per-device thresholds, this
classifier is not the right foundation.

---

## AWS setup

### Prerequisites

- AWS CLI configured for eu-central-1
- IAM permissions: IoT Core, Lambda (create/update), CloudWatch (put metric alarm,
  put dashboard), IAM (role/policy management)
- Python 3.11

### 1. IoT Thing and certificate

```bash
mkdir -p certs

aws iot create-thing --thing-name sensor-01 --region eu-central-1

aws iot create-keys-and-certificate \
  --set-as-active \
  --certificate-pem-outfile certs/device.pem.crt \
  --public-key-outfile certs/public.pem.key \
  --private-key-outfile certs/private.pem.key \
  --region eu-central-1
# note the certificateArn in the output

curl -o certs/AmazonRootCA1.pem \
  https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

### 2. IoT policy

Replace `ACCOUNT_ID` in `aws/iot_policy.json`, then:

```bash
aws iot create-policy \
  --policy-name SensorPublishPolicy \
  --policy-document file://aws/iot_policy.json \
  --region eu-central-1

aws iot attach-policy \
  --policy-name SensorPublishPolicy \
  --target <certificateArn>

aws iot attach-thing-principal \
  --thing-name sensor-01 \
  --principal <certificateArn>
```

### 3. Lambda execution role

```bash
cat > /tmp/lambda-trust.json << 'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

aws iam create-role \
  --role-name lambda-iot-role \
  --assume-role-policy-document file:///tmp/lambda-trust.json

# replace ACCOUNT_ID in aws/lambda_iam_policy.json first
aws iam put-role-policy \
  --role-name lambda-iot-role \
  --policy-name iot-sensor-policy \
  --policy-document file://aws/lambda_iam_policy.json
```

### 4. Build and deploy Lambda

```bash
# installs deps (if any) + bundles core/ alongside handler.py
bash scripts/build_lambda.sh

aws lambda create-function \
  --function-name process_sensor_data \
  --runtime python3.11 \
  --handler handler.handler \
  --zip-file fileb://dist/function.zip \
  --role arn:aws:iam::ACCOUNT_ID:role/lambda-iot-role \
  --region eu-central-1

# for updates:
aws lambda update-function-code \
  --function-name process_sensor_data \
  --zip-file fileb://dist/function.zip \
  --region eu-central-1
```

### 5. IoT Rule

```bash
aws iot create-topic-rule \
  --rule-name SensorToLambda \
  --topic-rule-payload '{
    "sql": "SELECT * FROM '"'"'iot/sensors/data'"'"'",
    "actions": [{"lambda":{"functionArn":"arn:aws:lambda:eu-central-1:ACCOUNT_ID:function:process_sensor_data"}}],
    "ruleDisabled": false,
    "awsIotSqlVersion": "2016-03-23"
  }' \
  --region eu-central-1

aws lambda add-permission \
  --function-name process_sensor_data \
  --statement-id iot-rule-invoke \
  --action lambda:InvokeFunction \
  --principal iot.amazonaws.com \
  --source-arn arn:aws:iot:eu-central-1:ACCOUNT_ID:rule/SensorToLambda \
  --region eu-central-1
```

### 6. CloudWatch alarm, dashboard, SNS

```bash
aws sns create-topic --name iot-alerts --region eu-central-1

aws sns subscribe \
  --topic-arn arn:aws:sns:eu-central-1:ACCOUNT_ID:iot-alerts \
  --protocol email \
  --notification-endpoint you@example.com \
  --region eu-central-1
# confirm the subscription email before the alarm will actually fire

aws cloudwatch put-metric-alarm \
  --alarm-name IoT-AnomalyAlert \
  --metric-name anomaly_count \
  --namespace IoTSensors \
  --statistic Sum \
  --period 300 \
  --threshold 3 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --alarm-actions arn:aws:sns:eu-central-1:ACCOUNT_ID:iot-alerts \
  --region eu-central-1

# replace ACCOUNT_ID in aws/cloudwatch_dashboard.json first
aws cloudwatch put-dashboard \
  --dashboard-name IoTSensors \
  --dashboard-body file://aws/cloudwatch_dashboard.json \
  --region eu-central-1
```

Get your IoT endpoint:
```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS --region eu-central-1
```

---

## Running the simulator

```bash
pip install -r requirements.txt

# baseline — no failure injection
python simulator/publisher.py \
  --endpoint YOUR_ENDPOINT.iot.eu-central-1.amazonaws.com \
  --cert certs/device.pem.crt \
  --key certs/private.pem.key \
  --ca certs/AmazonRootCA1.pem \
  --device-id sensor-01

# with failure injection
python simulator/publisher.py \
  --endpoint YOUR_ENDPOINT.iot.eu-central-1.amazonaws.com \
  --cert certs/device.pem.crt \
  --key certs/private.pem.key \
  --ca certs/AmazonRootCA1.pem \
  --device-id sensor-01 \
  --drop-rate 0.05 \
  --corrupt-rate 0.03 \
  --latency-jitter 2.0 \
  --device-failure-duration 15
```

Failure injection flags:

| Flag | What it simulates | Notes |
|---|---|---|
| `--drop-rate 0.05` | 5% of messages silently dropped | Client-side only; not broker-side |
| `--corrupt-rate 0.03` | 3% of messages sent as malformed JSON | Lambda logs validation error, emits metric |
| `--latency-jitter 2.0` | Up to 2s random delay per message | Uniform distribution 0..N |
| `--device-failure-duration 15` | Device goes silent for 15s every ~60s | Simulates crash/reboot cycle |

Expected output with failure flags:
```
2024-06-07T12:00:01 [INFO] connected to xxx.iot.eu-central-1.amazonaws.com
2024-06-07T12:00:01 [INFO] published #1 | device=sensor-01 temp=24.55°C hum=61.20% anomaly=False
2024-06-07T12:00:04 [WARNING] corrupt: sending malformed payload (total=1)
2024-06-07T12:00:07 [DEBUG] drop: packet discarded before publish (total=1)
2024-06-07T12:00:09 [INFO] published #2 | device=sensor-01 temp=83.10°C hum=45.00% anomaly=True
2024-06-07T12:01:02 [WARNING] device-failure: going silent for 15s (simulates crash/reboot cycle #1)
2024-06-07T12:01:17 [INFO] device-failure: resuming after 15s silence
```

---

## Running tests

```bash
pip install pytest boto3

# unit tests — no AWS required, runs in ~1s
pytest tests/ --ignore=tests/test_integration_localstack.py --ignore=tests/test_aws_smoke.py -v

# LocalStack integration tests
docker run --rm -p 4566:4566 -e SERVICES=lambda,logs,cloudwatch localstack/localstack
LOCALSTACK_ENDPOINT=http://localhost:4566 pytest tests/test_integration_localstack.py -v -m integration

# real AWS smoke tests (requires deployed infra)
IOT_ENDPOINT=xxx.iot.eu-central-1.amazonaws.com \
AWS_PROFILE=your-profile \
pytest tests/test_aws_smoke.py -v -m aws_live
```

Test suite breakdown:

| File | What it covers | Requires |
|---|---|---|
| `test_anomaly.py` | threshold boundaries, priority ordering | nothing |
| `test_validator.py` | field presence, types, range checks | nothing |
| `test_lambda_handler.py` | handler flow, metric routing, non-fatal CW failure | mocked boto3 |
| `test_failure_simulation.py` | corrupt payloads, edge cases, timeout behaviour | mocked boto3 |
| `test_integration_localstack.py` | Lambda deploy + invoke + log assertions | LocalStack |
| `test_aws_smoke.py` | real infra existence, port reachability, live invocation | real AWS |

One test is explicitly marked `@pytest.mark.flaky`:
`test_slow_cloudwatch_does_not_block_response` uses a wall-clock assertion that can
spuriously fail under load on slow machines. It tests absence of a retry loop, not
absolute latency — re-run once before treating it as a real failure.

---

## Verifying Lambda logs

```bash
aws logs tail /aws/lambda/process_sensor_data --follow --region eu-central-1
```

Normal record:
```json
{"device_id": "sensor-01", "timestamp": "2024-06-07T12:00:01+00:00", "temperature": 24.55, "humidity": 61.2, "status": "NORMAL", "reason": "all readings within bounds"}
```

Anomaly — also emits alert line:
```
{"device_id": "sensor-01", "timestamp": "2024-06-07T12:00:06+00:00", "temperature": 83.1, "humidity": 45.0, "status": "CRITICAL", "reason": "temperature=83.1°C exceeds critical threshold (80°C)"}
ALERT TRIGGERED
Device: sensor-01
Metric: anomaly detected
Value: temp=83.10°C hum=45.00%
Timestamp: 2024-06-07T12:00:06+00:00
Severity: CRITICAL
```

Validation failure (from corrupted payload):
```
ERROR validation failed: temperature must be numeric, got str | payload={"device_id": "sensor-01", ..., "temperature": "NaN"}
```

---

## Design tradeoffs

**Why no Lambda layer for `core/`**  
Layers add an indirection: you can redeploy `handler.py` and forget to bump the layer,
running stale logic silently. `build_lambda.sh` copies `core/` into the zip at build time.
The zip is slightly larger; the behaviour is explicit. If the team grows and layers become
necessary for size reasons, that's the time to introduce them — not now.

**Why QoS 1 not QoS 2**  
QoS 2 needs a four-way handshake. Lambda is idempotent on duplicates (same `device_id` +
`timestamp` gives the same classification). The at-most-twice delivery risk of QoS 1 is
acceptable here and saves ~2× the broker overhead per message.

**Why direct `put_metric_data` instead of log-based metric filters**  
Metric filters depend on log format stability. A log format change silently breaks the
metric. Direct `put_metric_data` makes the metric contract explicit in code. The tradeoff
is two failure surfaces (log emission + metric emission) instead of one, which is why
metric failure is non-fatal.

**Why deterministic thresholds instead of ML anomaly detection**  
Threshold rules are auditable, testable, and deployable without a model pipeline. The
business requirement is "alert on obviously bad readings." A z-score or EWMA approach
would catch subtle drift but adds model hosting, drift detection, and retraining overhead
that isn't justified for this prototype.

---

## Known limitations

**No deduplication**  
IoT Core QoS 1 can deliver a message more than once. Lambda processes duplicates as
independent events. Anomaly metrics may overcount during broker reconnects.

**CloudWatch metric resolution floor is 1 minute**  
The 5-minute alarm window can miss a burst that arrives and clears within 60 seconds.
Burst anomaly detection would require streaming into Kinesis Data Streams.

**No backpressure from Lambda**  
If Lambda throttles, IoT Core buffers and delivers the queue on unthrottle. This appears
in metrics as a spike. The alarm threshold (3 in 5 min) was not calibrated against this
scenario and may false-positive.

**Single device policy**  
The IoT policy client ID constraint is `${iot:Connection.Thing.ThingName}`. The
`--device-id` argument must exactly match the Thing name or IoT Core rejects the
connection. This is security-correct but catches out anyone testing locally with a
different device ID.

**`processing_duration_ms` is 0.0 on fast hardware**  
`time.monotonic()` on Windows has ~15ms resolution. If classify + validate completes
within one timer tick, the metric emits `0.0`. This is a measurement limitation, not
a bug. The metric is still useful at the p95/p99 level across many invocations.

**Simulator failure flags are client-side only**  
`--drop-rate` discards before publish; `--corrupt-rate` corrupts the payload locally.
Neither simulates broker-side drops, IoT Rule engine failures, or Lambda cold starts.
For those, use AWS Fault Injection Simulator or manually disable the IoT Rule.

**No log retention policy**  
Default CloudWatch Logs retention is indefinite. Set a retention policy on
`/aws/lambda/process_sensor_data` or storage costs will accumulate silently.

---

## Failure modes and debugging playbook

### Messages publish but Lambda is never invoked

Most common cause: IoT Rule exists but the `lambda:InvokeFunction` resource-based
policy is missing. IoT Core silently drops invocations if it lacks permission.

```bash
# verify rule exists and is enabled
aws iot get-topic-rule --rule-name SensorToLambda

# verify Lambda resource policy
aws lambda get-policy --function-name process_sensor_data \
  | python3 -m json.tool

# look for a Statement with Principal iot.amazonaws.com
# if missing, re-run the aws lambda add-permission step from the setup
```

### MQTT connection refused or certificate error

```bash
# confirm cert is active
aws iot describe-certificate --certificate-id <cert-id> --region eu-central-1

# confirm policy is attached
aws iot list-attached-policies --target <certificateArn>

# test port reachability (rules out local firewall)
python3 -c "import socket; socket.create_connection(('YOUR_ENDPOINT', 8883), 5)"
```

The `--device-id` argument must match the IoT Thing name exactly. The policy uses
`${iot:Connection.Thing.ThingName}` as the client ID constraint.

### ValidationError in Lambda logs for messages I sent

IoT Rule `SELECT *` injects extra fields for some rule configurations. These pass
through validation. The most common cause of actual validation errors is a device
publishing with a wrong field name (`temp` instead of `temperature`) or a firmware
version that serializes floats as strings.

Check the raw payload in the Lambda error log line:
```
ERROR validation failed: <reason> | payload=<raw>
```

### Lambda `ImportError: No module named 'core'`

The function was deployed from `lambda/handler.py` alone, without running
`build_lambda.sh` first. The build script copies `core/` into the zip. Rebuild:

```bash
bash scripts/build_lambda.sh
aws lambda update-function-code \
  --function-name process_sensor_data \
  --zip-file fileb://dist/function.zip
```

### CloudWatch alarm never fires even though anomalies are classified

1. Confirm the SNS subscription email was confirmed (unconfirmed = alarm fires, SNS delivers, email never arrives)
2. CloudWatch takes up to 2 minutes to reflect new PutMetricData calls
3. Alarm threshold is 3 anomalies in 5 minutes — use the IoT Core test client to inject
   3 known-anomaly payloads in quick succession to trigger it without waiting for the
   simulator's natural anomaly rate

### Lambda `FunctionError` in smoke tests but no traceback in CW logs

Lambda executed but the function process crashed before logging. Usually means:
- Python syntax error in a file that was changed after the last deploy
- Missing import (`core/` not included in zip — see `ImportError` above)

Check the raw invoke response:
```bash
aws lambda invoke \
  --function-name process_sensor_data \
  --payload '{"device_id":"test","timestamp":"2024-01-01T00:00:00+00:00","temperature":25.0,"humidity":50.0}' \
  --log-type Tail \
  response.json \
  --region eu-central-1 \
  | python3 -c "import sys,json,base64; r=json.load(sys.stdin); print(base64.b64decode(r.get('LogResult','')).decode())"
```

---

## Operational notes

- Set CloudWatch Logs retention: `aws logs put-retention-policy --log-group-name /aws/lambda/process_sensor_data --retention-in-days 30`
- The IoT policy is per-account (`ACCOUNT_ID` hardcoded). Adding a new device requires updating the policy resource ARN or switching to `iot:Connection.Thing.ThingName` everywhere.
- The SNS alarm threshold (3 anomalies in 5 min) was chosen arbitrarily. Calibrate it based on observed baseline anomaly rate from your actual devices before enabling the SNS email subscription.
