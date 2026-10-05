// Native check-only resolver: app aliases plus already-installed packages, never downloads.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '../..');
const installed = process.env.NINEROUTER_TEST_PACKAGES;
const require = installed ? createRequire(path.join(installed, 'package.json')) : null;
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('@/')) return { url: pathToFileURL(path.join(root, 'src', specifier.slice(2))).href, shortCircuit: true };
  if (specifier.startsWith('open-sse/')) return { url: pathToFileURL(path.join(root, specifier)).href, shortCircuit: true };
  try { return await nextResolve(specifier, context); } catch (error) {
    if (!require || specifier.startsWith('.') || specifier.startsWith('/') || specifier.startsWith('node:')) throw error;
    return { url: pathToFileURL(require.resolve(specifier)).href, shortCircuit: true };
  }
}
