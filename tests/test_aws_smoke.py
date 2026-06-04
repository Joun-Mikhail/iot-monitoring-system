"""
AWS live smoke tests.

These tests run against real AWS infrastructure. They are NOT run in CI by default.
They exist to validate that the deployed system is wired correctly — something
LocalStack cannot fully verify because it doesn't enforce IAM, certificate auth,
or actual MQTT broker behaviour.

Run manually after deploying:
    AWS_PROFILE=your-profile pytest tests/test_aws_smoke.py -v -m aws_live

Requirements:
    - AWS credentials configured (profile or env vars)
    - Infrastructure deployed (IoT Thing, Lambda, CloudWatch)
    - Set env vars:
        IOT_ENDPOINT      — from: aws iot describe-endpoint --endpoint-type iot:Data-ATS
        LAMBDA_FUNCTION   — default: process_sensor_data
        AWS_REGION        — default: eu-central-1

What these tests verify that LocalStack cannot:
  - IoT Core endpoint is reachable on port 8883
  - Lambda function exists and is in Active state
  - Lambda execution role has permission to call cloudwatch:PutMetricData
  - A direct Lambda invocation produces a structured result (not an IAM error)
  - CloudWatch namespace IoTSensors exists (at least one metric datapoint present)
    after a test invocation

What these tests deliberately do NOT do:
  - Publish via MQTT (requires cert files; not appropriate for automated smoke test)
  - Assert on specific metric values (race condition with 1-minute CloudWatch resolution)
  - Trigger SNS (would send real email)
"""

import json
import os
import socket
import time

import pytest

pytestmark = pytest.mark.aws_live

REGION = os.environ.get("AWS_REGION", "eu-central-1")
LAMBDA_FUNCTION = os.environ.get("LAMBDA_FUNCTION", "process_sensor_data")
IOT_ENDPOINT = os.environ.get("IOT_ENDPOINT", "")


def _aws_credentials_present() -> bool:
    try:
        import boto3
        sts = boto3.client("sts", region_name=REGION)
        sts.get_caller_identity()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_aws():
    if not _aws_credentials_present():
        pytest.skip("no valid AWS credentials — set AWS_PROFILE or AWS_ACCESS_KEY_ID")


@pytest.fixture(scope="session")
def lambda_client():
    import boto3
    return boto3.client("lambda", region_name=REGION)


@pytest.fixture(scope="session")
def cw_client():
    import boto3
    return boto3.client("cloudwatch", region_name=REGION)


@pytest.fixture(scope="session")
def iot_client():
    import boto3
    return boto3.client("iot", region_name=REGION)


# ---------------------------------------------------------------------------
# infrastructure existence checks
# ---------------------------------------------------------------------------

class TestInfrastructureExists:
    def test_lambda_function_exists(self, lambda_client):
        resp = lambda_client.get_function(FunctionName=LAMBDA_FUNCTION)
        assert resp["Configuration"]["FunctionName"] == LAMBDA_FUNCTION

    def test_lambda_function_is_active(self, lambda_client):
        cfg = lambda_client.get_function_configuration(FunctionName=LAMBDA_FUNCTION)
        state = cfg.get("State", "Unknown")
        assert state == "Active", f"Lambda state is {state!r} — check recent deploy logs"

    def test_lambda_runtime_is_python311(self, lambda_client):
        cfg = lambda_client.get_function_configuration(FunctionName=LAMBDA_FUNCTION)
        assert cfg["Runtime"] == "python3.11", \
            f"unexpected runtime: {cfg['Runtime']} — redeploy with python3.11"

    def test_iot_rule_exists(self, iot_client):
        try:
            resp = iot_client.get_topic_rule(ruleName="SensorToLambda")
            assert resp["rule"]["ruleName"] == "SensorToLambda"
        except iot_client.exceptions.ResourceNotFoundException:
            pytest.fail("IoT Rule 'SensorToLambda' not found — run aws iot create-topic-rule")

    def test_iot_rule_is_enabled(self, iot_client):
        resp = iot_client.get_topic_rule(ruleName="SensorToLambda")
        assert not resp["rule"]["ruleDisabled"], "IoT Rule is disabled — re-enable it"


# ---------------------------------------------------------------------------
# IoT endpoint reachability
# ---------------------------------------------------------------------------

class TestIoTEndpointReachable:
    def test_iot_endpoint_env_set(self):
        assert IOT_ENDPOINT, (
            "IOT_ENDPOINT env var not set. Get it with:\n"
            "  aws iot describe-endpoint --endpoint-type iot:Data-ATS"
        )

    def test_iot_endpoint_port_8883_open(self):
        if not IOT_ENDPOINT:
            pytest.skip("IOT_ENDPOINT not set")
        try:
            with socket.create_connection((IOT_ENDPOINT, 8883), timeout=5):
                pass
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            pytest.fail(
                f"Cannot reach {IOT_ENDPOINT}:8883 — check firewall / VPN: {exc}"
            )


# ---------------------------------------------------------------------------
# Lambda invocation against real AWS
# ---------------------------------------------------------------------------

VALID = {
    "device_id": "smoke-test",
    "timestamp": "2024-06-07T00:00:00+00:00",
    "temperature": 25.5,
    "humidity": 60.0,
}


class TestLambdaInvocation:
    def test_normal_payload_returns_200(self, lambda_client):
        resp = lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(VALID).encode(),
        )
        # FunctionError present means Lambda itself crashed (not a handled 400)
        assert "FunctionError" not in resp or resp.get("FunctionError") is None, \
            f"Lambda returned FunctionError — check CloudWatch logs for traceback"
        body = json.loads(resp["Payload"].read())
        assert body["statusCode"] == 200
        assert body["result"]["status"] == "NORMAL"

    def test_critical_payload_classified(self, lambda_client):
        resp = lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps({**VALID, "temperature": 85.0}).encode(),
        )
        body = json.loads(resp["Payload"].read())
        assert body["result"]["status"] == "CRITICAL"

    def test_invalid_payload_returns_400(self, lambda_client):
        resp = lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps({"garbage": True}).encode(),
        )
        body = json.loads(resp["Payload"].read())
        assert body["statusCode"] == 400

    @pytest.mark.slow
    def test_cloudwatch_metric_emitted_after_invocation(self, lambda_client, cw_client):
        """
        Invoke Lambda then check that IoTSensors/normal_count appears in CloudWatch.
        CloudWatch has ~1 minute resolution so we wait up to 90s.
        This test is inherently slow and should not run in normal CI.
        """
        lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(VALID).encode(),
        )

        import datetime
        deadline = time.monotonic() + 90
        found = False
        while time.monotonic() < deadline:
            time.sleep(15)
            stats = cw_client.get_metric_statistics(
                Namespace="IoTSensors",
                MetricName="normal_count",
                Dimensions=[{"Name": "device_id", "Value": "smoke-test"}],
                StartTime=datetime.datetime.utcnow() - datetime.timedelta(minutes=5),
                EndTime=datetime.datetime.utcnow(),
                Period=300,
                Statistics=["Sum"],
            )
            if stats["Datapoints"]:
                found = True
                break

        assert found, (
            "IoTSensors/normal_count metric not visible in CloudWatch after 90s. "
            "Check Lambda execution role has cloudwatch:PutMetricData. "
            "Also verify namespace: 'IoTSensors' (case-sensitive)."
        )
