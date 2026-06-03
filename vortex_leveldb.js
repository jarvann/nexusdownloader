#!/usr/bin/env node
// Minimal Node bridge between the Python app and Vortex's state.v2 LevelDB.
// Python owns all logic (record building, schema validation, drift checks);
// this only does raw I/O, because Python has no reliable LevelDB writer on
// Windows while Node's classic-level reads/writes Vortex's exact format.
//
// Commands (all paths are to the state.v2 directory):
//   probe <db>                 -> prints "AVAILABLE" or "LOCKED" (is Vortex holding the lock?)
//   read  <db> <prefix>        -> prints JSON {key: rawJsonValue, ...} for keys under prefix
//   write <db> <batchJsonFile> -> applies {key: rawJsonValue} batch (sync); prints count
//
// Exit codes: 0 ok, 3 locked/unavailable, 1 other error.

const fs = require('fs');
const { ClassicLevel } = require('classic-level');

async function open(db) {
  const lvl = new ClassicLevel(db, { keyEncoding: 'utf8', valueEncoding: 'utf8' });
  await lvl.open({ createIfMissing: false });
  return lvl;
}

(async () => {
  const [cmd, db, arg] = process.argv.slice(2);
  if (!cmd || !db) { console.error('usage: probe|read|write <db> [arg]'); process.exit(1); }

  if (cmd === 'probe') {
    // Acquiring LevelDB's exclusive lock succeeds only if no other process
    // (i.e. Vortex) holds it. We immediately release.
    try {
      const lvl = await open(db);
      await lvl.close();
      console.log('AVAILABLE');
      process.exit(0);
    } catch (e) {
      console.log('LOCKED');
      console.error(e.code || e.message);
      process.exit(3);
    }
  }

  let lvl;
  try { lvl = await open(db); }
  catch (e) { console.error('LOCKED', e.code || e.message); process.exit(3); }

  try {
    if (cmd === 'read') {
      const prefix = arg || '';
      const out = {};
      for await (const [k, v] of lvl.iterator({ gte: prefix, lte: prefix + '\xff' })) out[k] = v;
      process.stdout.write(JSON.stringify(out));
    } else if (cmd === 'write') {
      // Batch can be a plain {key: value} put-map, OR a structured
      // {put: {...}, delPrefixes: [...]} that also deletes every key under each
      // prefix (used to replace an old collection revision atomically).
      const batchObj = JSON.parse(fs.readFileSync(arg, 'utf8'));
      const structured = ('put' in batchObj) || ('delPrefixes' in batchObj);
      const puts = structured ? (batchObj.put || {}) : batchObj;
      const delPrefixes = structured ? (batchObj.delPrefixes || []) : [];
      const batch = lvl.batch();
      let delCount = 0;
      for (const pfx of delPrefixes) {
        for await (const [k] of lvl.iterator({ gte: pfx, lte: pfx + '\xff' })) {
          batch.del(k); delCount++;
        }
      }
      for (const [k, v] of Object.entries(puts)) batch.put(k, v);
      await batch.write({ sync: true });
      console.log(Object.keys(puts).length + delCount);
    } else {
      console.error('unknown command', cmd); process.exit(1);
    }
    await lvl.close();
  } catch (e) {
    try { await lvl.close(); } catch (_) {}
    console.error('ERR', e.message); process.exit(1);
  }
})();
