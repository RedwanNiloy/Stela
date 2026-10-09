"""Runs the browser-logic tests (tests/logic.test.js) with Node, so `pytest` covers them too.
Skipped when Node is not installed; the Python app itself does not need Node."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_logic_passes_under_node():
    result = subprocess.run(["node", "--test", str(HERE / "logic.test.js")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
