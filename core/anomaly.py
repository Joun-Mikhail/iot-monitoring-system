from dataclasses import dataclass


@dataclass
class AnomalyResult:
    status: str  # NORMAL / WARNING / CRITICAL
    reason: str


def classify(temperature: float, humidity: float) -> AnomalyResult:
    if temperature >= 80:
        return AnomalyResult("CRITICAL", f"temperature={temperature}°C exceeds critical threshold (80°C)")
    if temperature >= 60:
        return AnomalyResult("WARNING", f"temperature={temperature}°C exceeds warning threshold (60°C)")
    if humidity < 10:
        return AnomalyResult("CRITICAL", f"humidity={humidity}% below critical threshold (10%)")
    if humidity > 95:
        return AnomalyResult("CRITICAL", f"humidity={humidity}% exceeds critical threshold (95%)")
    return AnomalyResult("NORMAL", "all readings within bounds")
