#!/usr/bin/env node
// Vortex sync writer (Phase 1): make our downloaded + staged mods show up in
// Vortex as installed + enabled, by writing the records Vortex itself would.
//
// Reads collection.json + scans the downloads and staging folders, then writes,
// per mod, three record types into Vortex's state.v2 LevelDB:
//   1. persistent.downloads.files.<id>            (state: finished)   -- if missing
//   2. persistent.mods.skyrimse.<folder>          (state: installed)
//   3. persistent.profiles.<profile>.modState.<folder>.enabled = true
//
// SAFE BY DEFAULT: dry-run unless --apply is passed; --apply makes a timestamped
// backup of state.v2 first. Vortex MUST be closed (it holds an exclusive lock).
//
// Usage:
//   node vortex_sync.js --collection <collection.json> --downloads <dir> \
//       --staging <dir> --db <state.v2> --profile <id> [--limit N] [--apply]

const fs = require('fs');
const path = require('path');
const { ClassicLevel } = require('classic-level');

const P = '###';
const GAME = 'skyrimse';            // Vortex internal game id
const DOMAIN = 'skyrimspecialedition'; // Nexus domain

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  if (i === -1) return def;
  const v = process.argv[i + 1];
  return (v === undefined || v.startsWith('--')) ? true : v;
}

const CFG = {
  collection: arg('collection'),
  downloads: arg('downloads'),
  staging: arg('staging'),
  db: arg('db'),
  profile: arg('profile'),
  limit: arg('limit') ? parseInt(arg('limit'), 10) : Infinity,
  apply: !!arg('apply', false),
};

for (const k of ['collection', 'downloads', 'staging', 'db', 'profile']) {
  if (!CFG[k]) { console.error(`Missing --${k}`); process.exit(2); }
}

const ARCH_RE = /\.(7z|zip|rar)$/i;
const genId = (seed) => {
  // deterministic-ish 12-char id from a seed (modId-fileId) so re-runs are stable
  const c = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let h = 2166136261;
  for (const ch of String(seed)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
  let s = 'NXD';
  for (let i = 0; i < 9; i++) { h = Math.imul(h ^ (h >>> 13), 16777619); s += c[Math.abs(h) % c.length]; }
  return s;
};

// Index disk: archive filename -> path, and modId -> {archive, folder}
function indexDisk() {
  const archives = fs.readdirSync(CFG.downloads).filter(f => ARCH_RE.test(f));
  const folders = fs.readdirSync(CFG.staging, { withFileTypes: true })
    .filter(d => d.isDirectory()).map(d => d.name);
  const byModArchive = {}, byModFolder = {};
  for (const a of archives) {
    const m = a.match(/-(\d{2,7})-/);
    if (m) (byModArchive[m[1]] = byModArchive[m[1]] || []).push(a);
  }
  for (const f of folders) {
    const m = f.match(/-(\d{2,7})-/);
    if (m) (byModFolder[m[1]] = byModFolder[m[1]] || []).push(f);
  }
  return { byModArchive, byModFolder, archiveCount: archives.length, folderCount: folders.length };
}

// flatten an object tree into leaf [keyPath, jsonValue] under a base
function* leaves(base, obj) {
  for (const [k, v] of Object.entries(obj)) {
    const key = base + P + k;
    if (v === undefined) continue; // skip unset attributes
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      yield* leaves(key, v);
    } else {
      yield [key, JSON.stringify(v)];
    }
  }
}

function downloadRecord(id, archiveName, s, modName) {
  return {
    [`persistent${P}downloads${P}files${P}${id}`]: {
      chunks: [], urls: [], state: 'finished', source: 'nexus',
      fileMD5: s.md5, fileTime: s.fileTime || 0, game: [GAME],
      localPath: archiveName, size: s.fileSize,
      modInfo: {
        source: 'nexus',
        meta: {
          fileName: archiveName, fileMD5: s.md5, gameId: GAME, domainName: DOMAIN,
          source: 'nexus', logicalFileName: s.logicalFilename || modName,
          fileSizeBytes: s.fileSize, status: 'published',
          sourceURI: `nxm://${DOMAIN}/mods/${s.modId}/files/${s.fileId}`,
          details: { modId: String(s.modId), fileId: String(s.fileId) },
        },
        nexus: { ids: { modId: s.modId, fileId: s.fileId, gameId: DOMAIN } },
      },
    },
  };
}

function modRecord(folder, archiveId, s, m) {
  return {
    [`persistent${P}mods${P}${GAME}${P}${folder}`]: {
      id: folder, installationPath: folder, state: 'installed', type: '',
      archiveId, fileOverrides: [],
      attributes: {
        source: 'nexus', downloadGame: GAME, endorsed: 'Undecided',
        modId: s.modId, fileId: s.fileId, fileMD5: s.md5,
        logicalFileName: s.logicalFilename || m.name,
        name: folder, customFileName: m.name,
        version: String(m.version || ''), modVersion: String(m.version || ''),
        referenceTag: s.tag,                 // <-- links to collection rule
        author: m.author || '', category: (m.details && m.details.category) || '',
        fileSize: s.fileSize,
      },
    },
  };
}

function profileEntry(folder) {
  return {
    [`persistent${P}profiles${P}${CFG.profile}${P}modState${P}${folder}`]: {
      enabled: true, enabledTime: 0,
    },
  };
}

(async () => {
  const coll = JSON.parse(fs.readFileSync(CFG.collection, 'utf8'));
  const disk = indexDisk();
  console.log(`disk: ${disk.archiveCount} archives, ${disk.folderCount} staged folders`);

  const db = new ClassicLevel(CFG.db, { keyEncoding: 'utf8', valueEncoding: 'utf8' });
  try { await db.open({ createIfMissing: false }); }
  catch (e) { console.error('Cannot open DB (is Vortex running? close it):', e.message); process.exit(2); }

  // existing downloads: localPath -> id ; existing installed mod folders
  const dlByPath = {}; const existingMods = new Set();
  for await (const [k, v] of db.iterator({ gte: `persistent${P}downloads${P}files${P}`, lte: `persistent${P}downloads${P}files${P}\xff` }))
    if (k.endsWith(`${P}localPath`)) dlByPath[JSON.parse(v)] = k.split(P)[3];
  for await (const [k] of db.iterator({ gte: `persistent${P}mods${P}${GAME}${P}`, lte: `persistent${P}mods${P}${GAME}${P}\xff` }))
    if (k.endsWith(`${P}state`)) existingMods.add(k.split(P)[3]);
  console.log(`existing: ${Object.keys(dlByPath).length} downloads registered, ${existingMods.size} mods installed`);

  const records = {};               // key -> value (all leaves to write)
  let nMods = 0, nNewDl = 0, nProfile = 0, nNoFolder = 0, nManual = 0;
  const samples = [];

  for (const m of coll.mods) {
    if (nMods >= CFG.limit) break;
    const s = m.source || {};
    if (s.type !== 'nexus' || !s.modId || !s.fileId) { nManual++; continue; }

    const folders = disk.byModFolder[String(s.modId)] || [];
    const archives = disk.byModArchive[String(s.modId)] || [];
    if (!folders.length || !archives.length) { nNoFolder++; continue; }
    const folder = folders[0];
    const archiveName = archives[0];

    // download id: reuse existing registration, else create
    let dlId = dlByPath[archiveName];
    if (!dlId) { dlId = genId(`${s.modId}-${s.fileId}`); Object.assign(records, flat(downloadRecord(dlId, archiveName, s, m.name))); nNewDl++; }

    const mr = modRecord(folder, dlId, s, m);
    // fileName attribute (folder is the install dir; fileName is the archive)
    mr[`persistent${P}mods${P}${GAME}${P}${folder}`].attributes.fileName = archiveName;
    Object.assign(records, flat(mr));
    Object.assign(records, flat(profileEntry(folder)));
    nMods++; nProfile++;
    if (samples.length < 4) samples.push({ folder, dlId, modId: s.modId, tag: s.tag });
  }

  console.log(`\nPLAN: ${nMods} mod records, ${nNewDl} new downloads, ${nProfile} profile enables`);
  console.log(`skipped: ${nManual} non-nexus/manual, ${nNoFolder} with no staged folder/archive on disk`);
  console.log(`total leaf keys to write: ${Object.keys(records).length}`);
  console.log('samples:'); samples.forEach(s => console.log('  ', s));

  if (!CFG.apply) {
    console.log('\nDRY-RUN (no writes). Re-run with --apply to write (backs up first).');
    await db.close();
    return;
  }

  // --apply: backup then batch write
  await db.close();
  const ts = new Date().toISOString().replace(/[:.]/g, '').slice(0, 15);
  const bak = CFG.db + '.bak-sync-' + ts;
  fs.cpSync(CFG.db, bak, { recursive: true });
  console.log('backup ->', bak);

  const db2 = new ClassicLevel(CFG.db, { keyEncoding: 'utf8', valueEncoding: 'utf8' });
  await db2.open({ createIfMissing: false });
  const batch = db2.batch();
  for (const [k, v] of Object.entries(records)) batch.put(k, v);
  await batch.write({ sync: true }); // force durability before close
  await db2.close();
  console.log(`APPLIED: wrote ${Object.keys(records).length} keys for ${nMods} mods.`);
})().catch(e => { console.error('ERR', e); process.exit(1); });

// flatten helper used above (object-of-trees -> {leafKey: jsonValue})
function flat(objOfTrees) {
  const out = {};
  for (const [base, tree] of Object.entries(objOfTrees)) {
    for (const [k, v] of leaves(base, tree)) out[k] = v;
    // also handle the case where tree has array/scalar leaves directly handled by leaves()
  }
  return out;
}
