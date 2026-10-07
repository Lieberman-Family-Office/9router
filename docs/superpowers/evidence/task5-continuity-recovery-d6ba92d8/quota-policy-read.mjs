// Read-only live policy and revision discovery. Do not request new token privileges.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const root = path.resolve(process.argv[2]);
const req = createRequire(path.join(root, 'node_modules/@namespacelabs/sdk/package.json'));
const prefix = root + '/node_modules/@namespacelabs/sdk/dist/cjs';
const { createIAMClient } = req(prefix + '/api/iam/client.js');
const { loadDefaults } = req(prefix + '/auth/token.js');
const client = createIAMClient({ tokenSource: loadDefaults });
let result;
try {
  const response = await client.tenants.describePolicies({}, { timeoutMs: 30000 });
  const clean = JSON.parse(JSON.stringify(response, (_key, value) => typeof value === 'bigint' ? String(value) : value));
  result = { success: true, revision: String(response.revision), policyPopulation: response.policies.length, policies: clean.policies, scope: 'existing authenticated tenant policies; no write' };
} catch (error) {
  result = { success: false, error: { code: error.code, name: error.name, message: error.message }, scope: 'DescribePolicies refused; no write' };
}
result.authorization = { quote: 'Yes—raise only the per-user limit to 2, preserve the old guest, and retry (Recommended)', turn: '2026-10-07 18:24 EDT namespace_per_user_limit answer' };
result.at = new Date().toISOString();
fs.writeFileSync(path.resolve(process.argv[3]), JSON.stringify(result, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
console.log(JSON.stringify(result.success ? { success: true, revision: result.revision, policyPopulation: result.policyPopulation, policies: result.policies.map(item => ({ id: item.id, policy: item.policy })) } : result));
