# Backend dependency security exceptions

## PYSEC-2026-3447 / CVE-2026-59890 / GHSA-h35f-9h28-mq5c — setuptools 81.0.0

- Status: temporary upstream-constraint exception. Raw dependency audits must
  report this exact finding and exit non-zero; it must not be hidden or turned
  into a zero-vulnerability result.
- Owner: EchoDesk maintainers.
- Exception expires: 2026-09-30; renewal requires a compatible upstream
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
- Exception expires: 2026-09-30; renewal requires a fresh upstream release and
  advisory audit.
- Severity and affected range: the current `pip-audit==2.10.1` feed reports
  `fix_versions=["2.13.0"]`, but the locked product pair cannot take that
  version alone: the official CPU index currently exposes TorchAudio only
  through `2.11.0`, while the product pins `torch==2.11.0`/`torchaudio==2.11.0`.
  A matching patched pair is therefore not available for this release.
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
- Upstream state reviewed 2026-08-26: PyTorch 2.13.0 is published, but the
  CPU index has no matching TorchAudio 2.13.0 release. The PyTorch 2.11.0
  metadata also requires `setuptools<82`, so upgrading setuptools alone is
  unsatisfiable. Do not present 2.13.0 as resolving this finding until a
  matching Torch/TorchAudio pair and the ECAPA/package regression suite pass.
- Removal trigger: upgrade to a matching official torch/torchaudio release that
  the advisory marks patched, rerun the ECAPA/package regression suite and raw
  audits, then remove this exception. Keep the process-level JIT guard unless a
  separate reviewed change demonstrates it is unnecessary.
- Last reviewed: 2026-08-26.
- Upstream references:
  - <https://github.com/advisories/GHSA-rrmf-rvhw-rf47>
  - <https://github.com/pytorch/pytorch/issues/149623>
  - <https://github.com/pytorch/pytorch/commit/b90c94991cdf8b87c8f7439f79518e0ef2c4ca4f>
  - <https://github.com/pytorch/pytorch/releases/tag/v2.13.0>
  - <https://github.com/pytorch/audio/releases/tag/v2.11.0>
  - <https://download.pytorch.org/whl/cpu/>

## GHSA-w3rx-r6r6-pgpr / GHSA-5p2g-fcmc-qvqq — image-size 1.2.1 via pptxgenjs 3.12.0

- Status: temporary upstream exception for the packaged legacy PPT runtime.
  Raw npm audit JSON must be retained and must contain exactly these two
  advisories; the findings must not be hidden or converted to exit 0.
- Owner: EchoDesk maintainers.
- Exception expires: 2026-09-30; renew only after a fresh npm audit and an
  upstream release with a non-breaking fix.
- Constraint: npm audit currently recommends the breaking downgrade
  `pptxgenjs@1.1.5`; the EchoDesk legacy executor requires the 3.x constructor
  and API contract, so that downgrade cannot be applied as a lock-only fix.
- Audited lock: `backend/app/adapters/skill/assets/ppt_ib_deck/package-lock.json`
  pins `pptxgenjs==3.12.0` and `image-size==1.2.1`. The workflow keeps the raw
  report and validates the exact advisory IDs, package versions, expiry, and
  breaking-only fix before accepting this exception.
- Removal trigger: upgrade or replace the PPT runtime with a compatible
  release that removes both advisories, then delete this section and the
  exception checker call.
