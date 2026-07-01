// Generate the two RSA private JWKs the fixture Worker needs, as one-line JSON for
// `wrangler secret put`. Run: node generate-jwk.mjs
import { generateKeyPair } from "node:crypto";
import { promisify } from "node:util";

const generate = promisify(generateKeyPair);

for (const name of ["PRIVATE_JWK", "WRONG_PRIVATE_JWK"]) {
  const { privateKey } = await generate("rsa", { modulusLength: 2048 });
  const jwk = privateKey.export({ format: "jwk" });
  console.log(`${name}:`);
  console.log(JSON.stringify(jwk));
  console.log();
}
console.log("MINT_SECRET (random):");
console.log(
  Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url"),
);
