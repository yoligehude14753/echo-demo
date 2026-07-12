from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.unit
def test_backend_disables_torchscript_before_torch_import() -> None:
    env = os.environ.copy()
    env["PYTORCH_JIT"] = "1"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import app
                import os
                assert os.environ["PYTORCH_JIT"] == "0"
                import torch
                assert not bool(torch.jit._state._enabled)

                @torch.jit.script
                class Ignored:
                    def __init__(self):
                        self.count: int = 0
                        self.items: list = []

                Ignored()
                """
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
