"""Manifest sanity tests."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.comfort_band.const import DOMAIN

MANIFEST_PATH = (
    Path(__file__).parent.parent / "custom_components" / "comfort_band" / "manifest.json"
)


def test_domain_constant() -> None:
    assert DOMAIN == "comfort_band"


def test_manifest_shape() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "helper"
    assert manifest["iot_class"] == "local_push"
    assert manifest["codeowners"] == ["@dpretty"]
    assert manifest["version"] == "0.9.1"
    assert manifest["documentation"].startswith("https://")
    assert manifest["issue_tracker"].startswith("https://")
    assert manifest["requirements"] == []
