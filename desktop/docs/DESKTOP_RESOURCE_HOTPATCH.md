# EchoDesk Desktop Resource Hot-patch

This tool shortens internal and on-site repair loops. It does **not** replace
the normal clean build, package readback, installer, signing, or release gates.
No hot-patch ZIP is published as a user-facing GitHub Release asset.

## Security boundary

Only these installed resource paths can be patched:

- `resources/app.asar`
- `resources/agent-runtime/**`
- optionally `resources/backend/**`

The tool refuses Electron executables, macOS helpers, Windows DLLs, installer
files, absolute paths, path traversal, symbolic-link payloads, duplicate paths,
unexpected ZIP compression, and mismatched hashes.

Each manifest binds:

- installed source SHA and version;
- target source SHA;
- every affected path;
- the expected installed SHA-256 (or expected absence);
- replacement size and SHA-256, or an explicit delete operation.

`manifest.sha256` binds the canonical manifest. `apply` validates the manifest,
payload, and currently installed old bytes before it stops or changes EchoDesk.

## Create a patch

First produce the next `app.asar` / runtime resources without running a full
installer build. Then compare them with the currently installed or unpacked
`Resources` directory:

```bash
cd desktop
npm run hotpatch:create -- \
  --base-resources "/path/to/current/Resources" \
  --next-resources "/path/to/next/Resources" \
  --from-source 1111111111111111111111111111111111111111 \
  --from-version 0.3.3-preview.3 \
  --to-source 2222222222222222222222222222222222222222 \
  --include app.asar \
  --include agent-runtime \
  --output "/tmp/echodesk-resource-patch" \
  --zip "/tmp/echodesk-resource-patch.zip"
```

Add `--include backend` only when the bundled backend binary changed.

## Dry-run on the target

macOS:

```bash
npm run hotpatch:apply -- \
  --patch "/tmp/echodesk-resource-patch.zip" \
  --app "/Applications/EchoDesk.app" \
  --dry-run
```

Windows PowerShell:

```powershell
npm run hotpatch:apply -- `
  --patch "C:\Temp\echodesk-resource-patch.zip" `
  --app "$env:LOCALAPPDATA\Programs\EchoDesk" `
  --dry-run
```

Dry-run performs all manifest, payload, allowlist, and installed-old-hash
checks without stopping or changing EchoDesk.

## Apply

macOS:

```bash
npm run hotpatch:apply -- \
  --patch "/tmp/echodesk-resource-patch.zip" \
  --app "/Applications/EchoDesk.app"
```

Windows PowerShell:

```powershell
npm run hotpatch:apply -- `
  --patch "C:\Temp\echodesk-resource-patch.zip" `
  --app "$env:LOCALAPPDATA\Programs\EchoDesk"
```

The apply transaction:

1. validates every old and new hash;
2. stops EchoDesk;
3. clones the installed `Resources` tree into a same-volume staging directory;
4. changes only allowlisted files in staging and validates them again;
5. swaps `Resources` to a same-volume backup and activates staging by rename;
6. on macOS, performs ad-hoc deep signing and strict verification;
7. rolls back the original tree on any staging, activation, signing, or strict
   verification failure;
8. removes the backup after success and restarts EchoDesk.

Use `--keep-backup` for a retained successful backup, or `--no-restart` when
the operator wants to relaunch manually. Windows never modifies
`EchoDesk.exe`, DLLs, WDAC policy, or SmartScreen state.
