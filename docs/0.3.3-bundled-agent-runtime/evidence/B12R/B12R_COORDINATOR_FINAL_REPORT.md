# B12R final exact-SHA rebind report

## Verdict

`RELEASE_CHAIN_REPAIR_READY`

The preserved B12R release-chain overlay is mechanically rebound to the final source candidate. This verdict is limited to source identity, manifest/allowlist consistency, workflow/readback contracts, and focused static/unit gates. It does not claim B14/B15 signed-install acceptance, Windows runtime acceptance, package/install/publish acceptance, notarization, or remote validation.

## Immutable source binding

- Final source candidate: `65ce495a11e8158537b8fb387c9cec25b9801c2a`
- Final source candidate parent: `fc3ce989b61c01dc074cd2f897974fbd580fddb3`
- Candidate relationship: the source candidate is a direct child of the recorded parent.
- `fusion-content-manifest.json.release_sha`: `65ce495a11e8158537b8fb387c9cec25b9801c2a`
- `package-allowlist.json.release_sha`: `65ce495a11e8158537b8fb387c9cec25b9801c2a`
- Signing scope `STARTING_SHA`: `65ce495a11e8158537b8fb387c9cec25b9801c2a`
- Signing scope `PARENT_SHA`: `fc3ce989b61c01dc074cd2f897974fbd580fddb3`
- Logical content digest: `96f2ced4eaf5a0cf227a9aad19bd5485a7c60766c5479dea46863fc273e66258`
- `real_signing_performed`: `false` in both B12 manifest and package allowlist
- No signing identity, certificate, timestamp, or credential value was generated or read.

## Overlay provenance

The existing staged overlay was retained without reset or discard. It carries forward the original three-lane B12R implementation:

1. `019f6724-a13d-7283-90d5-25b9eefd601d` — manifest and allowlist
2. `019f6724-a0d9-7e12-b0fa-8aec3cb59a03` — Windows all-PE signing chain
3. `019f6724-a1ad-74c1-93c5-0a37cbdac8e8` — focused release-gate verification

## Changed files

- `.github/workflows/build-desktop-release-candidates.yml`
- `desktop/scripts/b12-post-sign-readback.cjs`
- `desktop/scripts/b12-post-sign-readback.test.cjs`
- `desktop/scripts/b12-signing-scope.cjs`
- `desktop/scripts/desktop-release-signing.cjs`
- `desktop/scripts/verify-windows-authenticode.ps1`
- `docs/0.3.3-bundled-agent-runtime/evidence/B12/fusion-content-manifest.json`
- `docs/0.3.3-bundled-agent-runtime/evidence/B12/package-allowlist.json`
- this task-owned report

## Manifest and allowlist evidence

- Manifest package files: 26
- Manifest source bindings: 30; exact candidate source hash/size checks: 30/30 pass
- Source-bound manifest package entries: 18; exact candidate source hash/size checks: 18/18 pass
- `desktop/package.json` was refreshed to 7052 bytes and SHA-256 `b4ff7021cc8ce71195578e0cd896f805b09f3282bf54d7238c5af49cbc0f93b9`.
- Allowlist ASAR entries: 26
- Allowlist unpacked resources: 13
- Allowlist worker source entries: 15
- Allowlist kernel source entries: 14
- Allowlist build inputs: 12; exact candidate existence checks: 12/12 pass
- Manifest digest value remains pending because no unsigned package build was performed.

## Windows PE/readback contract

- Recursive PE/COFF detection uses DOS `MZ` plus `PE\0\0` at `e_lfanew`; extension is non-authoritative.
- Inner `win-unpacked` PE files and the outer NSIS installer are separately enumerated and verified.
- Each PE verification requires expected thumbprint, publisher, certificate chain, SHA-256, and RFC3161 timestamp.
- Portable ZIP PE bytes are compared item-by-item against `win-unpacked` and receive separate `VerifyTree` and `VerifyZip` checks.
- Empty PE scopes, missing packaged resources, byte drift, and authentication/readback failures are fail-closed.
- Workflow evidence separates inner, portable-inner, and outer PE rows.

## Focused gates

| gate | result |
|---|---|
| JSON parse and exact SHA/parent binding | PASS |
| Workflow YAML parse | PASS |
| Node syntax | PASS; 4/4 |
| B12 post-sign readback unit tests | PASS; 5 passed, 0 failed |
| Exact-candidate source hash/size inventory | PASS; 30/30 source bindings and 18/18 source-bound package entries |
| Allowlist build-input existence | PASS; 12/12 |
| Static exact-SHA/workflow/readback contract | PASS |
| `git diff --check` | PASS |

## Explicitly not executed

- No platform build, Authenticode signing, certificate import, timestamp service, or credential read.
- No macOS signing, notarization, or staple.
- No Windows runner, NSIS install, upgrade/uninstall, Sunny acceptance, or remote acceptance.
- No package, install, or publish action.
- No push or pull request.

The clean local commit containing this report is the immutable B12R handoff; its exact full SHA is reported after commit.
