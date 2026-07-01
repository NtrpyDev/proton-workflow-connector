# Live-test fixture Worker

Disposable OIDC issuer and webhook receiver for `scripts/live_acceptance.py`. It signs test
JWTs the hosted-OAuth suite uses against a real HTTPS PWC endpoint, and records watcher
webhook deliveries (with an on-demand fail mode for dead-letter tests).

This is test tooling. It never sees Proton credentials or mail content.

## One-time setup

```bash
cd tests/fixtures/worker
node generate-jwk.mjs          # prints PRIVATE_JWK, WRONG_PRIVATE_JWK, MINT_SECRET
npx wrangler secret put PRIVATE_JWK
npx wrangler secret put WRONG_PRIVATE_JWK
npx wrangler secret put MINT_SECRET
npx wrangler deploy
```

The workers.dev URL is enough: the issuer value follows whatever origin the request arrives
on. To pin a hostname instead, uncomment `routes` in `wrangler.jsonc` once the zone is chosen.

## Endpoints

Protected endpoints require `Authorization: Bearer <MINT_SECRET>`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/.well-known/openid-configuration` | no | OIDC discovery |
| GET | `/jwks.json` | no | public signing key |
| POST | `/mint` | yes | sign a test JWT |
| POST | `/webhook` | no | record a watcher delivery |
| GET | `/deliveries` | yes | list recorded deliveries |
| DELETE | `/deliveries` | yes | clear recorded deliveries |
| PUT | `/fail-mode` | yes | `{"enabled": true}` makes `/webhook` return 500 |

`/mint` body fields (all optional): `sub`, `scope`, `aud`, `iss`, `expires_in` (seconds;
negative yields an already-expired token), `wrong_key: true` (signs with a key absent from
the JWKS, for bad-signature tests).
