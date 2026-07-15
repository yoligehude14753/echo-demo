# B12R Release-Chain Repair — Coordinator Final Report

- scope: B12R narrow repair on B13 accepted exact SHA
- input SHA: `60fda97109cb6b300a45b16928efaa397d9fdf91`
- verdict: `RELEASE_CHAIN_REPAIR_READY`
- formal B15/B14 acceptance: not claimed or superseded; real signing and installed acceptance remain separate gates
- B15 original report: read-only input; not modified

## Subagents

| role | subagent id | result |
|---|---|---|
| current-sha-manifest-allowlist | `019f6724-a13d-7283-90d5-25b9eefd601d` | manifest/allowlist repair complete |
| windows-all-pe-signing-chain | `019f6724-a0d9-7e12-b0fa-8aec3cb59a03` | recursive PE/COFF signing-chain repair complete |
| release-gate-focused-verification | `019f6724-a1ad-74c1-93c5-0a37cbdac8e8` | focused verification and blocked-gate evidence returned |

Exactly 3 subagents were created and no fourth subagent was added.

## Changed files

- `.github/workflows/build-desktop-release-candidates.yml`
- `desktop/electron/tests/desktop-release-signing.test.cjs`
- `desktop/scripts/b12-post-sign-readback.cjs`
- `desktop/scripts/b12-post-sign-readback.test.cjs`
- `desktop/scripts/b12-signing-scope.cjs`
- `desktop/scripts/desktop-release-signing.cjs`
- `desktop/scripts/verify-windows-authenticode.ps1`
- `docs/0.3.3-bundled-agent-runtime/evidence/B12/fusion-content-manifest.json`
- `docs/0.3.3-bundled-agent-runtime/evidence/B12/package-allowlist.json`
- this task-owned report

No agent kernel/model/tool/persistence semantics were changed. The macOS inside-out signing contract was left intact.

## Manifest binding evidence

- `fusion-content-manifest.json.release_sha` = `60fda97109cb6b300a45b16928efaa397d9fdf91`
- `package-allowlist.json.release_sha` = `60fda97109cb6b300a45b16928efaa397d9fdf91`
- both signing boundaries retain `real_signing_performed: false`
- fusion manifest source bindings checked: `29 PASS`
- package allowlist build inputs checked: `10 PASS`
- B13 production coverage includes kernel, worker, embedded runtime, production composition/factory, host IPC, host-kernel dependencies, and backend B13 runtime inputs
- B12 manifest logical digest remains `3a60a31d4333c61f3fdc2325f53a7119ce59bca4acc34700a4368965c6b3c2f7`
- manifest digest value remains pending because no unsigned package build was performed

## Windows PE/COFF repair evidence

- recursive detection uses DOS `MZ` plus `PE\0\0` at `e_lfanew`; file extensions are non-authoritative
- empty inner scope fails closed
- outer NSIS scope is mandatory and fails closed when omitted
- every inner record carries path, size, SHA-256, signer/thumbprint/publisher placeholders or verified values, digest algorithm, and timestamp status
- `VerifyTree` recursively verifies all actual PE/COFF files; `VerifyZip` independently expands and verifies portable inner PE files
- outer NSIS is verified separately
- portable comparison requires exact inner PE path set and byte-for-byte size/SHA-256 equality with `win-unpacked`
- formal workflow writes `signed-artifact-manifest.json` and `signing-evidence.json` after inner/outer/portable verification and includes them in SHA256SUMS, provenance, and retained candidate evidence

## Focused gates

| gate | result |
|---|---|
| JSON parse and SHA binding | PASS; 2 manifests, exact SHA and signing boundary asserted |
| source hash/size inventory | PASS; 29 source bindings + 10 allowlist build inputs |
| Node syntax | PASS; 5 files |
| B12 post-sign readback unit tests | PASS; 5 tests |
| synthetic recursive PE/COFF and portable mutation regression | PASS; included in the 5 tests |
| Windows signing mock contract | PASS; 3 individual artifact `Verify` calls + 1 `VerifyZip` call, after preflight |
| workflow YAML parse | PASS; Ruby YAML parser |
| `git diff --check` | PASS |
| full desktop signing test | BLOCKED/NOT RUN; local checkout lacks `js-yaml` |
| PowerShell/AuthentiCode execution | NOT RUN; local host has no `pwsh` or `powershell` |

## Explicitly not executed

- no real Authenticode signing, certificate import, timestamp service, or credential read
- no macOS signing/notary/staple
- no Windows runner, NSIS install, upgrade/uninstall, Sunny acceptance, or remote acceptance
- no B13 source/provider/fused gate rerun
- no B14/B15 acceptance claim
- no push and no pull request

The local commit containing this report is the clean full SHA handoff for the repair; the exact value is reported by `git rev-parse HEAD` after commit.
