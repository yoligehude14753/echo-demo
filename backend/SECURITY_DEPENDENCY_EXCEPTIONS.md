# Backend dependency security exceptions

## CVE-2025-3000 — torch 2.11.0

- Status: mitigated, temporarily allowlisted in dependency audit.
- Owner: EchoDesk maintainers.
- Exception expires: 2026-08-12; renewal requires a new upstream release audit.
- Scope: upstream advisory currently marks `torch<=2.12.0` affected and does
  not publish a fixed release compatible with the matching `torchaudio` pair.
- Product exposure: EchoDesk does not use TorchScript. The backend process
  forcibly sets `PYTORCH_JIT=0` before importing torch, and SpeechBrain ECAPA
  is instantiated with both `jit=False` and `compile=False`. Its Hugging Face
  model is pinned to revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`.
- Regression gate: `test_backend_disables_torchscript_before_torch_import`
  starts a clean interpreter with an adversarial `PYTORCH_JIT=1` and verifies
  the process boundary forces JIT off before torch loads.
- Removal trigger: replace the torch/torchaudio pair with a release containing
  the upstream fix, then remove the environment guard only after the regression
  suite and dependency audit pass without this exception.
- Last reviewed: 2026-07-12.
- Upstream references: GitHub advisory `GHSA-rrmf-rvhw-rf47`, PyTorch issue
  `#149623`, and fix commit `b90c94991cdf8b87c8f7439f79518e0ef2c4ca4f`.
