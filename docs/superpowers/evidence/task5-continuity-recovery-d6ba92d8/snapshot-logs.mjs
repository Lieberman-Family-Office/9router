// Non-activating existing instance log retrieval. Keep only sanitized storage/lifecycle lines.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const root = path.resolve(process.argv[2]);
const req = createRequire(path.join(root, 'node_modules/@namespacelabs/sdk/package.json'));
const { createComputeClient } = req(root + '/node_modules/@namespacelabs/sdk/dist/cjs/api/compute/client.js');
const client = createComputeClient({ region: 'us' });
const ids = ['c77ffjieqc6h2', 'dia700cgv26vs', '2d9r1uv5bl6au', 'ptg0dv09olrn0', '9d6g9pu89jovg'];
const result = { at: new Date().toISOString(), scope: 'existing specified instances storage/lifecycle log lines only', records: [] };
for (const id of ids) {
  let cursor;
  const seen = new Set();
  const record = { instanceId: id, subjects: 0, lines: [], complete: false };
  try {
    for (let page = 0; page < 20; page++) {
      const response = await client.observability.fetchInstanceLogs({ matchInstanceIds: { values: [id], op: 1 }, linesPerPage: 100, ...(cursor ? { paginationCursor: cursor } : {}) }, { timeoutMs: 30000 });
      record.subjects += response.logLine.length;
      record.retentionDays = response.retentionDays;
      for (const line of response.logLine) {
        if (!/snapshot|volume|mount|apfs|persist|restore|shutdown|stop|error|fail/i.test(line.content)) continue;
        if (/token|password|secret|credential|authorization|cookie|bearer|api.?key/i.test(line.content)) continue;
        record.lines.push({ timestamp: line.timestamp ? new Date(Number(line.timestamp.seconds) * 1000 + line.timestamp.nanos / 1000000).toISOString() : null, content: line.content.slice(0, 1500).replace(/(?:nsct_|ghp_|sk-)[A-Za-z0-9_.-]+/g, '[REDACTED]'), source: line.source, stream: line.stream });
      }
      cursor = response.paginationCursor;
      if (!cursor.length) { record.complete = true; break; }
      const key = Buffer.from(cursor).toString('base64');
      if (seen.has(key)) { record.failure = 'pagination repeated'; break; }
      seen.add(key);
    }
  } catch (error) { record.failure = { code: error.code, message: error.message }; }
  result.records.push(record);
}
fs.writeFileSync(path.resolve(process.argv[3]), JSON.stringify(result, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
console.log(JSON.stringify({ records: result.records.map(({ instanceId, subjects, complete, failure, lines }) => ({ instanceId, subjects, complete, failure, relevantLines: lines.length })) }));
