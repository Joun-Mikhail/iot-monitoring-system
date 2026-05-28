from datetime import datetime


REQUIRED_FIELDS = {"device_id", "timestamp", "temperature", "humidity"}


class ValidationError(Exception):
    pass


def validate_payload(payload: dict) -> None:
    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        raise ValidationError(f"missing fields: {missing}")

    if not isinstance(payload["device_id"], str) or not payload["device_id"].strip():
        raise ValidationError("device_id must be a non-empty string")

    try:
        datetime.fromisoformat(payload["timestamp"])
    except (ValueError, TypeError):
        raise ValidationError(f"timestamp is not valid ISO-8601: {payload['timestamp']!r}")

    for field in ("temperature", "humidity"):
        val = payload[field]
        if not isinstance(val, (int, float)):
            raise ValidationError(f"{field} must be numeric, got {type(val).__name__}")

    if not (0 <= payload["humidity"] <= 100):
        raise ValidationError(f"humidity out of range: {payload['humidity']}")
