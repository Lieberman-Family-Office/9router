import { afterEach, expect, it, vi } from 'vitest';
import { DefaultExecutor } from '../../open-sse/executors/default.js';
import { GeminiCLIExecutor } from '../../open-sse/executors/gemini-cli.js';
import { mergeRefreshedCredentials, refreshProviderCredentials } from '../../open-sse/services/oauthCredentialManager.js';
vi.mock('@/lib/localDb', () => ({
  getProviderConnectionById: async () => ({ provider: 'claude', authType: 'oauth', accessToken: 'fixture-expired', refreshToken: 'fixture-old', expiresAt: '2000-01-01' }),
  updateProviderConnection: vi.fn(), getSettings: async () => ({}), updateSettings: vi.fn(),
}));
vi.mock('@/lib/network/connectionProxy', () => ({ resolveConnectionProxyConfig: async () => ({}) }));
vi.mock('@/mitm/manager', () => ({ initDbHooks() {}, getCachedPassword() {}, loadEncryptedPassword() {} }));
const xiaomiWrite = vi.hoisted(() => vi.fn());
vi.mock('@/models', () => ({
  createProviderConnection: xiaomiWrite, updateProviderConnection: xiaomiWrite,
  getProviderConnections: async () => [{ id: 'fixture-xiaomi', provider: 'xiaomi-mimo', email: 'fixture@xiaomi' }],
}));
vi.mock('next/server', () => ({ NextResponse: { json: (body, init) => Response.json(body, init) } }));

vi.mock('open-sse/services/oauthCredentialManager.js', async original => ({
  ...await original(), refreshProviderCredentials: vi.fn(async () => ({ accessToken: 'fixture-new' })),
}));
afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('managed Xiaomi credential edits refuse before persistence', async () => {
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ data: [] })));
  const { POST } = await import('@/app/api/oauth/xiaomi-mimo/api-key/route.js');
  const response = await POST(new Request('http://localhost/api/oauth/xiaomi-mimo/api-key', {
    method: 'POST', body: JSON.stringify({ apiKey: 'sk-fixture-key', uid: 'fixture' }),
  }));
  expect(response.status).toBe(409);
  expect(xiaomiWrite).not.toHaveBeenCalled();
});

it('managed connection test does not mark a refresh error as active', async () => {
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
  refreshProviderCredentials.mockResolvedValueOnce({ error: 'invalid_grant' });
  const { testSingleConnection } = await import('@/app/api/providers/[id]/test/testUtils.js');
  const result = await testSingleConnection('fixture-id');
  expect(result.valid).toBe(false);
  expect(result.refreshed).toBe(false);
});

it('managed workers reject tunnel start operations before service side effects', async () => {
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
  const { enableTunnel } = await import('@/lib/tunnel/cloudflare/manager.js');
  const { enableTailscale } = await import('@/lib/tunnel/tailscale/manager.js');
  const { startFunnel } = await import('@/lib/tunnel/tailscale/tailscale.js');
  for (const start of [enableTunnel, enableTailscale, startFunnel]) {
    await expect(start(20128)).rejects.toThrow('Managed workers cannot start app-owned ingress');
  }
});

it('managed default refresh rejects configured proxy before issuing a token request', async () => {
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
  const executor = new DefaultExecutor('openai');
  await expect(executor.refreshCredentials({ refreshToken: 'fixture-old' }, null,
    { connectionProxyEnabled: true, connectionProxyUrl: 'http://127.0.0.1:1' })).rejects.toThrow(/proxy/i);
  expect(fetch).not.toHaveBeenCalled();
});

it('managed Kiro retry retains configuration omitted from partial refresh', () => {
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
  const current = { providerSpecificData: { region: 'fixture-region', clientId: 'fixture-client', authMethod: 'social' } };
  const next = mergeRefreshedCredentials('kiro', current,
    { accessToken: 'fixture-new', providerSpecificData: { profileArn: 'fixture-arn' }, refreshGenerations: { oauth: 1 } });
  expect(next.providerSpecificData).toEqual({ ...current.providerSpecificData, profileArn: 'fixture-arn' });
});

it('managed Gemini refresh keeps the current project when the patch omits it', async () => {
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
  const executor = new GeminiCLIExecutor();
  const result = await executor.refreshCredentials({ refreshToken: 'fixture-old', projectId: 'fixture-project' });
  expect(result.projectId).toBe('fixture-project');
});
