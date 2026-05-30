import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.anomaly import classify


class TestCriticalTemperature:
    def test_exactly_80_is_critical(self):
        result = classify(80.0, 50.0)
        assert result.status == "CRITICAL"

    def test_above_80_is_critical(self):
        result = classify(95.5, 50.0)
        assert result.status == "CRITICAL"

    def test_99_9_is_critical(self):
        result = classify(99.9, 50.0)
        assert result.status == "CRITICAL"


class TestWarningTemperature:
    def test_exactly_60_is_warning(self):
        result = classify(60.0, 50.0)
        assert result.status == "WARNING"

    def test_79_9_is_warning(self):
        result = classify(79.9, 50.0)
        assert result.status == "WARNING"

    def test_65_is_warning(self):
        result = classify(65.0, 50.0)
        assert result.status == "WARNING"


class TestNormal:
    def test_normal_readings(self):
        result = classify(25.0, 55.0)
        assert result.status == "NORMAL"

    def test_boundary_below_warning(self):
        result = classify(59.9, 50.0)
        assert result.status == "NORMAL"

    def test_boundary_humidity_lower(self):
        result = classify(25.0, 10.0)
        assert result.status == "NORMAL"

    def test_boundary_humidity_upper(self):
        result = classify(25.0, 95.0)
        assert result.status == "NORMAL"


class TestCriticalHumidity:
    def test_humidity_below_10_is_critical(self):
        result = classify(25.0, 9.9)
        assert result.status == "CRITICAL"

    def test_humidity_zero_is_critical(self):
        result = classify(25.0, 0.0)
        assert result.status == "CRITICAL"

    def test_humidity_above_95_is_critical(self):
        result = classify(25.0, 95.1)
        assert result.status == "CRITICAL"

    def test_humidity_100_is_critical(self):
        result = classify(25.0, 100.0)
        assert result.status == "CRITICAL"


class TestTemperaturePriority:
    """Critical temperature takes precedence over humidity anomalies."""

    def test_critical_temp_beats_critical_humidity(self):
        result = classify(85.0, 5.0)
        assert result.status == "CRITICAL"
        assert "temperature" in result.reason

    def test_warning_temp_beats_humidity_anomaly(self):
        result = classify(65.0, 5.0)
        assert result.status == "WARNING"
        assert "temperature" in result.reason
