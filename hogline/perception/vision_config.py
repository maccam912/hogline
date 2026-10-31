"""Read explicit local vision settings without exporting secrets into job documents."""

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

KEYS = {"OPENAI_API_KEY", "HOGLINE_VISION_MODEL", "HOGLINE_VISION_BUDGET_USD", "HOGLINE_VISION_ENABLED"}


@dataclass(frozen=True)
class VisionConfig:
    api_key: str = field(repr=False)
    model: str
    budget_usd: Decimal
    enabled: bool

    def public_status(self):
        return {
            "key_configured": bool(self.api_key),
            "model": self.model,
            "budget_usd": str(self.budget_usd),
            "enabled": self.enabled,
        }


def load_vision_config(path: Path | None = None):
    """Literal KEY=value only; no interpolation, command evaluation or implicit shell loading."""
    path = path or Path(__file__).resolve().parents[2] / ".env"
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key.strip() not in KEYS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    try:
        budget = Decimal(values.get("HOGLINE_VISION_BUDGET_USD", "5"))
    except InvalidOperation as exc:
        raise ValueError("Vision budget must be a finite nonnegative dollar amount") from exc
    if not budget.is_finite() or budget < 0:
        raise ValueError("Vision budget must be a finite nonnegative dollar amount")
    enabled = values.get("HOGLINE_VISION_ENABLED", "false").lower()
    if enabled not in ("true", "false"):
        raise ValueError("HOGLINE_VISION_ENABLED must be true or false")
    return VisionConfig(
        api_key=values.get("OPENAI_API_KEY", ""),
        model=values.get("HOGLINE_VISION_MODEL", "gpt-6.1-terra"),
        budget_usd=budget,
        enabled=enabled == "true",
    )
