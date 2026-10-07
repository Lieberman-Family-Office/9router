import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import managed from '../../src/lib/db/managed.cjs';
import { TABLES, buildCreateTableSql } from '../../src/lib/db/schema.js';

let root, db;
const saved = { ...process.env };
beforeEach(async () => {
  root = fs.mkdtempSync(path.join(os.homedir(), '.9r-review-'));
  const directory = path.join(root, 'db');
  fs.mkdirSync(directory, { mode: 0o700 });
  const file = path.join(directory, 'data.sqlite');
  fs.writeFileSync(file, '', { mode: 0o600 });
  db = new DatabaseSync(file);
  for (const [name, definition] of Object.entries(TABLES)) {
    db.exec(buildCreateTableSql(name, definition));
    for (const index of definition.indexes || []) db.exec(index);
  }
  db.exec("INSERT INTO _meta VALUES('schemaVersion','1'),('backupSchemaVersion','1')");
  db.prepare('INSERT INTO providerConnections VALUES(?,?,?,?,?,?,?,?,?,?)').run(
    'fake-id', 'github', 'oauth', 'before', 'fake@example.invalid', 1, 1,
    JSON.stringify({ accessToken: 'fake-old-access', refreshToken: 'fake-old-refresh', providerSpecificData: {
      copilotToken: 'fake-old-copilot', copilotTokenExpiresAt: 1, usage: 7,
    } }), 'now', 'now');
  const receipt = path.join(directory, 'enrolled.json');
  fs.writeFileSync(receipt, JSON.stringify(await managed.createManifest(path.resolve(import.meta.dirname, '../..'))), { mode: 0o600 });
  const refresh = path.join(directory, 'refresh.sqlite');
  managed.enrollRefreshStore(refresh);
  Object.assign(process.env, { DATA_DIR: root, NINEROUTER_MANAGED_WORKER: '1',
    NINEROUTER_HOTSWAP_MANIFEST: receipt, NINEROUTER_HOTSWAP_ENROLLED_MANIFEST: receipt,
    NINEROUTER_HOTSWAP_REFRESH_DB: refresh });
  delete global._dbAdapter;
  vi.resetModules();
  vi.doMock('next/server', () => ({ NextResponse: { json: (body, init) => Response.json(body, init) } }));
  vi.doMock('@/lib/network/connectionProxy', () => ({ resolveConnectionProxyConfig: async () => ({}) }));
});
afterEach(() => {
  global._dbAdapter?.instance?.close();
  delete global._dbAdapter;
  db?.close();
  fs.rmSync(root, { recursive: true, force: true });
  for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
  Object.assign(process.env, saved);
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.doUnmock('open-sse/index.js');
  vi.doUnmock('open-sse/executors/index.js');
  vi.doUnmock('dns');
  vi.doUnmock('open-sse/services/usage.js');
  vi.doUnmock('@/lib/network/connectionProxy');
  vi.doUnmock('next/server');
});

it.each(['example.invalid', 'cloudcode-pa.googleapis.com'])('single-attempt managed transport refuses accepted-but-response-lost replay for %s', async host => {
  const resolver = vi.fn(function () { return { setServers() {}, resolve4(_host, callback) { callback(new Error('isolated DNS refusal')); } }; });
  vi.doMock('dns', () => ({ Resolver: resolver }));
  const issuer = vi.fn(async () => { throw new Error('accepted; response lost'); });
  vi.stubGlobal('fetch', issuer);
  const { proxyAwareFetch } = await import('../../open-sse/utils/proxyFetch.js');
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  await expect(dedupRefresh('fake-provider', host, () => proxyAwareFetch(`https://${host}/token`,
    { method: 'POST', body: 'fake-body' }, { connectionProxyEnabled: true, connectionProxyUrl: 'http://127.0.0.1:1' })))
    .rejects.toThrow('Uncertain');
  expect(issuer).toHaveBeenCalledTimes(1);
  expect(resolver).not.toHaveBeenCalled();
});

it('repeatable Copilot exchange replaces only expired completed results; OAuth remains durable', async () => {
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  const expiry = Math.floor(Date.now() / 1000) + 2;
  const issuer = vi.fn(async () => ({ token: 'fake-copilot', expiresAt: expiry }));
  await dedupRefresh('copilot', 'fake-gh', issuer);
  await dedupRefresh('copilot', 'fake-gh', issuer);
  expect(issuer).toHaveBeenCalledTimes(1);
  vi.spyOn(Date, 'now').mockReturnValue((expiry + 1) * 1000);
  const next = vi.fn(async () => ({ token: 'fake-new', expiresAt: expiry + 3600 }));
  const renewed = await Promise.all([dedupRefresh('copilot', 'fake-gh', next), dedupRefresh('copilot', 'fake-gh', next)]);
  expect(next).toHaveBeenCalledTimes(1);
  expect(renewed[0]).toEqual(renewed[1]);
  const rotating = vi.fn(async () => ({ accessToken: 'fake-oauth', expiresIn: 1 }));
  const first = await dedupRefresh('github', 'fake-rotating', rotating);
  vi.spyOn(Date, 'now').mockReturnValue((expiry + 7200) * 1000);
  expect(await dedupRefresh('github', 'fake-rotating', rotating)).toEqual(first);
  expect(rotating).toHaveBeenCalledTimes(1);
});

it('uncertain and pending Copilot exchanges are never reclaimed', async () => {
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  const issuer = vi.fn(async () => { throw new Error('lost'); });
  await expect(dedupRefresh('copilot', 'fake-uncertain', issuer)).rejects.toThrow('Uncertain');
  await expect(dedupRefresh('copilot', 'fake-uncertain', issuer)).rejects.toThrow('Uncertain');
  expect(issuer).toHaveBeenCalledTimes(1);
  let release;
  const pending = dedupRefresh('copilot', 'fake-pending', () => new Promise(resolve => { release = resolve; }));
  process.env.NINEROUTER_HOTSWAP_REFRESH_WAIT_MS = '1';
  await expect(dedupRefresh('copilot', 'fake-pending', issuer)).rejects.toThrow('unresolved');
  expect(issuer).toHaveBeenCalledTimes(1);
  release({ token: 'fake-done', expiresAt: Math.floor(Date.now() / 1000) + 3600 });
  await pending;
});

it('usage real GET refreshes without an enabled proxy and persists Copilot generation', async () => {
  const issuer = vi.fn(async () => ({ ok: true, json: async () => ({ token: 'fake-new-copilot', expires_at: 1900000000 }) }));
  vi.stubGlobal('fetch', issuer);
  const { GithubExecutor } = await import('../../open-sse/executors/github.js');
  vi.doMock('open-sse/index.js', () => ({}));
  vi.doMock('open-sse/executors/index.js', () => ({ getExecutor: () => new GithubExecutor() }));
  vi.doMock('open-sse/services/usage.js', () => ({ getUsageForProvider: async () => ({ used: 1 }) }));
  const { GET } = await import('@/app/api/usage/[connectionId]/route.js');
  const response = await GET(new Request('http://localhost/api/usage/fake-id'), { params: Promise.resolve({ connectionId: 'fake-id' }) });
  expect(response.status).toBe(200);
  expect(issuer).toHaveBeenCalledTimes(1);
  const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  const row = await getProviderConnectionById('fake-id');
  expect(row.providerSpecificData.copilotToken).toBe('fake-new-copilot');
  expect(row.refreshGenerations.copilot).toBeGreaterThan(0);
});

it('translator actual POST carries family generations to real repository and keeps fixed expiry', async () => {
  const expiry = '2030-01-01T00:00:00.000Z';
  const execute = vi.fn().mockResolvedValueOnce({ response: new Response('', { status: 401 }) })
    .mockResolvedValueOnce({ response: new Response('data: done\n\n') });
  vi.doMock('open-sse/index.js', () => ({ getExecutor: () => ({ execute,
    refreshCredentials: async () => ({ accessToken: 'fake-new', refreshToken: 'fake-rotate',
      expiresIn: 3600, expiresAt: expiry, refreshGenerations: { oauth: 1 } }),
  }) }));
  const { POST } = await import('@/app/api/translator/send/route.js');
  const response = await POST(new Request('http://localhost/api/translator/send', { method: 'POST',
    body: JSON.stringify({ provider: 'github', model: 'fake', body: {} }) }));
  expect(response.status).toBe(200);
  const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  const row = await getProviderConnectionById('fake-id');
  expect(row.refreshGenerations).toEqual({ oauth: 1 });
  expect(row.expiresAt).toBe(expiry);
});

it('real Kiro managed refresh returns the supported OAuth family', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ accessToken: 'fake-kiro', refreshToken: 'fake-kiro-rotate', expiresIn: 3600 }) })));
  const { refreshKiroToken } = await import('../../open-sse/services/tokenRefresh.js');
  const result = await refreshKiroToken('fake-kiro-old', { authMethod: 'social', profileArn: 'fake-profile' });
  expect(Object.keys(result.refreshGenerations)).toEqual(['oauth']);
  expect(result.refreshGenerations.oauth).toBeGreaterThan(0);
});

it('translator retains ephemeral Vertex service-account tokens without CAS persistence', async () => {
  const execute = vi.fn().mockResolvedValueOnce({ response: new Response('', { status: 401 }) })
    .mockResolvedValueOnce({ response: new Response('data: done\n\n') });
  const account = JSON.stringify({ type: 'service_account', client_email: 'fake@example.invalid', private_key: 'fake', project_id: 'fake' });
  db.prepare('UPDATE providerConnections SET provider=?,data=? WHERE id=?').run('vertex', JSON.stringify({ apiKey: account }), 'fake-id');
  vi.doMock('open-sse/index.js', () => ({ getExecutor: () => ({ execute,
    refreshCredentials: async () => ({ accessToken: 'fake-ephemeral', expiresAt: Date.now() + 3600_000 }),
  }) }));
  const { POST } = await import('@/app/api/translator/send/route.js');
  const response = await POST(new Request('http://localhost/api/translator/send', { method: 'POST',
    body: JSON.stringify({ provider: 'vertex', model: 'fake', body: {} }) }));
  expect(response.status).toBe(200);
  expect(execute).toHaveBeenCalledTimes(2);
  expect(execute.mock.calls[1][0].credentials.accessToken).toBe('fake-ephemeral');
  const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  expect((await getProviderConnectionById('fake-id')).accessToken).toBeUndefined();
});

it('provider actual PUT preserves noncredential edits and refuses stale credential input', async () => {
  const { PUT } = await import('@/app/api/providers/[id]/route.js');
  const call = body => PUT(new Request('http://localhost/api/providers/fake-id', { method: 'PUT', body: JSON.stringify(body) }),
    { params: Promise.resolve({ id: 'fake-id' }) });
  expect((await call({ name: 'after' })).status).toBe(200);
  expect((await call({ providerSpecificData: { copilotToken: 'fake-stale' } })).status).toBe(400);
  expect((await call({ apiKey: 'fake-new-key' })).status).toBe(400);
  expect((await call({ providerSpecificData: { usage: 8 } })).status).toBe(200);
  expect((await call({ connectionProxyEnabled: false, connectionProxyUrl: '', proxyPoolId: null })).status).toBe(200);
  const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  const row = await getProviderConnectionById('fake-id');
  expect(row.name).toBe('after');
  expect(row.providerSpecificData.copilotToken).toBe('fake-old-copilot');
  expect(row.providerSpecificData.usage).toBe(8);
});

it('Xiaomi browser status mints a bound proof only after provider verification', async () => {
  let verified = false;
  vi.doMock('@/lib/mimoLoginSession', () => ({
    sessionFromRequest: () => ({ state: 'fixture-state', status: 'done', region: 'sgp' }),
    readSessionIdentity: () => ({ userId: 'fixture', passToken: 'fake-after' }),
    ensureServiceSession: async () => verified,
    attachSessionCookie: response => response,
  }));
  vi.doMock('next/server', () => ({ NextResponse: { json: (body, init) => {
    const response = Response.json(body, init); response.cookies = { set(name, value) { response.headers.append('Set-Cookie', `${name}=${value}`); } }; return response;
  } } }));
  try {
    const { GET } = await import('@/app/api/oauth/xiaomi-mimo/login/status/route.js');
    const request = new Request('http://localhost/api/oauth/xiaomi-mimo/login/status?state=fixture-state');
    expect((await GET(request)).status).toBe(400);
    verified = true;
    const response = await GET(request);
    expect(response.status).toBe(200);
    const payload = await response.json();
    expect(payload.reauthorizationProof).toBeUndefined();
    const token = response.headers.getSetCookie().find(cookie => cookie.startsWith('9r_mimo_reauth=')).slice('9r_mimo_reauth='.length);
    const { getDashboardAuthSession } = await import('@/lib/auth/dashboardSession');
    const proof = await getDashboardAuthSession(token);
    expect(proof).toMatchObject({ purpose: 'xiaomi-reauthorization', userId: 'fixture', region: 'sgp' });
    expect(proof.reauthorizationExpiresAt).toBeGreaterThan(Date.now());
  } finally { vi.doUnmock('@/lib/mimoLoginSession'); }
});

it('signed Xiaomi reauthorization uses real issuance generation and rejects a tampered proof', async () => {
  db.prepare('UPDATE providerConnections SET provider=?,email=?,data=? WHERE id=?')
    .run('xiaomi-mimo', 'fixture@xiaomi', JSON.stringify({ providerSpecificData: { mimoPassToken: 'fake-before', usage: 7 } }), 'fake-id');
  vi.doMock('@/models', () => ({
    getProviderConnections: async () => (await import('@/lib/db/repos/connectionsRepo.js')).getProviderConnections(),
    createProviderConnection: async (...args) => (await import('@/lib/db/repos/connectionsRepo.js')).createProviderConnection(...args),
    updateProviderConnection: async (...args) => (await import('@/lib/db/repos/connectionsRepo.js')).updateProviderConnection(...args),
  }));
  try {
    const { createHash } = await import('node:crypto');
    const { createDashboardAuthToken } = await import('@/lib/auth/dashboardSession');
    const proof = await createDashboardAuthToken({ purpose: 'xiaomi-reauthorization', userId: 'fixture', region: 'sgp',
      reauthorizationExpiresAt: Date.now() + 60000, credentialSha256: createHash('sha256').update('fake-after').digest('hex') });
    const { POST } = await import('@/app/api/oauth/xiaomi-mimo/api-key/route.js');
    const call = reauthorizationProof => POST(new Request('http://localhost/api/oauth/xiaomi-mimo/api-key', { method: 'POST',
      headers: { cookie: `9r_mimo_reauth=${reauthorizationProof}` },
      body: JSON.stringify({ uid: 'fixture', mimoUserId: 'fixture', mimoPassToken: 'fake-after', region: 'sgp' }) }));
    expect((await call(proof + 'tampered')).status).toBe(409);
    expect((await call(proof)).status).toBe(200);
    const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
    const row = await getProviderConnectionById('fake-id');
    expect(row.providerSpecificData.mimoPassToken).toBe('fake-after');
    expect(row.providerSpecificData.usage).toBe(7);
    expect(row.refreshGenerations.oauth).toBeGreaterThan(0);
  } finally { vi.doUnmock('@/models'); }
});

it('provider-node metadata update does not replay credential snapshots', async () => {
  vi.doMock('@/models', () => ({
    getProviderNodeById: async () => ({ type: 'openai-compatible' }),
    updateProviderNode: async (_id, patch) => patch,
    getProviderConnections: async () => [{ id: 'fake-id', providerSpecificData: { copilotToken: 'fake-old-copilot' } }],
    updateProviderConnection: async (...args) => (await import('@/lib/db/repos/connectionsRepo.js')).updateProviderConnection(...args),
    deleteProviderNode: async () => {}, deleteProviderConnectionsByProvider: async () => {},
  }));
  try {
    const { PUT } = await import('@/app/api/provider-nodes/[id]/route.js');
    const response = await PUT(new Request('http://localhost/api/provider-nodes/fake-node', { method: 'PUT',
      body: JSON.stringify({ name: 'metadata', prefix: 'node', apiType: 'chat', baseUrl: 'https://example.invalid' }) }),
      { params: Promise.resolve({ id: 'fake-node' }) });
    expect(response.status).toBe(200);
    const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
    const row = await getProviderConnectionById('fake-id');
    expect(row.providerSpecificData.copilotToken).toBe('fake-old-copilot');
    expect(row.providerSpecificData.nodeName).toBe('metadata');
  } finally { vi.doUnmock('@/models'); }
});

it('reauth fences pending old refresh and its delayed callback without blocking the new grant', async () => {
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  const { createProviderConnection, getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
  let release;
  const pending = dedupRefresh('github', 'fake-old-refresh', () => new Promise(resolve => { release = resolve; }));
  let releaseCopilot;
  const pendingCopilot = dedupRefresh('copilot', 'fake-old-access', () => new Promise(resolve => { releaseCopilot = resolve; }));
  await createProviderConnection({ provider: 'github', authType: 'oauth', email: 'fake@example.invalid',
    accessToken: 'fake-reauth', refreshToken: 'fake-reauth-refresh' });
  release({ accessToken: 'fake-delayed', refreshToken: 'fake-delayed-rotate', expiresIn: 3600 });
  await updateProviderCredentials('fake-id', await pending);
  releaseCopilot({ token: 'fake-delayed-copilot', expiresAt: 1900000000 });
  const staleCopilot = await pendingCopilot;
  await updateProviderCredentials('fake-id', { copilotToken: staleCopilot.token,
    copilotTokenExpiresAt: staleCopilot.expiresAt, refreshGenerations: staleCopilot.refreshGenerations });
  expect((await getProviderConnectionById('fake-id')).accessToken).toBe('fake-reauth');
  expect((await getProviderConnectionById('fake-id')).providerSpecificData.copilotToken).toBeUndefined();
  const fresh = await dedupRefresh('github', 'fake-reauth-refresh', async () => ({ accessToken: 'fake-next', refreshToken: 'fake-next-rotate' }));
  await updateProviderCredentials('fake-id', fresh);
  expect((await getProviderConnectionById('fake-id')).accessToken).toBe('fake-next');
});

it('managed persistence ignores undefined credential fields but keeps intentional empty strings', async () => {
  const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
  const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  await updateProviderCredentials('fake-id', { token: undefined, scope: '', refreshGenerations: { oauth: 1 } });
  const row = await getProviderConnectionById('fake-id');
  expect(row.accessToken).toBe('fake-old-access');
  expect(row.token).toBeUndefined();
  expect(row.scope).toBe('');
});

it('corrupt durable OAuth expiry is refused even without expiresIn', async () => {
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  await dedupRefresh('github', 'fake-corrupt', async () => ({ accessToken: 'fake-token' }));
  const store = managed.openRefreshStore(process.env.NINEROUTER_HOTSWAP_REFRESH_DB);
  try {
    const row = store.prepare("SELECT key,result FROM refresh_flights WHERE state='done'").get();
    const result = { ...JSON.parse(row.result), expiresAt: 'not-an-expiry' };
    store.prepare('UPDATE refresh_flights SET result=? WHERE key=?').run(JSON.stringify(result), row.key);
  } finally { store.close(); }
  const issuer = vi.fn();
  await expect(dedupRefresh('github', 'fake-corrupt', issuer)).rejects.toThrow('Invalid durable refresh result');
  expect(issuer).not.toHaveBeenCalled();
});

it('durable completion fixes expiry before delayed persistence and replay', async () => {
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
  const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
  const start = Date.now();
  const result = await dedupRefresh('github', 'fake-expiry', async () => ({ accessToken: 'fake-fixed', expiresIn: 3600 }));
  expect(Date.parse(result.expiresAt)).toBeGreaterThanOrEqual(start + 3600_000);
  const fixed = result.expiresAt;
  vi.spyOn(Date, 'now').mockReturnValue(start + 1800_000);
  const replay = await dedupRefresh('github', 'fake-expiry', () => { throw new Error('must not issue'); });
  const { mergeRefreshedCredentials } = await import('../../open-sse/services/oauthCredentialManager.js');
  expect(mergeRefreshedCredentials('github', {}, replay, start + 1800_000).expiresAt).toBe(fixed);
  await updateProviderCredentials('fake-id', replay);
  expect((await getProviderConnectionById('fake-id')).expiresAt).toBe(fixed);
});
