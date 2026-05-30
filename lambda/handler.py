"""
Lambda function: process_sensor_data
Triggered by AWS IoT Rule Engine on topic iot/sensors/data.

Packaging note: core/ is bundled into the zip at build time via scripts/build_lambda.sh.
No Lambda layer. boto3 is provided by the Lambda runtime (no need to ship it).
"""

import json
import logging
import os
import time

import boto3

from core.anomaly import classify
from core.validator import ValidationError, validate_payload

log = logging.getLogger()
log.setLevel(logging.INFO)

METRIC_NAMESPACE = "IoTSensors"

# module-level client — reused across warm invocations
_cw = None


def _get_cw():
    global _cw
    if _cw is None:
        _cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "eu-central-1"))
    return _cw


def _emit(metrics: list) -> None:
    """
    Emit a batch of metric datums. Failures are non-fatal — we log and move on.
    CloudWatch PutMetricData accepts up to 20 datums per call; we stay well under that.
    """
    try:
        _get_cw().put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metrics)
    except Exception as exc:
        log.warning("metric emission failed (non-fatal): %s", exc)


def handler(event, context):
    start = time.monotonic()

    # warn if Lambda is running close to its timeout — helps diagnose cold-start issues
    remaining_fn = getattr(context, "get_remaining_time_in_millis", None)
    if remaining_fn and remaining_fn() < 2000:
        log.warning("low remaining time: %dms — handler may not complete", remaining_fn())

    log.info("raw event: %s", json.dumps(event))

    try:
        validate_payload(event)
    except ValidationError as exc:
        log.error("validation failed: %s | payload=%s", exc, json.dumps(event))
        _emit([
            {
                "MetricName": "validation_error_count",
                "Dimensions": [{"Name": "device_id", "Value": str(event.get("device_id", "unknown"))}],
                "Value": 1,
                "Unit": "Count",
            }
        ])
        return {"statusCode": 400, "error": str(exc)}

    device_id = event["device_id"]
    timestamp = event["timestamp"]
    temperature = float(event["temperature"])
    humidity = float(event["humidity"])

    result = classify(temperature, humidity)
    elapsed_ms = (time.monotonic() - start) * 1000

    record = {
        "device_id": device_id,
        "timestamp": timestamp,
        "temperature": temperature,
        "humidity": humidity,
        "status": result.status,
        "reason": result.reason,
    }

    log.info("processed: %s", json.dumps(record))

    if result.status in ("WARNING", "CRITICAL"):
        log.warning(
            "ALERT TRIGGERED\nDevice: %s\nMetric: anomaly detected\nValue: temp=%.2f°C hum=%.2f%%\nTimestamp: %s\nSeverity: %s",
            device_id, temperature, humidity, timestamp, result.status,
        )

    status_metric = "anomaly_count" if result.status != "NORMAL" else "normal_count"
    _emit([
        {
            "MetricName": status_metric,
            "Dimensions": [{"Name": "device_id", "Value": device_id}],
            "Value": 1,
            "Unit": "Count",
        },
        {
            "MetricName": "processing_duration_ms",
            "Dimensions": [{"Name": "device_id", "Value": device_id}],
            "Value": elapsed_ms,
            "Unit": "Milliseconds",
        },
    ])

    return {"statusCode": 200, "result": record}
