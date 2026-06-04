"""
Integration tests against LocalStack.

Requires LocalStack running with lambda + logs + cloudwatch services:
    docker run --rm -p 4566:4566 \
        -e SERVICES=lambda,logs,cloudwatch \
        localstack/localstack

Run with:
    pytest tests/test_integration_localstack.py -v -m integration

These are skipped automatically when LocalStack is unreachable.
They are NOT part of the default unit-test run.

Known LocalStack gaps vs real AWS (documented, not worked around):
  - LocalStack community edition does not always populate Function.State.
    We poll on that but fall through after a timeout rather than hanging.
  - CloudWatch PutMetricData succeeds silently in LocalStack but metrics
    may not be queryable via GetMetricStatistics for several seconds.
  - Log propagation in LocalStack is faster than real AWS but still async;
    tests that assert on log content use short sleeps + skips rather than retries.
"""

import io
import json
import os
import sys
import time
import zipfile

import pytest

LOCALSTACK_ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")
REGION = "eu-central-1"
ACCOUNT_ID = "000000000000"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _localstack_reachable() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen(f"{LOCALSTACK_ENDPOINT}/_localstack/health", timeout=3)
        return True
    except Exception:
        return False


def _build_zip() -> bytes:
    """Bundle handler.py + core/ into an in-memory zip for deployment."""
    buf = io.BytesIO()
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(root, "lambda", "handler.py"), "handler.py")
        for fname in ("__init__.py", "anomaly.py", "validator.py"):
            src = os.path.join(root, "core", fname)
            if os.path.exists(src):
                zf.write(src, os.path.join("core", fname))
    return buf.getvalue()


def _wait_lambda_active(client, fn_name: str, timeout: float = 15.0) -> None:
    """
    Poll until function is Active or timeout.
    LocalStack community edition often skips state transitions and returns
    Active immediately (or never sets it at all). We try for a reasonable
    period then give up and proceed — if the function isn't ready the invoke
    will fail with its own error.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cfg = client.get_function_configuration(FunctionName=fn_name)
            state = cfg.get("State", "Active")  # default Active for LocalStack compat
            if state == "Active":
                return
            if state == "Failed":
                pytest.fail(f"Lambda function entered Failed state: {cfg.get('StateReasonCode')}")
        except Exception:
            pass
        time.sleep(0.5)
    # don't fail here — let the invoke surface whatever problem exists


# ---------------------------------------------------------------------------
# session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def require_localstack():
    if not _localstack_reachable():
        pytest.skip(
            f"LocalStack not reachable at {LOCALSTACK_ENDPOINT}. "
            "Start it with: docker run --rm -p 4566:4566 -e SERVICES=lambda,logs,cloudwatch localstack/localstack"
        )


@pytest.fixture(scope="session")
def _bkw():
    return {
        "endpoint_url": LOCALSTACK_ENDPOINT,
        "region_name": REGION,
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }


@pytest.fixture(scope="session")
def lambda_client(_bkw):
    import boto3
    return boto3.client("lambda", **_bkw)


@pytest.fixture(scope="session")
def logs_client(_bkw):
    import boto3
    return boto3.client("logs", **_bkw)


@pytest.fixture(scope="session")
def cw_client(_bkw):
    import boto3
    return boto3.client("cloudwatch", **_bkw)


@pytest.fixture(scope="session")
def deployed_lambda(lambda_client):
    fn_name = "iot_integration_test"
    zip_bytes = _build_zip()

    try:
        lambda_client.delete_function(FunctionName=fn_name)
        time.sleep(0.5)
    except lambda_client.exceptions.ResourceNotFoundException:
        pass

    lambda_client.create_function(
        FunctionName=fn_name,
        Runtime="python3.11",
        Role=f"arn:aws:iam::{ACCOUNT_ID}:role/dummy",
        Handler="handler.handler",
        Code={"ZipFile": zip_bytes},
        Timeout=10,
        Environment={"Variables": {"AWS_ENDPOINT_URL": LOCALSTACK_ENDPOINT}},
    )

    _wait_lambda_active(lambda_client, fn_name)
    yield fn_name

    try:
        lambda_client.delete_function(FunctionName=fn_name)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _invoke(lambda_client, fn_name: str, payload: dict) -> dict:
    resp = lambda_client.invoke(
        FunctionName=fn_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    return json.loads(resp["Payload"].read())


VALID = {
    "device_id": "sensor-01",
    "timestamp": "2024-06-07T12:00:00+00:00",
    "temperature": 25.5,
    "humidity": 60.0,
}


# ---------------------------------------------------------------------------
# invocation tests
# ---------------------------------------------------------------------------

class TestInvocation:
    def test_normal_returns_200(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, VALID)
        assert r["statusCode"] == 200
        assert r["result"]["status"] == "NORMAL"

    def test_critical_temperature(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, {**VALID, "temperature": 85.0})
        assert r["result"]["status"] == "CRITICAL"

    def test_warning_temperature(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, {**VALID, "temperature": 65.0})
        assert r["result"]["status"] == "WARNING"

    def test_missing_field_400(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, {k: v for k, v in VALID.items() if k != "humidity"})
        assert r["statusCode"] == 400

    def test_wrong_type_400(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, {**VALID, "temperature": "hot"})
        assert r["statusCode"] == 400

    def test_empty_payload_400(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, {})
        assert r["statusCode"] == 400

    def test_response_shape(self, lambda_client, deployed_lambda):
        r = _invoke(lambda_client, deployed_lambda, VALID)
        rec = r["result"]
        for field in ("device_id", "timestamp", "temperature", "humidity", "status", "reason"):
            assert field in rec


# ---------------------------------------------------------------------------
# log assertions
# Note: LocalStack log propagation is async. We use a short sleep + skip-on-miss
# rather than a hard assertion, because the timing is unreliable enough that
# making it a hard assert would cause spurious CI failures.
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestLogs:
    def _get_recent_messages(self, logs_client, log_group: str) -> list[str]:
        try:
            streams = logs_client.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=1,
            )["logStreams"]
        except logs_client.exceptions.ResourceNotFoundException:
            return []
        if not streams:
            return []
        events = logs_client.get_log_events(
            logGroupName=log_group,
            logStreamName=streams[0]["logStreamName"],
            limit=50,
        )["events"]
        return [e["message"] for e in events]

    def test_processed_line_in_logs(self, lambda_client, logs_client, deployed_lambda):
        _invoke(lambda_client, deployed_lambda, VALID)
        time.sleep(2)
        messages = self._get_recent_messages(logs_client, f"/aws/lambda/{deployed_lambda}")
        if not messages:
            pytest.skip("log group not populated yet — LocalStack async delay")
        assert any("processed" in m for m in messages), \
            f"'processed' not found in recent log lines: {messages[-3:]}"

    def test_alert_line_for_anomaly(self, lambda_client, logs_client, deployed_lambda):
        _invoke(lambda_client, deployed_lambda, {**VALID, "temperature": 92.0})
        time.sleep(2)
        messages = self._get_recent_messages(logs_client, f"/aws/lambda/{deployed_lambda}")
        if not messages:
            pytest.skip("log group not populated yet")
        assert any("ALERT TRIGGERED" in m for m in messages), \
            f"alert line not found in recent log lines: {messages[-3:]}"
