# Access Key Policy

EchoDesk public builds use maintainer-issued Access Keys to call the hosted gateway.

## For Users

Keep your Access Key private. Do not share it publicly or commit it to any repository.

If you believe your key leaked, ask the maintainer to rotate it.

## For Maintainers

Generate one key per user or user group:

```bash
openssl rand -hex 24
```

Store allowed keys only on the gateway server in `ECHO_GW_TOKENS`.

Do not put real keys in:

- this public repository
- README examples
- GitHub issues
- screenshots
- installer resources
- `.env.example`

To revoke a key:

1. Remove it from `ECHO_GW_TOKENS` on the gateway server.
2. Restart the `echo-gateway` container.
3. Confirm the revoked key returns `401`.

## Current Public Gateway

```text
https://echodesk.yoliyoli.uk
```

Root path `/` is intentionally not a UI page. `{"detail":"Not Found"}` is expected.

