// Host-only Namespace orchestration/metadata. Create is explicitly stopped and called once.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const [sdkRoot, action, ref, output] = process.argv.slice(2);
const req = createRequire(path.join(path.resolve(sdkRoot), 'node_modules/@namespacelabs/sdk/package.json'));
const prefix = path.join(path.resolve(sdkRoot), 'node_modules/@namespacelabs/sdk/dist/cjs');
const { createClient } = req('@connectrpc/connect');
const { createConnectTransport } = req('@connectrpc/connect-node');
const { bearerAuthInterceptor } = req(prefix + '/api/interceptors.js');
const { loadDefaults } = req(prefix + '/auth/token.js');
const { DevBoxService } = req(prefix + '/proto/namespace/private/devbox/devbox_pb.js');
const { createComputeClient } = req(prefix + '/api/compute/client.js');
const rpc = createClient(DevBoxService, createConnectTransport({ httpVersion: '1.1', baseUrl: process.env.NSC_DEVBOX_ENDPOINT || 'https://private-api.iad.namespaceapis.com', useBinaryFormat: false, interceptors: [bearerAuthInterceptor(loadDefaults)] }));
const opts = { timeoutMs: 60000 };
const clean = value => JSON.parse(JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? String(item) : item));
const project = response => {
  const b = response.devbox;
  return clean({ id: b.id, name: b.name, state: response.instanceId ? 'running' : 'stopped', instance_id: response.instanceId || null, os: b.instanceShape.os, architecture: b.instanceShape.machineArch, shape: b.instanceShape, site: b.site, creator: b.creator, volumeName: b.volumeName, volumeSizeGb: b.volumeSizeGb, accessMode: b.accessMode, ephemeral: b.ephemeral || null, versionControl: b.versionControl, repository: b.repository, spec: b.spec ? { workspaceDir: b.spec.workspaceDir, wholeSystemPersistency: b.spec.wholeSystemPersistency, onStartupCount: b.spec.onStartup.length, onCreateCount: b.spec.onCreate.length, environmentNames: b.spec.environment.map(item => item.name), volumes: b.spec.volumes } : null, blueprintRef: b.blueprintRef, imageRef: b.imageRef, integrationsFields: b.integrations ? Object.keys(b.integrations) : [] });
};
let result;
if (action === 'create') {
  if (fs.existsSync(output)) throw new Error('Refuse existing creation receipt');
  // Empty versionControl overrides workspace checkout. No template, snapshot, dotfiles or integration is supplied.
  const request = { name: ref, activate: false, accessMode: 1, site: 'iad', volumeSizeGb: 300n, versionControl: { repositories: [] }, instanceShape: { virtualCpu: 6, memoryMegabytes: 14336, machineArch: 'arm64', os: 'macos', selectors: [{ name: 'macos.version', value: '26.x' }, { name: 'macos.purpose', value: 'githubrunner' }, { name: 'image.with', value: 'xcode-latest' }] }, features: { enabled: [] }, documentedPurpose: 'Task5 fresh 300 GiB persistence and exact-package qualification; independent guest credentials only' };
  result = { request: clean(request), returned: project(await rpc.create(request, opts)), at: new Date().toISOString() };
} else if (action === 'metadata') {
  result = project(await rpc.fetch({ idOrName: ref, includeSshCredentials: false, returnActivatedInstance: true }, opts));
} else if (action === 'lineage') {
  const response = await rpc.fetch({ id: ref, includeSshCredentials: false, returnActivatedInstance: true }, opts);
  const b = response.devbox;
  const listed = await rpc.listSnapshots({ idOrName: ref, orderBy: 1 }, opts);
  result = { metadata: project(response), snapshots: clean(listed.snapshots), at: new Date().toISOString() };
  if (response.instanceId) {
    const compute = createComputeClient({ region: 'us' });
    const instance = await compute.compute.describeInstance({ instanceId: response.instanceId }, opts);
    result.instance = { metadata: clean(instance.metadata), shutdownReasons: clean(instance.shutdownReasons), attachments: instance.attachments.map(item => {
      if (item.typeUrl !== 'namespacelabs.dev/internal/volume/persistent/wal-snapshot') return { typeUrl: item.typeUrl };
      try { return { typeUrl: item.typeUrl, snapshot: JSON.parse(Buffer.from(item.content).toString()) }; } catch { return { typeUrl: item.typeUrl, encoding: 'unknown' }; }
    }) };
  }
} else throw new Error('Unknown permitted API action');
if (output) fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
console.log(JSON.stringify(result));
