// Shared by the build and managed runtime. No provider calls or enrollment side effects on import.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { pathToFileURL } = require('node:url');

function privateDirectory(directory) {
  if (!path.isAbsolute(directory)) throw new Error('Managed path must be absolute');
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || stat.uid !== process.getuid() ||
      (stat.mode & 0o777) !== 0o700) throw new Error('Unsafe managed directory');
  // macOS exposes /tmp and /var through root-owned system symlinks.
  let ancestor = path.dirname(directory);
  while (ancestor !== path.dirname(ancestor)) {
    const parent = fs.lstatSync(ancestor);
    if (parent.isSymbolicLink()) {
      if (parent.uid !== 0 || !['/tmp', '/var'].includes(ancestor)) throw new Error('Unsafe managed ancestor');
    } else if (!parent.isDirectory() || ![0, process.getuid()].includes(parent.uid) ||
        ((parent.mode & 0o022) && !(parent.uid === 0 && (parent.mode & 0o1000)))) {
      throw new Error('Unsafe managed ancestor');
    }
    ancestor = path.dirname(ancestor);
  }
}

function privateFile(file, optional = false) {
  privateDirectory(path.dirname(file));
  let stat;
  try { stat = fs.lstatSync(file); } catch (error) {
    if (optional && error.code === 'ENOENT') return;
    throw new Error('Missing managed file');
  }
  if (!stat.isFile() || stat.isSymbolicLink() || stat.uid !== process.getuid() ||
      (stat.mode & 0o777) !== 0o600 || stat.nlink !== 1) throw new Error('Unsafe managed file');
}

function sqlite() {
  if (process.versions.bun) throw new Error('Managed runtime requires node:sqlite');
  try { return require('node:sqlite').DatabaseSync; } catch {
    throw new Error('Managed runtime requires node:sqlite');
  }
}

function openRefreshStore(file) {
  privateFile(file);
  for (const suffix of ['-wal', '-shm', '-journal']) privateFile(file + suffix, true);
  const Database = sqlite();
  const oldMask = process.umask(0o077);
  let db;
  try {
    db = new Database(file, { readOnly: false });
    db.exec('PRAGMA busy_timeout=5000; PRAGMA synchronous=FULL;');
    const meta = db.prepare('SELECT protocol FROM refresh_meta WHERE id=1').get();
    if (meta?.protocol !== 1) throw new Error('Invalid refresh enrollment');
    db.prepare('SELECT key, owner, state, result, generation, started_at FROM refresh_flights LIMIT 0').all();
    db.prepare('SELECT value FROM refresh_sequence WHERE id=1').get();
    return db;
  } catch {
    db?.close();
    throw new Error('Managed refresh store refused');
  } finally { process.umask(oldMask); }
}

// Explicit initial maintenance enrollment only. Workers must never recreate this file.
function enrollRefreshStore(file, generation = 0) {
  privateDirectory(path.dirname(file));
  if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('Invalid generation baseline');
  const Database = sqlite();
  const fd = fs.openSync(file, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
  fs.closeSync(fd);
  const oldMask = process.umask(0o077);
  let db;
  try {
    db = new Database(file);
    db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      BEGIN IMMEDIATE;
      CREATE TABLE refresh_meta (id INTEGER PRIMARY KEY CHECK(id=1), protocol INTEGER NOT NULL);
      INSERT INTO refresh_meta VALUES(1,1);
      CREATE TABLE refresh_sequence (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);
      CREATE TABLE refresh_flights (
        key TEXT PRIMARY KEY, owner TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','done','uncertain')),
        result TEXT, generation INTEGER, started_at TEXT NOT NULL);
    `);
    db.prepare('INSERT INTO refresh_sequence VALUES(1,?)').run(generation);
    db.exec('COMMIT');
  } finally { db?.close(); process.umask(oldMask); }
}

function schemaLayout(db) {
  return db.prepare("SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").all()
    .map(row => ({ ...row, sql: row.sql?.replace(/\s+/g, ' ').trim() }));
}

async function createManifest(root) {
  const base = path.join(root, 'src/lib/db');
  const files = ['schema.js', 'version.js', 'migrate.js'];
  function walk(directory) {
    for (const entry of fs.readdirSync(path.join(base, directory), { withFileTypes: true })) {
      const relative = `${directory}/${entry.name}`;
      if (entry.isDirectory()) walk(relative);
      else if (entry.isFile()) files.push(relative);
      else throw new Error('Unsafe manifest input');
    }
  }
  walk('migrations');
  const hash = createHash('sha256');
  for (const relative of files.sort()) {
    const bytes = fs.readFileSync(path.join(base, relative));
    const name = Buffer.from(`src/lib/db/${relative}`);
    for (const part of [name, bytes]) hash.update(String(part.length)).update(':').update(part);
  }
  const { TABLES, buildCreateTableSql, SCHEMA_VERSION } = await import(pathToFileURL(path.join(base, 'schema.js')));
  const { latestVersion } = await import(pathToFileURL(path.join(base, 'migrations/index.js')));
  const Database = sqlite();
  const expected = new Database(':memory:');
  try {
    for (const [name, definition] of Object.entries(TABLES)) {
      expected.exec(buildCreateTableSql(name, definition));
      for (const index of definition.indexes || []) expected.exec(index);
    }
    return { protocol: 1, schemaVersion: SCHEMA_VERSION, migrationVersion: latestVersion(),
      adapter: 'node:sqlite', persistenceFingerprint: hash.digest('hex'), layout: schemaLayout(expected) };
  } finally { expected.close(); }
}

function readManifest(file, privateInput = false) {
  if (privateInput) privateFile(file);
  const value = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (value.protocol !== 1 || value.adapter !== 'node:sqlite' ||
      !Number.isSafeInteger(value.schemaVersion) || !Number.isSafeInteger(value.migrationVersion) ||
      !/^[a-f0-9]{64}$/.test(value.persistenceFingerprint) || !Array.isArray(value.layout) || !value.layout.length) {
    throw new Error('Invalid compatibility manifest');
  }
  return value;
}

function verifyManagedDatabase(file, buildFile, enrolledFile, refreshFile) {
  if (!buildFile || !enrolledFile || !refreshFile) throw new Error('Managed enrollment paths required');
  privateFile(file);
  for (const suffix of ['-wal', '-shm', '-journal']) privateFile(file + suffix, true);
  const candidate = readManifest(buildFile);
  const enrolled = readManifest(enrolledFile, true);
  if (JSON.stringify(candidate) !== JSON.stringify(enrolled)) throw new Error('Incompatible persistence manifest');
  const Database = sqlite();
  const db = new Database(file, { readOnly: true });
  let refresh;
  try {
    if (JSON.stringify(schemaLayout(db)) !== JSON.stringify(candidate.layout)) throw new Error('Enrolled database layout mismatch');
    for (const [key, value] of [['schemaVersion', candidate.migrationVersion], ['backupSchemaVersion', candidate.schemaVersion]]) {
      const stored = db.prepare('SELECT value FROM _meta WHERE key=?').get(key);
      if (stored?.value !== String(value)) throw new Error('Enrolled migration metadata mismatch');
    }
    refresh = openRefreshStore(refreshFile);
    const sequence = refresh.prepare('SELECT value FROM refresh_sequence WHERE id=1').get()?.value;
    if (!Number.isSafeInteger(sequence) || sequence < 0) throw new Error('Invalid refresh sequence');
    for (const row of db.prepare('SELECT data FROM providerConnections').all()) {
      const data = JSON.parse(row.data);
      if (data.refreshGeneration !== undefined) throw new Error('Legacy credential generation requires maintenance');
      const generations = data.refreshGenerations ?? {};
      if (!generations || typeof generations !== 'object' || Array.isArray(generations) ||
          Object.entries(generations).some(([family, generation]) => !['oauth', 'copilot'].includes(family) ||
            !Number.isSafeInteger(generation) || generation < 1 || generation > sequence)) {
        throw new Error('Refresh generation store is stale');
      }
    }
    return candidate;
  } finally { refresh?.close(); db.close(); }
}

module.exports = { privateDirectory, privateFile, openRefreshStore, enrollRefreshStore,
  createManifest, readManifest, verifyManagedDatabase, schemaLayout };
