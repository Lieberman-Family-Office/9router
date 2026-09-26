# Dashboard edge Caddy proof

Production Caddy on the `9r` EC2 edge historically proxied only `/v1/*` and
returned an empty HTTP 404 for every other path, so
`https://9r.liebermanfamilyoffice.com/` could not serve the Next.js dashboard.

[`Caddyfile.prod-desired`](./Caddyfile.prod-desired) is the desired edge shape:

1. `/v1` / `/v1/*` stay bearer-gated → combo-router `:8090`.
2. Everything else → Mac 9router `:20128` without rewriting `Host` (cookies bind
   to the public hostname).

Disposable Linux container proof (old vs new):

- **old:** `/v1` bearer-gated; `/` and `/dashboard` → empty 404
- **new:** same `/v1` gate; UI paths reach a fake 9router (HTML + Set-Cookie)

```bash
./tests/dashboard_edge/repro.sh
```

Requires Docker (or Colima with `DOCKER_HOST` set). Does not touch production.
The live edge file is machine-local (`/etc/caddy/Caddyfile`); re-apply from
`Caddyfile.prod-desired` after rebuild or rollback.
