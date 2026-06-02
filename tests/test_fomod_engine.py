"""Tests for the FOMOD engine: parsing, selection mapping, conditional installs."""
from utils import fomod_engine as fe


MODULE_CONFIG = """<?xml version="1.0"?>
<config>
  <moduleName>Test Mod</moduleName>
  <requiredInstallFiles>
    <file source="always.esp" destination="always.esp" priority="0"/>
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Main">
      <optionalFileGroups order="Explicit">
        <group name="Patches" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Alpha">
              <files><file source="alpha.esp" destination="alpha.esp" priority="0"/></files>
              <conditionFlags><flag name="wantAlpha">On</flag></conditionFlags>
            </plugin>
            <plugin name="Beta">
              <files><folder source="beta" destination="beta" priority="0"/></files>
            </plugin>
            <plugin name="Gamma">
              <files><file source="gamma.esp" destination="gamma.esp" priority="0"/></files>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies operator="And">
          <flagDependency flag="wantAlpha" value="On"/>
        </dependencies>
        <files><file source="alpha_extra.esp" destination="alpha_extra.esp"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""


def _make_package(tmp_path):
    """Lay out an extracted FOMOD package with the files the config references."""
    (tmp_path / "fomod").mkdir()
    (tmp_path / "fomod" / "ModuleConfig.xml").write_text(MODULE_CONFIG)
    for f in ["always.esp", "alpha.esp", "gamma.esp", "alpha_extra.esp"]:
        (tmp_path / f).write_text("x")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "beta1.esp").write_text("x")
    (tmp_path / "beta" / "beta2.esp").write_text("x")
    return tmp_path


def test_parse_structure():
    cfg = fe.parse_moduleconfig(MODULE_CONFIG)
    assert cfg.module_name == "Test Mod"
    assert len(cfg.required_files) == 1
    assert len(cfg.install_steps) == 1
    group = cfg.install_steps[0].groups[0]
    assert group.group_type == "SelectAny"
    assert [p.name for p in group.plugins] == ["Alpha", "Beta", "Gamma"]
    assert cfg.install_steps[0].groups[0].plugins[0].set_flags == {"wantAlpha": "On"}
    assert len(cfg.conditional_installs) == 1


def test_resolve_selects_required_plus_chosen_plus_conditional(tmp_path):
    pkg = _make_package(tmp_path)
    cfg = fe.parse_moduleconfig(fe.find_moduleconfig(pkg))
    # collection chose idx 0 (Alpha, sets flag) and idx 2 (Gamma)
    choices = {"type": "fomod", "options": [
        {"name": "Main", "groups": [
            {"name": "Patches", "choices": [
                {"name": "Alpha", "idx": 0}, {"name": "Gamma", "idx": 2},
            ]}
        ]}
    ]}
    ops, report = fe.resolve_install(cfg, choices, pkg)
    dests = sorted(o.destination for o in ops)
    # required (always) + alpha + gamma + the flag-conditional alpha_extra
    assert "always.esp" in dests
    assert "alpha.esp" in dests
    assert "gamma.esp" in dests
    assert "alpha_extra.esp" in dests          # conditional fired via wantAlpha flag
    assert "beta.esp" not in " ".join(dests)   # Beta NOT chosen
    assert report.conditional_patterns_applied == 1
    assert "Alpha" in report.chosen_plugins and "Gamma" in report.chosen_plugins


def test_unchosen_flag_skips_conditional(tmp_path):
    pkg = _make_package(tmp_path)
    cfg = fe.parse_moduleconfig(fe.find_moduleconfig(pkg))
    # choose only Gamma (idx 2) -> wantAlpha never set -> no alpha_extra
    choices = {"type": "fomod", "options": [
        {"name": "Main", "groups": [{"name": "Patches", "choices": [{"name": "Gamma", "idx": 2}]}]}
    ]}
    ops, report = fe.resolve_install(cfg, choices, pkg)
    dests = [o.destination for o in ops]
    assert "alpha_extra.esp" not in dests
    assert report.conditional_patterns_applied == 0


def test_folder_source_expands_to_files(tmp_path):
    pkg = _make_package(tmp_path)
    cfg = fe.parse_moduleconfig(fe.find_moduleconfig(pkg))
    choices = {"type": "fomod", "options": [
        {"name": "Main", "groups": [{"name": "Patches", "choices": [{"name": "Beta", "idx": 1}]}]}
    ]}
    ops, _ = fe.resolve_install(cfg, choices, pkg)
    dests = sorted(o.destination for o in ops)
    assert "beta/beta1.esp" in dests and "beta/beta2.esp" in dests
