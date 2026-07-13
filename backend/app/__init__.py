"""EchoDesk backend process boundary."""

from __future__ import annotations

import os

# CVE-2025-3000 affects torch.jit.script through torch 2.12 and has no patched
# release compatible with the current torchaudio runtime. EchoDesk never uses
# TorchScript, so disable it before any adapter can import torch/SpeechBrain.
os.environ["PYTORCH_JIT"] = "0"

__version__ = "0.3.2"
