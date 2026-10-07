// Read-only API evidence. Never activate compute or request SSH credentials.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const root = path.resolve(process.argv[2]);
const output = path.resolve(process.argv[3]);
const req = createRequire(path.join(root, 'node_modules/@namespacelabs/sdk/package.json'));
const { createClient } = req('@connectrpc/connect');
const sdk = root + '/node_modules/@namespacelabs/sdk/dist/cjs';
const { createConnectTransport } = req('@connectrpc/connect-node');
const { bearerAuthInterceptor } = req(sdk + '/api/interceptors.js');
const { loadDefaults } = req(sdk + '/auth/token.js');
const { DevBoxService } = req(sdk + '/proto/namespace/private/devbox/devbox_pb.js');
const { createComputeClient } = req(sdk + '/api/compute/client.js');
const transport = createConnectTransport({ httpVersion: '1.1', baseUrl: process.env.NSC_DEVBOX_ENDPOINT || 'https://private-api.iad.namespaceapis.com', useBinaryFormat: false, interceptors: [bearerAuthInterceptor(loadDefaults)] });
const devboxes = createClient(DevBoxService, transport);
const compute = createComputeClient({ region: 'us' });
const options = { timeoutMs: 30000 };
const clean = value => JSON.parse(JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? String(item) : item));
const timestamp = item => item ? new Date(Number(item.seconds) * 1000 + item.nanos / 1000000).toISOString() : null;
const record = { schema: 1, at: new Date().toISOString(), sdkVersion: JSON.parse(fs.readFileSync(path.join(root, 'node_modules/@namespacelabs/sdk/package.json'), 'utf8')).version, devboxId: '2tc0b2eg4mveo', scope: 'non-activating API devbox, volume and snapshot lineage; no file-byte verdict', calls: [] };
async function call(name, action, project) {
  try { const value = await action(); record.calls.push({ name, success: true }); return project(value); }
  catch (error) { const result = { error: { code: error.code, name: error.name, message: error.message } }; record.calls.push({ name, success: false, ...result }); return result; }
}
record.devbox = await call('DevBoxService.Fetch', () => devboxes.fetch({ id: record.devboxId, includeSshCredentials: false, returnActivatedInstance: true }, options), response => {
  const b = response.devbox;
  return clean({ id: b.id, name: b.name, site: b.site, activeInstanceId: response.instanceId || null, volumeName: b.volumeName, volumeSizeGb: b.volumeSizeGb, ephemeral: b.ephemeral || null, workspaceDir: b.workspaceDir, defaultDir: b.defaultDir, blueprintRef: b.blueprintRef, enabledFeatures: b.enabledFeatures, privateFeatures: b.privateFeatures, poolLease: b.poolLease, spec: b.spec ? { wholeSystemPersistency: b.spec.wholeSystemPersistency, workspaceDir: b.spec.workspaceDir, volumes: b.spec.volumes, onCreateCount: b.spec.onCreate.length, onStartup: b.spec.onStartup, environmentNames: b.spec.environment.map(item => item.name), cacheVolumeTag: b.spec.cacheVolumeTag, sessionsCount: b.spec.sessions.length } : null, fieldNames: Object.keys(b) });
});
const snapshotProjection = value => ({ ...clean(value), createdAtIso: timestamp(value.createdAt), releasedAtIso: timestamp(value.releasedAt), abandonedAtIso: timestamp(value.abandonedAt) });
record.devboxSnapshots = await call('DevBoxService.ListSnapshots', () => devboxes.listSnapshots({ idOrName: record.devboxId, orderBy: 1 }, options), response => ({ count: response.snapshots.length, snapshots: response.snapshots.map(snapshotProjection) }));
const tag = record.devbox.volumeName;
if (tag) {
  record.volume = await call('StorageService.DescribePersistentVolume', () => compute.storage.describePersistentVolume({ tag }, options), response => clean(response.volume));
  record.volumeSnapshots = await call('StorageService.ListPersistentVolumeSnapshots', () => compute.storage.listPersistentVolumeSnapshots({ tag }, options), response => ({ count: response.snapshots.length, snapshots: response.snapshots.map(snapshotProjection) }));
  record.taggedVolumes = await call('StorageService.ListPersistentVolumes', async () => {
    const volumes = [];
    let cursor;
    const seen = new Set();
    for (;;) {
      const response = await compute.storage.listPersistentVolumes({ tag, maxEntries: 10n, ...(cursor ? { paginationCursor: cursor } : {}) }, options);
      volumes.push(...response.volumes);
      cursor = response.paginationCursor;
      if (!cursor.length) break;
      const key = Buffer.from(cursor).toString('base64');
      if (seen.has(key)) throw new Error('Persistent volume pagination repeated; population unknown');
      seen.add(key);
    }
    return volumes;
  }, volumes => ({ count: volumes.length, volumes: clean(volumes) }));
  const actualSite = record.taggedVolumes.volumes?.[0]?.site;
  if (actualSite) {
    record.volumeDetails = [];
    for (const id of [...new Set([record.taggedVolumes.volumes[0].id, '2mn9a3dq2cfno', 'be5cf3us8jpq8', 'ff5ssm93t0f18', 'jquifouvgefno'])]) {
      record.volumeDetails.push({ id, ...await call('StorageService.DescribePersistentVolume:' + id, () => compute.storage.describePersistentVolume({ id, site: actualSite }, options), response => ({ volume: clean(response.volume) })) });
    }
  }
}
record.instances = [];
for (const instanceId of [...new Set(['c77ffjieqc6h2', 'dia700cgv26vs', '4q7uuv7ae1uk2', '2d9r1uv5bl6au', 'oui3ubpjdu7ns', ...String(process.argv[4] || '').split(',')].filter(Boolean))]) {
  record.instances.push({ instanceId, ...await call('ComputeService.DescribeInstance:' + instanceId, () => compute.compute.describeInstance({ instanceId }, options), response => ({ metadata: clean(response.metadata), shutdownReasons: clean(response.shutdownReasons), attachmentDescriptions: response.attachments.map(item => {
  const description = { typeUrl: item.typeUrl, contentBytes: item.content?.length, fieldNames: Object.keys(item) };
  if (item.typeUrl === 'namespacelabs.dev/internal/volume/persistent/wal-snapshot') {
    try {
      const content = JSON.parse(Buffer.from(item.content).toString('utf8'));
      description.contentFieldNames = Object.keys(content);
      description.snapshotMetadata = Object.fromEntries(Object.entries(content).filter(([key, value]) => /snapshot|volume|tag|site|id/i.test(key) && (typeof value === 'string' || typeof value === 'number')));
    } catch { description.contentEncoding = 'not-json'; }
  }
  return description;
}), credentialMetadataExcluded: true })) });
}
fs.writeFileSync(output, JSON.stringify(record, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
console.log(JSON.stringify({ evidence: output, calls: record.calls, devbox: { id: record.devbox.id, activeInstanceId: record.devbox.activeInstanceId, volumeName: record.devbox.volumeName }, snapshotCount: record.devboxSnapshots.count }));
