// Host-only evidence reduction. No guest commands or API mutations.
import fs from 'node:fs';
import path from 'node:path';
const base = path.dirname(new URL(import.meta.url).pathname);
const evidence = JSON.parse(fs.readFileSync(path.join(base, 'snapshot-metadata-three-path.json'), 'utf8'));
const ts = item => item ? Number(item.seconds) + item.nanos / 1e9 : null;
const runs = [
  ['sign-in', '2d9r1uv5bl6au', 1791406451, false],
  ['nested-checkpoint-create', 'c77ffjieqc6h2', null, false],
  ['nested-checkpoint-read', 'dia700cgv26vs', null, null],
  ['root-workspace-create', 'ptg0dv09olrn0', 1791409881.886009, true],
  ['three-path-create', '9d6g9pu89jovg', 1791410254.639478, true],
];
const records = runs.map(([label, instanceId, observedWriteAt, retained]) => {
  const instance = evidence.instances.find(item => item.instanceId === instanceId);
  const snapshot = evidence.devboxSnapshots.snapshots.find(item => item.attachedInstanceId === instanceId);
  const attachment = instance.attachmentDescriptions.find(item => item.snapshotMetadata)?.snapshotMetadata;
  const metadata = instance.metadata;
  return { label, instanceId, observedWriteAt, retained, hardware: metadata.hwDeployment.majorHwplatform, instanceCreatedAt: ts(metadata.createdAt), instanceReadyAt: ts(metadata.readyAt), instanceDestroyedAt: ts(metadata.destroyedAt), snapshotId: snapshot.id, snapshotParent: snapshot.snapshotFrom, snapshotCompleted: snapshot.completedSnapshot, snapshotCreatedAt: ts(snapshot.createdAt), snapshotReleasedAt: ts(snapshot.releasedAt), snapshotReleaseReason: snapshot.releasedReason, snapshotReleaseMinusReadySeconds: ts(snapshot.releasedAt) - ts(metadata.readyAt), snapshotReleaseMinusDestroyedSeconds: ts(snapshot.releasedAt) - ts(metadata.destroyedAt), snapshotReleaseMinusWriteSeconds: observedWriteAt ? ts(snapshot.releasedAt) - observedWriteAt : null, attachment };
});
const result = { scope: 'five selected existing instances; timestamps are API metadata, not a snapshot byte-content oracle', records, diagnosis: 'Lost-write instances have snapshot released/committed timestamps before instance readiness. Passing instances have release timestamps after instance destruction. Association is measured; vendor metadata semantics and causal mechanism remain unproven.' };
fs.writeFileSync(path.join(base, 'snapshot-lineage-summary.json'), JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
console.log(JSON.stringify(result));
