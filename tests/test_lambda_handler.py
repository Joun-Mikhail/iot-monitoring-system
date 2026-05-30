"""
Unit tests for Lambda handler — all AWS calls mocked.
"""

import importlib
import json
import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 'lambda' is a Python keyword; importlib is the only clean way in
handler_module = importlib.import_module("lambda.handler")

VALID_EVENT = {
    "device_id": "sensor-01",
    "timestamp": "2024-06-07T12:00:00+00:00",
    "temperature": 25.5,
    "humidity": 60.0,
}


@pytest.fixture(autouse=True)
def mock_cloudwatch(monkeypatch):
    mock_cw = MagicMock()
    monkeypatch.setattr("boto3.client", lambda *a, **kw: mock_cw)
    handler_module._cw = None
    yield mock_cw


def invoke(event):
    return handler_module.handler(event, context={})


# --- classification outcomes ---

def test_normal_reading():
    r = invoke(VALID_EVENT)
    assert r["statusCode"] == 200
    assert r["result"]["status"] == "NORMAL"


def test_critical_temperature():
    r = invoke({**VALID_EVENT, "temperature": 85.0})
    assert r["result"]["status"] == "CRITICAL"


def test_warning_temperature():
    r = invoke({**VALID_EVENT, "temperature": 65.0})
    assert r["result"]["status"] == "WARNING"


def test_critical_humidity_low():
    r = invoke({**VALID_EVENT, "humidity": 5.0})
    assert r["result"]["status"] == "CRITICAL"


def test_critical_humidity_high():
    r = invoke({**VALID_EVENT, "humidity": 98.0})
    assert r["result"]["status"] == "CRITICAL"


# --- validation errors ---

def test_missing_field_returns_400():
    event = {k: v for k, v in VALID_EVENT.items() if k != "temperature"}
    assert invoke(event)["statusCode"] == 400


def test_string_temperature_returns_400():
    assert invoke({**VALID_EVENT, "temperature": "hot"})["statusCode"] == 400


def test_empty_payload_returns_400():
    assert invoke({})["statusCode"] == 400


def test_null_device_id_returns_400():
    assert invoke({**VALID_EVENT, "device_id": ""})["statusCode"] == 400


def test_bad_timestamp_returns_400():
    assert invoke({**VALID_EVENT, "timestamp": "yesterday"})["statusCode"] == 400


# --- metrics ---

def test_anomaly_emits_anomaly_count(mock_cloudwatch):
    invoke({**VALID_EVENT, "temperature": 90.0})
    mock_cloudwatch.put_metric_data.assert_called_once()
    md = mock_cloudwatch.put_metric_data.call_args[1]["MetricData"][0]
    assert md["MetricName"] == "anomaly_count"
    assert md["Dimensions"][0]["Value"] == "sensor-01"


def test_normal_emits_normal_count(mock_cloudwatch):
    invoke(VALID_EVENT)
    md = mock_cloudwatch.put_metric_data.call_args[1]["MetricData"][0]
    assert md["MetricName"] == "normal_count"


def test_cloudwatch_failure_is_non_fatal(mock_cloudwatch):
    mock_cloudwatch.put_metric_data.side_effect = Exception("timeout")
    r = invoke(VALID_EVENT)
    assert r["statusCode"] == 200


# --- record shape ---

def test_response_includes_all_fields():
    r = invoke(VALID_EVENT)
    rec = r["result"]
    for field in ("device_id", "timestamp", "temperature", "humidity", "status", "reason"):
        assert field in rec, f"missing field: {field}"
