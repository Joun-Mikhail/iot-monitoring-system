import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.validator import validate_payload, ValidationError


VALID = {
    "device_id": "sensor-01",
    "timestamp": "2024-06-07T12:00:00+00:00",
    "temperature": 25.5,
    "humidity": 60.0,
}


def test_valid_payload_passes():
    validate_payload(VALID)  # no exception


def test_missing_device_id():
    p = {k: v for k, v in VALID.items() if k != "device_id"}
    with pytest.raises(ValidationError, match="missing"):
        validate_payload(p)


def test_missing_temperature():
    p = {k: v for k, v in VALID.items() if k != "temperature"}
    with pytest.raises(ValidationError, match="missing"):
        validate_payload(p)


def test_missing_multiple_fields():
    with pytest.raises(ValidationError, match="missing"):
        validate_payload({})


def test_empty_device_id():
    p = {**VALID, "device_id": ""}
    with pytest.raises(ValidationError, match="device_id"):
        validate_payload(p)


def test_whitespace_device_id():
    p = {**VALID, "device_id": "   "}
    with pytest.raises(ValidationError, match="device_id"):
        validate_payload(p)


def test_invalid_timestamp():
    p = {**VALID, "timestamp": "not-a-date"}
    with pytest.raises(ValidationError, match="timestamp"):
        validate_payload(p)


def test_temperature_as_string():
    p = {**VALID, "temperature": "25.5"}
    with pytest.raises(ValidationError, match="temperature"):
        validate_payload(p)


def test_humidity_out_of_range_high():
    p = {**VALID, "humidity": 105.0}
    with pytest.raises(ValidationError, match="humidity out of range"):
        validate_payload(p)


def test_humidity_out_of_range_low():
    p = {**VALID, "humidity": -1.0}
    with pytest.raises(ValidationError, match="humidity out of range"):
        validate_payload(p)


def test_integer_values_accepted():
    p = {**VALID, "temperature": 25, "humidity": 60}
    validate_payload(p)  # no exception


def test_malformed_mqtt_payload_missing_all():
    """Simulates receiving a completely wrong MQTT payload."""
    with pytest.raises(ValidationError):
        validate_payload({"foo": "bar"})
