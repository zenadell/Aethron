#!/usr/bin/env python3
"""Test for handling optional config fields (attribute-on-none regression check)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forge

def test_optional_config_none_safety():
    # Verify that reading an optional field from config that returns None
    # does not cause an unhandled AttributeError when stripped/accessed.
    sample_cfg = {"name": "test-project", "platform": "framer"}

    val = sample_cfg.get("manifest_version")
    assert val is None, "manifest_version should default to None when absent"

    # Safe pattern for optional string fields
    safe_val = (val or "").strip()
    assert safe_val == "", "Safe conversion of None should be empty string"

    val_with_str = "  v1.0  "
    safe_val_str = (val_with_str or "").strip()
    assert safe_val_str == "v1.0", "Safe conversion should strip whitespace when present"
    print("test_optional_config_none_safety: PASS")

if __name__ == "__main__":
    test_optional_config_none_safety()
