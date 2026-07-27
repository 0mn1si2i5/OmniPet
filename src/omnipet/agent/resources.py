"""Safe access to packaged agent contracts."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any


def resource_inventory() -> list[str]:
    """Return packaged JSON resource paths in deterministic order."""
    root = files("omnipet.agent")
    inventory: list[str] = []
    for directory_name in ("contracts", "schemas"):
        directory = root.joinpath(directory_name)
        if directory.is_dir():
            inventory.extend(
                f"{directory_name}/{entry.name}"
                for entry in directory.iterdir()
                if entry.is_file() and entry.name.endswith(".json")
            )
    return sorted(inventory)


def load_json_resource(name: str) -> dict[str, Any]:
    """Load a known agent JSON resource, rejecting unsafe or unknown names."""
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("agent resource is invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("agent resource is invalid")
    if name not in resource_inventory():
        raise ValueError("agent resource is invalid")

    try:
        value = json.loads(
            files("omnipet.agent").joinpath(*path.parts).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("agent resource is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("agent resource is invalid")
    return value
