# Tech Debt — Code Duplication & Incidental Bugs

Generated 2026-06-30 from a 4-domain duplication audit (config/version/path,
logging/imports, deploy/manifest/archive/FOMOD, API/GUI/records/load-order).

Effort estimates show **human review/run time** and **my build time / tokens**.
Severity: HIGH = divergent behavior / correctness risk; MEDIUM = maintenance &
drift; LOW = cosmetic.

---

## Tier A — Live bugs surfaced by the audit (fix regardless of dedup)

| ID | Bug | Location | Fix effort |
|----|-----|----------|-----------|
| B1 | `endorse` never receives the unified operation logger — `return logger` sits *before* `set_endorse_logger(logger)`, making it (and the trailing lines) dead code. | `loadcollection.py:65-66` | trivial — me ~10 min / ~10k |
| B2 | `_install_fomod` lacks the phantom-install guard that `_install_simple` has, so a FOMOD mod that extracts to an empty/invalid folder isn't caught. Directly relevant to gap #1. | `fomod_installer.py:725-743` (guard) vs `_install_fomod` 771-949 | small — me ~30 min / ~40k |
| B3 | `endorse_mod` has no retry/backoff while `download` does; a transient network blip silently fails an endorsement. | `endorse.py:72-87` vs `download.py:115-145` | small (falls out of D6) |

---

## Tier 1 — HIGH severity duplication (correctness divergence)

### D1 — FOMOD archive-finder + name-overlap scorer cloned 2–5 ways  ⟵ gap #1
The "match archive to mod" heuristic (modId token → exact fileSize → fuzzy
name-word overlap) is reimplemented across:
- `fomod_installer.py:324-396` (`_find_mod_archive`)
- `fomod_installer.py:398-457` (`_find_mod_archive_optimized`)
- `vortex_sync.py:55-64` (`_best_match`)
- `state_reconcile.py:105-107` (`_match_collection_mod`)
Kept in sync by a comment (`fomod_installer.py:431`). Drift → skip-predictor
picks a different folder than the installer → wrong archive / silent re-install.
**Consolidate** to `utils/mod_matching.py: pick_archive_for_mod()` +
`name_overlap_score()`; all four delegate.
Effort: **medium** — human ~4-6 hr regression; me ~1 hr / ~150k. Needs install-path testing.

### D2 — Two parallel logging frameworks collide on the `'nexusdownloader'` logger
`unified_logging.py:143-241` and `logging_config.py:263-333` both build the same
named logger, both call `handlers.clear()`, and export same-named
`get_logger`/`setup_logging` with **incompatible return types**. Last initializer
wins; mixing them (main_window imports from `logging_config`, rest from
`unified_logging`) can drop handlers.
**Consolidate** on `unified_logging` as the only handler/formatter/rotation
builder; demote `logging_config` to optional add-ons (`PerformanceMetrics`,
`ColoredFormatter`, etc.).
Effort: **large** — human ~1 day; me ~1.5-2 days / ~1.5-2M. Public import surface.

### D3 — `_install_simple` and `_install_fomod` share ~80% of their bodies
Identical extract-to-temp (hash dir + `_acquire_temp_space` + long-path fallback),
temp cleanup (success + exception paths), empty-check, validate, return. Only the
file-selection middle differs. The phantom-guard divergence (B2) is a direct
symptom.
**Consolidate** to `_extract_to_temp(mod, archive) -> Path` context manager +
`_finalize_staging(...) -> InstallationResult`. Both installers shrink to
extract → choose files → place → finalize.
Effort: **medium** — human ~2-3 hr; me ~30-45 min / ~60k.

### D4 — `config.json` path resolved 4 inconsistent ways (the "wrong config" bug class)
`__file__`-relative (`config.py:21`, `install_tab.py:358/414`), CWD-relative
(`ConfigManager('config.json')` in download/endorse/config_manager), and a
PyInstaller multi-candidate walk (`main_window.py:884-913`). The GUI can save to
one file while `download.py` reads another. CLAUDE.md already flags this.
**Consolidate** to `utils/paths.py: project_root()` + `config_path()` (frozen-aware);
make `ConfigManager` default to the resolved absolute path.
Effort: **medium** — human ~2-3 hr (verify source + frozen); me ~45-60 min / ~80k.

---

## Tier 2 — MEDIUM duplication

### D5 — Legacy config shim (`LegacyConfig`/`MinimalConfig` + fallback ladder) copy-pasted
`download.py:20-50` ≈ `endorse.py:11-35` (endorse omits `VortexSettings`; already
diverged). **Consolidate** to `config/compat.py: CONFIG`. Effort: small — me ~15 min / ~20k.

### D6 — Nexus API auth-header + request/retry boilerplate duplicated  ⟵ enables B3
Auth dict `{'apikey': ..., 'Accept': ...}` in `download.py:105` and `endorse.py:64`;
retry/backoff envelope twice in download + un-retried twin in endorse.
**Consolidate** to `nexus_api.py: nexus_headers()` + `nexus_request(...)`.
Streaming download stays bespoke (chunk-level cancel). Effort: small-medium —
me ~30 min / ~50k.

### D7 — `collection.json` (modId, fileId) indexing walked in 4+ modules  ⟵ underpins gap #2
`vortex_sync.py:188`, `state_reconcile.py:290`, `loadcollection.py:98/135`,
`fomod_installer.py:335/410`. Each re-loads JSON and re-derives the nexus-source
identity tuple. **Consolidate** to `collection_model.py: iter_nexus_mods()` /
`index_collection_by_modfile()`. This is also the shared rule/identity layer the
conflict-ordering work (gap #2) will build on. Effort: medium — me ~45 min / ~90k.

### D8 — Project-root detection re-derived by `..`-counting in 3 places
`unified_logging.py:115-127` (robust marker walk) vs hardcoded level counts in
`vortex_db.py:55`, `main_window.py:129/752/790/861`. **Consolidate** into
`utils/paths.py: project_root()` (pairs with D4). Effort: medium — me ~40 min / ~50k.

### D9 — Three ad-hoc "tmp + os.replace" atomic writers bypass `AtomicFileWriter`
`vortex_deploy.py:296` and `install_tab.py:375` reimplement (without the on-error
cleanup / Windows handling) what `file_operations.AtomicFileWriter` already does.
**Consolidate** to `atomic_write_text(path, text)`. Effort: small — me ~15 min / ~20k.

### D10 — Logging odds-and-ends: injection triad, VERBOSE level, cleanup_old_logs
- `set_*/get_*_logger` triad duplicated download↔endorse → `logger_injection.py`.
- `VERBOSE` level defined in `loadcollection.py:51` but used in `download.py:159`
  (AttributeError if download imported standalone) → move to `unified_logging`.
- `cleanup_old_logs` duplicated (`unified_logging.py:299` ≈ `logging_config.py:442`,
  different glob — the second deletes unrelated `*.log`).
Effort: small each — me ~45 min total / ~60k.

### D11 — Mod version stringified 4 ways with 3 different defaults; no semver normalize  ⟵ gap #3
`vortex_records.py:116/253/266` (`""` vs `'0'`) and `install_tab.py:272` (`'?'`).
Record and `versionMatch` rule disagree for a version-less mod. No coercion of
`1.01`→`1.1.0` / `v`-strip anywhere → suspected Starfield-tooling semver error.
**Consolidate** to `normalize_version()` + `mod_version(mod, default="")`.
Effort: small — me ~30 min / ~40k.

---

## Tier 3 — LOW (cheap, do alongside neighbors)

| ID | Dup | Locations | Consolidate to |
|----|-----|-----------|----------------|
| D12 | external 7z/winrar extractor near-identical | `archive_handler.py:406-474` / `476-546` | `_run_external_extractor(...)` |
| D13 | FOMOD `fomod/moduleconfig.xml` detect-by-name ×3 | `archive_handler.py:239/599`, `fomod_installer.py:831` | `find_fomod_members(names)` |
| D14 | archive tool-path discovery ×2 | `archive_handler.py:29-67` / `76-112` | `_discover_archive_tools()` |
| D15 | `parse_revision` + `ARCHIVE_RE` dup | `vortex_sync.py:30/132` / `state_reconcile.py:30/138` | import from `vortex_sync` |
| D16 | hardlink-or-copy ×2 (intentionally diff layers) | `vortex_deploy.py:168` / `fomod_installer.py:175` | optional `link_or_copy()` |

---

## How this maps to the three reliability gaps

- **Gap #1 (FOMOD at scale):** B2 + D1 + D3 are the substrate. Fixing the
  archive-matcher clone and the phantom-guard divergence removes the actual
  silent-wrong-install mechanisms.
- **Gap #2 (conflict ordering):** D7 (shared collection/rule model) is the
  foundation the rule-ordered deploy will reuse.
- **Gap #3 (semver/metadata):** D11 is the fix.

## Suggested batches

1. **Quick-wins bundle** (B1, B3, D5, D6, D9, D11, D15) — ~half a day of mine /
   ~250k. Removes the version sentinels, gives endorse retry+logger, the nexus
   client, atomic writes. Low risk, high tidy.
2. **FOMOD reliability bundle** (B2, D1, D3) — ~1.5 days / ~250k. The gap-#1
   substrate; needs install regression testing.
3. **Collection model** (D7) — feeds gap #2. ~half day.
4. **Path/config unification** (D4, D8) — ~half day.
5. **Logging unification** (D2, D10) — the big one, ~2 days; lower urgency, do last.

**Grand total (everything): ~4-6 of my days / ~4-6M tokens.**
**High-value subset (batches 1-2): ~2 days / ~1.5M tokens** and it clears every
correctness-risk item plus gaps #1 and #3.
