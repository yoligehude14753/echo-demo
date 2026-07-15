# Backend dependency security exceptions

## PYSEC-2026-3447 / CVE-2026-59890 / GHSA-h35f-9h28-mq5c — setuptools 81.0.0

- Status: temporary upstream-constraint exception. Raw dependency audits must
  report this exact finding and exit non-zero; it must not be hidden or turned
  into a zero-vulnerability result.
- Owner: EchoDesk maintainers.
- Exception expires: 2026-08-12; renewal requires a compatible upstream
  dependency release and a fresh advisory audit.
- Constraint: `torch==2.11.0` and `torch==2.11.0+cpu` require
  `setuptools<82` on the Python 3.11 Linux, macOS, and Windows install paths.
  The advisory reports `setuptools==81.0.0` fixed in `83.0.0`, so selecting the
  fixed release makes all three locked install graphs unsatisfiable.
- Audited locks: `requirements.lock`, `requirements-dev.lock`, and
  `packaging/requirements-build.lock` each report exactly this advisory plus
  the separate torch exception below. The lint, typecheck, and audit-tool locks
  do not contain either package.
- Removal trigger: upgrade the torch/torchaudio model pair or another reviewed
  compatible dependency path so `setuptools>=83.0.0` installs on all three
  platforms, then rerun the raw audit and remove this exception.

## CVE-2025-3000 / GHSA-rrmf-rvhw-rf47 — torch 2.11.0

- Status: unresolved upstream finding with runtime mitigation. Raw dependency
  audits are expected to report this finding and exit non-zero; it must not be
  ignored and presented as a zero-vulnerability result.
- Owner: EchoDesk maintainers.
- Exception expires: 2026-08-12; renewal requires a fresh upstream release and
  advisory audit.
- Severity and affected range: GitHub Advisory Database reports CVSS v4 `1.9`
  and a vulnerable range of `torch<=2.12.0`, but still reports
  `first_patched_version: null`; `pip-audit==2.10.1` likewise returns an empty
  `fix_versions` list as of 2026-07-13.
- Audited locks: `requirements.lock`, `requirements-dev.lock`, and
  `packaging/requirements-build.lock` each report this one finding. The lint,
  typecheck, and audit-tool locks do not contain torch.
- Required product use: torch/torchaudio/SpeechBrain implement the default
  in-process ECAPA speaker diarizer and are imported by the packaged backend
  dependency boundary. Removing them from the 0.3.2 runtime would remove the
  default speaker-identification path. Splitting ECAPA into an optional install
  also requires coordinated installer, frozen-binary, CI, and product fallback
  changes, so it is not a lock-only security update.
- Exposure reduction: EchoDesk does not call TorchScript. The backend process
  forces `PYTORCH_JIT=0` before any torch or SpeechBrain import; SpeechBrain
  ECAPA is instantiated with `jit=False` and `compile=False`, uses CPU eager
  execution, and pins its Hugging Face model to revision
  `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`.
- Regression gate: `test_backend_disables_torchscript_before_torch_import`
  starts a clean interpreter with adversarial `PYTORCH_JIT=1`, verifies the
  process boundary forces JIT off before torch loads, and exercises the advisory
  reproducer shape without enabling TorchScript.
- Upstream state: PyTorch 2.13.0 was published on 2026-07-08, and the latest
  TorchAudio release remains 2.11.0 with an upstream compatibility statement
  covering future torch versions. This is not sufficient patched-version
  evidence: PyTorch main fix commit
  `b90c94991cdf8b87c8f7439f79518e0ef2c4ca4f` is not an ancestor of the
  `v2.13.0` tag, while the advisory and `pip-audit` still declare no first/fix
  version. Do not present 2.13.0 as resolving this finding until upstream
  advisory data confirms a patched release and the ECAPA/package regression
  suite passes on that release pair.
- Removal trigger: upgrade to a matching official torch/torchaudio release that
  the advisory marks patched, rerun the ECAPA/package regression suite and raw
  audits, then remove this exception. Keep the process-level JIT guard unless a
  separate reviewed change demonstrates it is unnecessary.
- Last reviewed: 2026-07-13.
- Upstream references:
  - <https://github.com/advisories/GHSA-rrmf-rvhw-rf47>
  - <https://github.com/pytorch/pytorch/issues/149623>
  - <https://github.com/pytorch/pytorch/commit/b90c94991cdf8b87c8f7439f79518e0ef2c4ca4f>
  - <https://github.com/pytorch/pytorch/releases/tag/v2.13.0>
  - <https://github.com/pytorch/audio/releases/tag/v2.11.0>
