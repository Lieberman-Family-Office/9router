// Native check-only resolver: app aliases plus already-installed packages, never downloads.
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '../..');
const installed = process.env.NINEROUTER_TEST_PACKAGES;
const require = installed ? createRequire(path.join(installed, 'package.json')) : null;
export async function resolve(specifier, context, nextResolve) {
  const alias = specifier.startsWith('@/') ? path.join(root, 'src', specifier.slice(2))
    : specifier.startsWith('open-sse/') ? path.join(root, specifier) : null;
  if (alias) {
    const file = [alias, alias + '.js', path.join(alias, 'index.js')].find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).isFile());
    if (!file) throw new Error(`Unresolved local alias: ${specifier}`);
    return { url: pathToFileURL(file).href, shortCircuit: true };
  }
  try { return await nextResolve(specifier, context); } catch (error) {
    if (!require || specifier.startsWith('.') || specifier.startsWith('/') || specifier.startsWith('node:')) throw error;
    return { url: pathToFileURL(require.resolve(specifier)).href, shortCircuit: true };
  }
}
