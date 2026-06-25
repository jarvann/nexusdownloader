# Vortex Parity — Tech Debt Audit

A subsystem-by-subsystem comparison of NexusDownloader against the real Vortex
source (`~/personal/nexusoriginal`), to stop the recurring "we're doing X
differently than Vortex" surprises. Generated 2026-06-25 via a 6-agent audit
(download, install, rules, cycles, deploy, plugin-sorting).

---

## The three root causes (every finding traces to one of these)

1. **Identity from disk names vs. stored metadata.**
   Vortex persists `modId`/`fileId`/`fileMD5`/`fileSize` on each download record
   (at download time — it initiated the nxm URL, so it *knows* them) and
   `instanceId` on app state, then matches on those. We have **no persisted
   per-file records**, so we re-derive identity by parsing/globbing filenames
   (`-<modId>-` token, name-prefix globs, `_gen_id` from a seed). This is the
   source of the archive-mismatch, "Never Installed", duplicate-mods, and
   wrong-folder bugs we've hit repeatedly.
   *Highest-leverage fix: emit a per-file download manifest and match on it.*

2. **Silent success / auto-resolve vs. stop-and-ask.**
   Vortex stops and asks the user (Cycles dialog, FOMOD validation prompt,
   purge query) or hard-errors. We silently auto-break cycles, fall back from
   FOMOD to extract-all, accept partial extractions, and reported phantom
   installs. Silent divergence is how bugs hid for days.

3. **Re-implementing LOOT/Vortex algorithms vs. delegating to them.**
   Plugin sort (LOOT masterlist + groups), ESL eligibility (LOOT
   `isValidAsLightPlugin`), and conflict normalization are approximated by hand.
   They will diverge from real Vortex output whenever the masterlist/group graph
   matters.

---

## Priority roadmap (do these in order)

**P0 — correctness bugs that silently install/deploy the wrong thing**
- INSTALL-1/2/6: unify on one resolver keyed by `(modId,fileId)` → md5 → fileSize, never name-fallthrough. (The archive-matcher fix `5d762ed` is the first slice; the dual-matcher F6 is next.)
- DEPLOY-1: route root/ENB/dinput modTypes to the game root, not `Data/`.
- PLUGIN-1/2/8: fixed 18-name native list, correct `plugins.txt` header+encoding, never bulk-enable.
- RULES-1: clear the inverse rule on the dest mod (cycle reappearance across re-Links).

**P1 — fidelity gaps ("make Vortex believe it did it")**
- INSTALL-3: faithful `deriveModInstallName` (char-code masking).
- DEPLOY-2/8: write the staging msgpack manifest backup + the redux deployment state.
- CYCLE-1: deterministic, logged cycle-edge victim (or surface to user).
- DOWNLOAD-2: compute real MD5 while streaming.

**P2 — robustness / data-loss-prevention**
- INSTALL-7/8/10: stop content-filtering files, don't degrade FOMOD silently, verify extraction count on all paths.
- DOWNLOAD-3/4: per-file resume + size verification.
- DEPLOY-5: orphan purge + backups on redeploy.

**P3 — accept-or-delegate (big structural items)**
- PLUGIN-3/4/6: real LOOT pass (masterlist groups + `isValidAsLightPlugin`) or explicitly document the approximation.

---

## DOWNLOAD
| # | Sev | Our behavior | Vortex behavior | Risk |
|---|-----|--------------|-----------------|------|
| D1 | High | `vortex_sync.index_by_modid` re-derives identity from the `-<modId>-` filename token; fileId never used | stores `modInfo.nexus.ids.{modId,fileId}` on the record at download time; matches on it (`eventHandlers.ts:387`, `dependencies.findDownloadByRef`) | name tokens collide (versions/years), same-modId files can't be told apart by id → silent missing / wrong file |
| D2 | High | `build_download` md5 = whatever collection.json carried; file never hashed | `postprocessDownload.finalizeDownload` always `genMd5Hash` + `setDownloadHashByFile` | empty `fileMD5` breaks Vortex's md5-first matching (`md5Hint`) and metadata enrichment |
| D3 | High | resume marks a modId done when `existing[mid] >= expected[mid]` (per-modId count) | per-file by fileId | a stale/extra `-mid-` file can satisfy the count → a wanted fileId never downloads |
| D4 | Med | skip-if-`exists` ignores size; `received:size` taken from collection meta | `removeInvalidDownloads` validates `received===size` | a half-written archive from a crashed run is accepted → corrupt install |
| D5 | Low | `_gen_id = sha1(modId-fileId)` (no gameId) | random `shortid()`, identity in `nexus.ids` | cross-game (modId,fileId) collision could overwrite a record |

**Single fix collapsing D1/D3/D4:** have `download.py` write `downloads/<game>/.nxd_manifest.json` mapping `localPath → {modId,fileId,md5,size}` as it downloads; `loadcollection` (resume) and `build_plan` (association) consume it. Compute md5 in the existing chunk loop (D2).

## INSTALL
| # | Sev | Our behavior | Vortex behavior | Risk |
|---|-----|--------------|-----------------|------|
| I1 | High | `_find_mod_archive`/`_optimized` match by modId token then name-glob; `fileId` unused | `findDownloadByRef`: md5 → `(modId,fileId)`; **refuses** name-fallthrough when ids differ (`testModReference.ts:347`) | wrong archive (addon vs base) — the OAR/TrueHUD loop |
| I2 | High | nexus path uses fileSize only as a within-modId tiebreak; md5 never | `lookupFulfills` checks md5+fileSize+logicalName together | same-size variants → name guess |
| I3 | High | `_get_vortex_folder_name` = `stem` + single-`_` mask, all platforms | `deriveModInstallName` = `maskFSInvalidChars` (`_<charCode>_`, platform-specific set) | folder names disagree with Vortex → re-install churn, skip-check misfire |
| I4 | Med | `_find_mod_root` bespoke indicator list, descends one level, special-cases `Data/` | gamebryo stop-patterns, first-prefix search at any depth | deep-nested or `interface/`-only mods get wrong root |
| I5 | Med | "already installed" = predicted folder non-empty + heuristic content | `findModByRef` incl. `installerChoices` equality | changed FOMOD choices silently skipped; folder-prediction wobble → churn |
| I6 | Med | skip-check and installer call **different** matchers (`_optimized` vs `_find_mod_archive`) | one `findDownloadByRef` everywhere | the two disagree → install-then-not-recognized loop |
| I7 | Med | `_should_skip_file` drops readme/txt/md/pdf/license by substring | `basicInstaller` copies everything (only stop-patterns strip `fomod/`) | drops needed `.txt`/strings/config; false "empty/invalid" |
| I8 | Med | `_install_fomod` silently falls back to extract-all on parse/0-file | FOMOD failure prompts user; never degrades to copy-all | every option's files dumped → conflicting plugins, silent SUCCESS |
| I9 | Low | dead `_parse_fomod_choices`/`_map_choice_to_files` still reachable | parses ModuleConfig.xml authoritatively | maintenance trap |
| I10 | Med | extraction count check only on py7zr path; zip/external lack it | short/failed extraction is a hard error | partial extraction → partial install marked SUCCESS |

## RULES (per-mod conflict rules)
*Confirmed correct: `{id,idHint,archiveId}` shape, source-only writes, JSON-blob `###rules` leaf.*
| # | Sev | Our behavior | Vortex behavior | Risk |
|---|-----|--------------|-----------------|------|
| R1 | High | Phase 1b overwrites the source's `rules`; never reads existing, never clears the **inverse** rule on the dest | `postprocessCollection` removes conflicting `exSourceRules` AND `exDestRules` | a prior "dest after source" survives next to new "source before dest" → contradictory pair → Cycles dialog returns |
| R2 | Med | unresolved-dest fallback writes the raw collection reference (may carry a collection `id` that can never equal a folder) | keeps the original reference but it resolves by content marker | rule silently never matches → conflict stays unresolved |
| R3 | Med | `_resolve_ref` uses md5/logicalName/idHint only | `testRef` also matches `repo`(modId+fileId), `tag`(referenceTag), `fileExpression` | author rules using fileExpression/repo/tag don't resolve |
| R4 | Low | only `before`/`after` processed; `conflicts` dropped | keeps `conflicts` rules | incompatibility declarations lost (cosmetic for ordering) |

## CYCLES
*Confirmed correct: only `before`/`after` affect order (matches `sort.ts`); self-loop drop for shared-modId variants is legitimately ours.*
| # | Sev | Our behavior | Vortex behavior | Risk |
|---|-----|--------------|-----------------|------|
| C1 | High | `_break_rule_cycles` drops the first DFS back-edge (order-dependent) and removes that rule silently | `sort.ts` throws `CycleError`; **refuses to deploy**, shows Cycles dialog for the user to fix | may drop the author's important edge; victim depends on iteration order (non-deterministic across runs) |
| C2 | Med | `stable_toposort` tiebreak = installed-folder order | graphlib topsort = mod-array (collection `mods[]`) insertion order | rule-free conflict winners differ from Vortex for overlapping files |
| C3 | Low | three cycle behaviors across two files; `order_plugins` builds plugin edges with no cycle handling | one policy | a cyclic `pluginRules` set is silently tolerated → arbitrary plugin order |

## DEPLOY
*Confirmed correct: `instanceId` stamping via `deploy_collection` (purge-trap avoidance), `time = mtime*1000`, last-writer-wins in principle.*
| # | Sev | Our behavior | Vortex behavior | Risk |
|---|-----|--------------|-----------------|------|
| DE1 | High | every folder hard-linked into single `Data/`; `target=""` | per-modType deploy: `getModPaths` maps `dinput/skse64/enb` → **game root**, each with its own manifest | ENB/dinput/root-type mods land in `Data/` → don't load |
| DE2 | High | only primary `Data/vortex.deployment.json` | also `<staging>/vortex.deployment.<type>msgpack` backup; reads it on corruption | no corruption recovery; lower fidelity |
| DE3 | Med | conflict key = `relpath.replace('/','\\').lower()` | `getNormalizeFunc`: + NFC unicode + `path.normalize` | unicode-variant / `.`/`..` paths key differently → wrong winner |
| DE4 | Med | order from `collection.modRules` only | also honors per-file `fileOverrides` | manual/author file-level overrides ignored |
| DE5 | Med | always relink winners; no prev-manifest diff | `diffActivation` purges `removed`, backs up overwritten vanilla, drops `__folder_managed_by_vortex` | orphan files accumulate; purge can't restore originals |
| DE6 | Med | `target` always `""` | `activate` sets `target=deployPath` | benign for Data-only; wrong once DE1 fixed |
| DE7 | Low | standalone `deploy()` falls back to manifest-derived instanceId | authoritative = `state.app.instanceId` | a stale manifest could poison instanceId → purge prompt |
| DE8 | Med | bumps `deploymentCounter`, clears `needToDeploy` | also persists redux `lastActivation` snapshot | Vortex's in-state deployed list stale → possible redeploy/badge mismatch (needs live test) |

## PLUGIN SORTING
*Key fact: Skyrim SE uses Vortex's `"fallout4"` plugins.txt format. `loadorder.txt` header/CRLF already correct.*
| # | Sev | Our behavior | Vortex behavior | Risk |
|---|-----|--------------|-----------------|------|
| P1 | High | `_BASE_CC_RE` excludes the whole `cc???sse###` namespace as "native" | fixed 18-name `skyrimse.nativePlugins` set (5 ESMs + 13 free pre-1.6 CC) | bought/AE CC ESLs dropped from plugins.txt → load inactive; cap accounting off |
| P2 | High | `plugins.txt` header = vanilla "# This file is used by Skyrim", utf-8 | header `"# Automatically generated by Vortex"`, **latin1**; rejects foreign headers | Vortex treats our file as foreign-modified → resyncs/clobbers |
| P3 | High | force masters-first + drop cross-block edges; vanilla→CC→rest | order = LOOT `sortPluginsAsync` integer (`loadOrder`), no masters-first reorder | order differs from LOOT → conflicts resolve differently |
| P4 | High | only `pluginRules.plugins[].after`; groups ignored | LOOT masterlist+userlist **groups** are first-class sort inputs | grouped plugins land arbitrarily |
| P5 | Med | no `.ghost` of disabled plugins; `scan_plugins` counts all | `makeSetPluginGhost` renames disabled → `.ghost`; sort treats ghost as not-deployed | disabled masters mishandled; cap counts inflated |
| P6 | Med | hand-rolled `could_be_light` (`objidx<=0xFFF`); ignores medium `0x400`, empty plugins | asks LOOT `isValidAsLightPlugin`; tracks `FLAG_MEDIUM`, `isEmpty` | mislabels injected/medium/empty → runtime FormID corruption risk |
| P7 | Med | caps "254 full / 4096 light"; counts exclude vanilla/CC | tiered `253/254/255` regular, `medium<=256`, `light<=4096`, counts **enabled incl. natives** | cap accounting not comparable to Vortex's |
| P8 | Med | no-collection-list default enables every deployed plugin | `autoEnable:false` → new plugins stay disabled | bulk-enable blows cap, activates optionals |

---

## Notes
- Items marked "needs live test" (DE8) should be verified by opening Vortex
  post-deploy and confirming no redeploy/purge is triggered.
- P3/P4/P6 are the deepest: they amount to "we approximate LOOT." The clean fix
  is to shell out to `libloot`/the LOOT CLI for the authoritative sort + ESL
  eligibility, or to explicitly accept and document the approximation.
- The agents flagged several already-correct behaviors (listed under each
  section) — those are parity wins worth preserving when refactoring.
