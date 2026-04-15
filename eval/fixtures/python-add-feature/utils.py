"""Utility functions for data processing."""

import json
from pathlib import Path


def load_config(config_path: str) -> dict:
    """Load a JSON configuration file."""
    with open(config_path) as f:
        return json.load(f)


def save_config(config_path: str, data: dict) -> None:
    """Save data to a JSON configuration file."""
    with open(config_path, "w") as f:
        json.dump(data, f, indent=2)


def process_records(records: list[dict]) -> list[dict]:
    """Process a list of records by normalizing fields."""
    result = []
    for record in records:
        processed = {
            "id": record.get("id"),
            "name": record.get("name", "").strip(),
            "value": float(record.get("value", 0)),
        }
        result.append(processed)
    return result
