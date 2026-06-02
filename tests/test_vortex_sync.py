"""Tests for the sync orchestrator's pure planning logic."""
from utils import vortex_sync as vs


COLLECTION = {
    "info": {"name": "Test Coll", "author": "me", "installInstructions": "read the page"},
    "mods": [
        {"name": "ModA", "version": "1", "author": "a", "details": {"category": "Misc"},
         "source": {"type": "nexus", "modId": 100, "fileId": 1000, "md5": "aaa",
                    "fileSize": 10, "logicalFilename": "ModA", "tag": "tagA", "updatePolicy": "prefer"}},
        {"name": "ModB", "version": "2", "author": "b", "details": {"category": "Armor"},
         "source": {"type": "nexus", "modId": 200, "fileId": 2000, "md5": "bbb",
                    "fileSize": 20, "logicalFilename": "ModB", "tag": "tagB", "updatePolicy": "exact"}},
        {"name": "Browse", "source": {"type": "browse", "url": "http://x"}},
        {"name": "NoDisk", "version": "1",
         "source": {"type": "nexus", "modId": 300, "fileId": 3000, "md5": "ccc", "tag": "tagC"}},
    ],
}
DL = {"100": ["ModA-100-1-0.zip"], "200": ["ModB-200-1-0.7z"]}
FOLDERS = {"100": ["ModA-100-1-0"], "200": ["ModB-200-1-0"]}
COLL_FOLDER = "Test-Coll-689581-232-1777961508"


def _plan(existing=None):
    return vs.build_plan(
        COLLECTION, downloads_by_modid=DL, folders_by_modid=FOLDERS,
        existing_dl_by_path=existing or {}, profile_id="PROF",
        collection_folder=COLL_FOLDER, collection_id=26945, slug="gnfjwh",
        revision_id=689581, revision_number=232)


def test_counts_and_skips():
    p = _plan()
    assert p.mod_count == 2            # ModA, ModB (have disk)
    assert p.skipped_manual == 1       # Browse
    assert p.skipped_no_disk == 1      # NoDisk (nexus but not on disk)
    assert p.profile_enables == 2
    assert p.rule_count == 3           # rules for all 3 nexus members regardless of disk


def test_all_records_are_schema_valid():
    assert _plan().violations == []


def test_new_downloads_counts_members_plus_collection():
    # nothing registered -> ModA + ModB + the collection archive = 3 new downloads
    assert _plan().new_downloads == 3


def test_reuses_existing_download_ids():
    existing = {"ModA-100-1-0.zip": "EXISTING_A"}
    p = _plan(existing)
    # ModA reuses, ModB + collection are new -> 2 new
    assert p.new_downloads == 2
    mod_a_arch = f"persistent###mods###skyrimse###ModA-100-1-0###archiveId"
    assert p.records[mod_a_arch] == '"EXISTING_A"'


def test_collection_record_is_present_and_typed():
    p = _plan()
    type_key = f"persistent###mods###skyrimse###{COLL_FOLDER}###type"
    rev_key = f"persistent###mods###skyrimse###{COLL_FOLDER}###attributes###revisionNumber"
    assert p.records[type_key] == '"collection"'
    assert p.records[rev_key] == "232"
    # collection enabled in the profile
    enabled = f"persistent###profiles###PROF###modState###{COLL_FOLDER}###enabled"
    assert p.records[enabled] == "true"


def test_revision_manifest_written():
    p = _plan()
    mid = "persistent###collections###revisions###689581###info###revisionNumber"
    assert p.records[mid] == "232"


def test_helpers():
    assert vs.parse_revision_from_folder("X-689581-232-1777961508") == (689581, 232)
    idx = vs.index_by_modid(["A-100-1-0.zip", "B-200-1.7z", "notes.txt"], archives_only=True)
    assert idx == {"100": ["A-100-1-0.zip"], "200": ["B-200-1.7z"]}
    # deterministic ids
    assert vs._gen_id("100-1000") == vs._gen_id("100-1000")
    assert vs._gen_id("a") != vs._gen_id("b")
