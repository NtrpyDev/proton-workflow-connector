// Disposable OIDC issuer + webhook receiver used by scripts/live_acceptance.py.
//
// Endpoints (auth = `Authorization: Bearer <MINT_SECRET>`):
//   GET  /.well-known/openid-configuration   issuer metadata (issuer follows the request origin)
//   GET  /jwks.json                          public keys (real + a decoy kid for negative tests)
//   POST /mint             (auth)            sign a test JWT; body controls claims, see below
//   POST /webhook                            record a watcher delivery; honors fail mode
//   GET  /deliveries       (auth)            recorded deliveries, newest first
//   DELETE /deliveries     (auth)            clear recorded deliveries
//   PUT  /fail-mode        (auth)            {"enabled": true} -> /webhook returns 500 until disabled
//
// /mint body (all optional): sub, scope, aud, iss, expires_in (seconds, negative for an expired
// token), wrong_key (true signs with WRONG_PRIVATE_JWK so the JWKS never validates it).
//
// Secrets: MINT_SECRET, PRIVATE_JWK, WRONG_PRIVATE_JWK (see README.md).

import { DurableObject } from "cloudflare:workers";

const KID = "pwc-test";
const WRONG_KID = "pwc-wrong";

export class WebhookLog extends DurableObject {
  async record(delivery) {
    const key = `d:${Date.now()}:${crypto.randomUUID()}`;
    await this.ctx.storage.put(key, delivery);
    return key;
  }

  async list() {
    const entries = await this.ctx.storage.list({ prefix: "d:", reverse: true, limit: 100 });
    return [...entries.values()];
  }

  async clear() {
    const entries = await this.ctx.storage.list({ prefix: "d:" });
    await this.ctx.storage.delete([...entries.keys()]);
  }

  async setFailMode(enabled) {
    await this.ctx.storage.put("fail-mode", Boolean(enabled));
  }

  async failMode() {
    return (await this.ctx.storage.get("fail-mode")) === true;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      switch (`${request.method} ${url.pathname}`) {
        case "GET /.well-known/openid-configuration":
          return json(discovery(url.origin));
        case "GET /jwks.json":
          return json(await jwks(env));
        case "POST /mint":
          return (await authorized(request, env)) ? mint(request, env, url.origin) : unauthorized();
        case "POST /webhook":
          return webhook(request, env);
        case "GET /deliveries":
          return (await authorized(request, env)) ? json(await log(env).list()) : unauthorized();
        case "DELETE /deliveries":
          if (!(await authorized(request, env))) return unauthorized();
          await log(env).clear();
          return json({ cleared: true });
        case "PUT /fail-mode": {
          if (!(await authorized(request, env))) return unauthorized();
          const body = await request.json();
          await log(env).setFailMode(body.enabled);
          return json({ fail_mode: Boolean(body.enabled) });
        }
        default:
          return json({ error: "not found" }, 404);
      }
    } catch (error) {
      console.log(JSON.stringify({ level: "error", path: url.pathname, message: String(error) }));
      return json({ error: "internal error" }, 500);
    }
  },
};

function log(env) {
  return env.WEBHOOK_LOG.get(env.WEBHOOK_LOG.idFromName("log"));
}

function discovery(origin) {
  return {
    issuer: origin,
    jwks_uri: `${origin}/jwks.json`,
    response_types_supported: ["token"],
    subject_types_supported: ["public"],
    id_token_signing_alg_values_supported: ["RS256"],
  };
}

async function jwks(env) {
  return { keys: [publicJwk(JSON.parse(env.PRIVATE_JWK), KID)] };
}

function publicJwk(privateJwk, kid) {
  const { kty, n, e } = privateJwk;
  return { kty, n, e, kid, use: "sig", alg: "RS256" };
}

async function mint(request, env, origin) {
  const body = await request.json().catch(() => ({}));
  const wrongKey = body.wrong_key === true;
  const jwk = JSON.parse(wrongKey ? env.WRONG_PRIVATE_JWK : env.PRIVATE_JWK);
  const key = await crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const now = Math.floor(Date.now() / 1000);
  const expiresIn = Number.isFinite(body.expires_in) ? body.expires_in : 600;
  const header = { alg: "RS256", typ: "JWT", kid: wrongKey ? WRONG_KID : KID };
  const payload = {
    iss: body.iss ?? origin,
    sub: body.sub ?? "pwc-live-test",
    aud: body.aud ?? "unset-audience",
    iat: now,
    exp: now + expiresIn,
    ...(body.scope !== undefined ? { scope: body.scope } : {}),
  };
  const signingInput = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(signingInput),
  );
  return json({ access_token: `${signingInput}.${b64url(signature)}`, claims: payload });
}

async function webhook(request, env) {
  const stub = log(env);
  const body = await request.text();
  const headers = {};
  for (const [name, value] of request.headers) {
    if (name.toLowerCase().startsWith("x-proton-") || name.toLowerCase() === "content-type") {
      headers[name.toLowerCase()] = value;
    }
  }
  if (await stub.failMode()) {
    await stub.record({ received_at: new Date().toISOString(), headers, body, rejected: true });
    return json({ error: "fail mode enabled" }, 500);
  }
  await stub.record({ received_at: new Date().toISOString(), headers, body, rejected: false });
  return json({ ok: true });
}

async function authorized(request, env) {
  const header = request.headers.get("authorization") ?? "";
  const presented = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!env.MINT_SECRET || !presented) return false;
  const [a, b] = await Promise.all([sha256(presented), sha256(env.MINT_SECRET)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

function unauthorized() {
  return json({ error: "unauthorized" }, 401);
}

async function sha256(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

function b64url(data) {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : new Uint8Array(data);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value, null, 2), {
    status,
    headers: { "content-type": "application/json" },
  });
}
