# Security Policy

## Official Distribution

The official EchoDesk public distribution repository is:

```text
https://github.com/yoligehude14753/echodesk-public
```

Only download installers from this repository's Releases page. Do not run binaries from unofficial mirrors, random ZIP files, or third-party "cracked" packages.

## Access Key Safety

EchoDesk uses an Access Key to call the public gateway:

```text
https://echodesk.yoliyoli.uk
```

Never publish your Access Key in:

- GitHub issues, pull requests, screenshots, or logs
- public `.env`, `config.json`, or shell history
- shared documents or chat messages with untrusted users

If your key is exposed, contact the maintainer to revoke and rotate it.

## Gateway Behavior

The gateway root path `/` is not a website. Seeing `{"detail":"Not Found"}` at `https://echodesk.yoliyoli.uk/` is expected.

Use `https://echodesk.yoliyoli.uk/health` for a minimal health check. Authenticated app traffic uses `/v1/...` API endpoints.

## Reporting Vulnerabilities

Report suspected vulnerabilities privately to the maintainer. Do not open a public issue containing exploit details, access keys, private logs, or internal URLs.

Useful report details:

- EchoDesk version
- Operating system and CPU architecture
- Steps to reproduce
- Whether the issue requires a valid Access Key
- Redacted logs or screenshots

## Build Integrity

Release assets should include checksums where possible. Verify downloaded files before installation:

```bash
shasum -a 256 EchoDesk-*
```

Code signing and notarization are planned. Until then, macOS Gatekeeper and Windows SmartScreen may show warnings for unsigned builds.

