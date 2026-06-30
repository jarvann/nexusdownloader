"""
FOMOD engine: parse a mod's ``fomod/ModuleConfig.xml`` and resolve which files to
install given the choices recorded in a Nexus collection.

This is a faithful port of Vortex's actual FOMOD installer (the open-source C#
``fomod-installer`` it compiles into ``@nexusmods/fomod-installer-native``;
reference source at ~/personal/fomod-installer-cs/src/InstallScripting/XmlScript).
The algorithm, not a heuristic:

* Parse install steps -> option groups -> options, each option with its files,
  the condition flags it sets, and an option-type resolver (Required / Optional /
  Recommended / NotUsable / CouldBeUsable), plus step visibility conditions,
  required files, and conditionalFileInstalls patterns.
* Run the headless preset flow Vortex uses for collection installs: process
  visible steps IN ORDER, preselect options from the collection's recorded
  choices (and Required / SelectAll / SelectExactlyOne-default rules), letting the
  flags set by selected options drive later steps' visibility, option types, and
  conditional installs.
* Build the file set exactly as Vortex does: required files (phase -1e9), selected
  options' files (phase 0) plus alwaysInstall / installIfUsable files of unselected
  options, then conditionalFileInstalls whose condition holds (phase +1e9); resolve
  same-destination conflicts by higher effective priority, ties by source path.

The engine is GAME-AGNOSTIC. Like Vortex's ``PluginCondition`` (which only calls
``IsActive(path)``), a ``fileDependency`` is evaluated against an ``active_plugins``
set the CALLER supplies (the collection's declared plugins + the game's active
masters). The engine contains no Skyrim-specific knowledge.

It is free of installer side effects: it returns a list of :class:`FileOperation`
(absolute source path -> destination relative path). The caller performs the copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def _norm(s: Optional[str]) -> str:
    """Normalize a name for lenient matching: lowercase, strip non-alphanumerics.
    Vortex matches step/group/option names with exact string equality; we match
    exact first and fall back to this so a collection choice still maps when the
    recorded name differs only by prefix/punctuation/spacing."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ModuleConfig.xml comes from third-party archives -> harden with defusedxml.
try:
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover - defusedxml is a listed dependency
    import xml.etree.ElementTree as ET


# --------------------------------------------------------------------------- #
# Parsed model (mirrors the C# XmlScript model)
# --------------------------------------------------------------------------- #
@dataclass
class FileItem:
    source: str
    destination: str
    priority: int = 0
    is_folder: bool = False
    always_install: bool = False
    install_if_usable: bool = False


@dataclass
class Dependency:
    """A parsed <dependencies> tree: flag/file/version conditions joined And/Or."""
    operator: str = "and"                                       # "and" | "or"
    flags: List[Tuple[str, str]] = field(default_factory=list)        # (flag, value)
    files: List[Tuple[str, str]] = field(default_factory=list)        # (plugin_lower, state)
    children: List["Dependency"] = field(default_factory=list)
    # version deps are parsed but treated as satisfied (we evaluate them as the
    # common case: the user meets the min game/app/extender version).


@dataclass
class TypePattern:
    type: str
    dependency: Optional[Dependency]


@dataclass
class OptionTypeResolver:
    """Static type, or a conditional resolver (first matching pattern, else default)."""
    static_type: Optional[str] = None
    default_type: str = "Optional"
    patterns: List[TypePattern] = field(default_factory=list)

    def resolve(self, flags: Dict[str, str], active: Set[str]) -> str:
        if self.static_type is not None:
            return self.static_type
        for pat in self.patterns:
            if _eval_dependency(pat.dependency, flags, active):
                return pat.type
        return self.default_type


@dataclass
class Option:
    """A selectable option ('plugin' in FOMOD terms). Identity-based selection."""
    name: str = ""
    files: List[FileItem] = field(default_factory=list)
    set_flags: Dict[str, str] = field(default_factory=dict)
    type_resolver: OptionTypeResolver = field(default_factory=OptionTypeResolver)


# Back-compat alias: older code/tests referenced ``Plugin``.
Plugin = Option


@dataclass
class Group:
    name: str
    group_type: str  # SelectAtLeastOne|SelectAtMostOne|SelectExactlyOne|SelectAll|SelectAny
    plugins: List[Option] = field(default_factory=list)   # named 'plugins' for back-compat

    @property
    def options(self) -> List[Option]:
        return self.plugins


@dataclass
class InstallStep:
    name: str
    groups: List[Group] = field(default_factory=list)
    visibility: Optional[Dependency] = None


@dataclass
class ConditionalInstall:
    dependency: Optional[Dependency] = None
    files: List[FileItem] = field(default_factory=list)


@dataclass
class FomodConfig:
    module_name: str = ""
    install_steps: List[InstallStep] = field(default_factory=list)
    required_files: List[FileItem] = field(default_factory=list)
    conditional_installs: List[ConditionalInstall] = field(default_factory=list)
    mod_prerequisites: Optional[Dependency] = None


@dataclass
class FileOperation:
    abs_source: str       # absolute path to the file inside the extracted archive
    destination: str      # path relative to the mod's data root (forward slashes)
    priority: int = 0     # effective priority (file priority + phase offset)


# --------------------------------------------------------------------------- #
# XML parsing
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _find(elem, name):
    for child in elem if elem is not None else []:
        if _localname(child.tag) == name:
            return child
    return None


def _findall(elem, name):
    return [c for c in (elem if elem is not None else []) if _localname(c.tag) == name]


def _find_any(elem, *names):
    """First child matching any of ``names`` (handles schema spelling variants).
    Uses explicit None checks -- an empty element is falsy, so ``a or b`` would
    wrongly skip a real-but-childless element."""
    for name in names:
        found = _find(elem, name)
        if found is not None:
            return found
    return None


def _parse_files(files_elem) -> List[FileItem]:
    items: List[FileItem] = []
    if files_elem is None:
        return items
    for child in files_elem:
        kind = _localname(child.tag)
        if kind not in ("file", "folder"):
            continue
        source = child.get("source", "")
        if not source:
            continue
        destination = child.get("destination")
        if destination is None:
            destination = source        # FOMOD: missing dest -> same relative path
        try:
            priority = int(child.get("priority", "0"))
        except ValueError:
            priority = 0
        items.append(FileItem(
            source=source, destination=destination, priority=priority,
            is_folder=(kind == "folder"),
            always_install=child.get("alwaysInstall", "false").lower() == "true",
            install_if_usable=child.get("installIfUsable", "false").lower() == "true",
        ))
    return items


# Condition element names vary across schema versions (dependancy/dependency etc.);
# match leniently on the local name.
def _parse_dependencies(dep_elem) -> Optional[Dependency]:
    """Parse a <dependencies>/<dependancies> tree (flag + file + version, And/Or)."""
    if dep_elem is None:
        return None
    op = (dep_elem.get("operator", "And") or "And").lower()
    dep = Dependency(operator=("or" if op == "or" else "and"))
    for child in dep_elem:
        kind = _localname(child.tag).lower()
        if kind in ("flagdependency",):
            dep.flags.append((child.get("flag", ""), child.get("value", "")))
        elif kind in ("filedependency", "moduledependency"):
            dep.files.append((child.get("file", "").strip().lower(),
                              child.get("state", "Active") or "Active"))
        elif kind in ("dependencies", "dependancies"):
            nested = _parse_dependencies(child)
            if nested is not None:
                dep.children.append(nested)
        # *VersionDependency / gameDependency / fommDependency / foseDependency etc.
        # are parsed-as-satisfied (treated as met); they rarely gate file selection.
    return dep


def _parse_type_resolver(option_elem) -> OptionTypeResolver:
    """Parse <typeDescriptor> -> static <type> or <dependencyType> patterns."""
    td = _find(option_elem, "typeDescriptor")
    if td is None:
        return OptionTypeResolver(static_type="Optional")
    static = _find(td, "type")
    if static is not None:
        return OptionTypeResolver(static_type=static.get("name", "Optional"))
    dt = _find_any(td, "dependencyType", "dependancyType")
    if dt is None:
        return OptionTypeResolver(static_type="Optional")
    default = _find(dt, "defaultType")
    resolver = OptionTypeResolver(
        static_type=None,
        default_type=(default.get("name", "Optional") if default is not None else "Optional"))
    patterns = _find(dt, "patterns")
    for pat in _findall(patterns, "pattern"):
        ptype = _find(pat, "type")
        deps = _find_any(pat, "dependencies", "dependancies")
        resolver.patterns.append(TypePattern(
            type=(ptype.get("name", "Optional") if ptype is not None else "Optional"),
            dependency=_parse_dependencies(deps)))
    return resolver


def _parse_option(plugin_elem) -> Option:
    opt = Option(name=plugin_elem.get("name", ""))
    opt.files = _parse_files(_find(plugin_elem, "files"))
    cond_flags = _find(plugin_elem, "conditionFlags")
    if cond_flags is not None:
        for flag in _findall(cond_flags, "flag"):
            opt.set_flags[flag.get("name", "")] = (flag.text or "").strip()
    opt.type_resolver = _parse_type_resolver(plugin_elem)
    return opt


def _parse_group(group_elem) -> Group:
    group = Group(name=group_elem.get("name", ""),
                  group_type=group_elem.get("type", "SelectAny"))
    plugins_container = _find(group_elem, "plugins")
    for plugin_elem in _findall(plugins_container, "plugin"):
        group.plugins.append(_parse_option(plugin_elem))
    return group


def parse_moduleconfig(xml_data) -> FomodConfig:
    """Parse ModuleConfig.xml (bytes, str, or path) into a :class:`FomodConfig`."""
    if isinstance(xml_data, (bytes, bytearray)):
        root = ET.fromstring(xml_data)
    elif isinstance(xml_data, str) and "<" in xml_data:
        root = ET.fromstring(xml_data)
    else:
        root = ET.parse(str(xml_data)).getroot()

    config = FomodConfig()
    name_elem = _find(root, "moduleName")
    if name_elem is not None and name_elem.text:
        config.module_name = name_elem.text.strip()

    # Mod prerequisites gate (moduleDependencies / moduleDependancies)
    prereq = _find_any(root, "moduleDependencies", "moduleDependancies")
    config.mod_prerequisites = _parse_dependencies(prereq)

    config.required_files = _parse_files(_find(root, "requiredInstallFiles"))

    # Install steps. v1/v2 put a single implicit step's groups directly under root
    # in <optionalFileGroups>; v4+ wrap them in <installSteps><installStep>.
    steps_container = _find(root, "installSteps")
    if steps_container is not None:
        for step_elem in _findall(steps_container, "installStep"):
            step = InstallStep(name=step_elem.get("name", ""))
            vis = _find(step_elem, "visible")
            if vis is not None:
                step.visibility = _parse_dependencies(vis)
            groups_container = _find(step_elem, "optionalFileGroups")
            for group_elem in _findall(groups_container, "group"):
                step.groups.append(_parse_group(group_elem))
            config.install_steps.append(step)
    else:
        groups_container = _find(root, "optionalFileGroups")
        if groups_container is not None:
            step = InstallStep(name="")
            for group_elem in _findall(groups_container, "group"):
                step.groups.append(_parse_group(group_elem))
            config.install_steps.append(step)

    # Conditional file installs
    cond_container = _find(root, "conditionalFileInstalls")
    if cond_container is not None:
        patterns = _find(cond_container, "patterns")
        for pattern in _findall(patterns, "pattern"):
            deps = _find_any(pattern, "dependencies", "dependancies")
            config.conditional_installs.append(ConditionalInstall(
                dependency=_parse_dependencies(deps),
                files=_parse_files(_find(pattern, "files"))))

    return config


# --------------------------------------------------------------------------- #
# Condition evaluation (mirrors CompositeCondition / FlagCondition / PluginCondition)
# --------------------------------------------------------------------------- #
def _eval_dependency(dep: Optional[Dependency], flags: Dict[str, str],
                     active: Set[str]) -> bool:
    """Empty And -> True, empty Or -> False (Vortex CompositeCondition semantics)."""
    if dep is None:
        return True
    results: List[bool] = []
    for name, val in dep.flags:
        cur = flags.get(name)
        if val is None or val == "":
            results.append(cur is None or cur == "")     # empty value matches absent/empty
        else:
            results.append(cur == val)                    # case-sensitive equality
    for plugin, state in dep.files:
        present = plugin in active
        s = (state or "Active").lower()
        if s == "missing":
            results.append(not present)
        elif s == "inactive":
            results.append(not present)                   # we model active-set membership
        else:                                             # Active
            results.append(present)
    for child in dep.children:
        results.append(_eval_dependency(child, flags, active))
    if not results:
        return dep.operator != "or"                       # And-empty True, Or-empty False
    return any(results) if dep.operator == "or" else all(results)


# --------------------------------------------------------------------------- #
# Selection bridge (collection preset -> selected options)
# --------------------------------------------------------------------------- #
def selections_from_collection(choices_data: Dict) -> List[Tuple[Optional[str], str, int, str]]:
    """Flatten a collection ``choices`` block -> (step_name, group_name, idx, choice_name)."""
    selections = []
    for option in choices_data.get("options", []):
        step_name = option.get("name")
        for group in option.get("groups", []):
            group_name = group.get("name", "")
            for choice in group.get("choices", []):
                selections.append((step_name, group_name,
                                   int(choice.get("idx", -1)), choice.get("name", "")))
    return selections


def _preset_index(choices_data: Dict):
    """Build lookup: (step_norm, group_norm) -> set(option_name_norm) chosen.
    Also a group-name-only fallback for when the collection omitted step names."""
    by_step_group: Dict[Tuple[str, str], Set[str]] = {}
    by_group: Dict[str, Set[str]] = {}
    for step_name, group_name, _idx, choice_name in selections_from_collection(choices_data):
        cn = _norm(choice_name)
        by_step_group.setdefault((_norm(step_name), _norm(group_name)), set()).add(cn)
        by_group.setdefault(_norm(group_name), set()).add(cn)
    return by_step_group, by_group


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
@dataclass
class ResolveReport:
    chosen_plugins: List[str] = field(default_factory=list)
    unmatched_selections: List[str] = field(default_factory=list)
    flags_set: Dict[str, str] = field(default_factory=dict)
    conditional_patterns_applied: int = 0
    prerequisites_failed: bool = False


def _resolve_file_item(item: FileItem, package_root: Path,
                       lower_index: Dict[str, Path], offset: int) -> List[Tuple[str, str, int]]:
    """Expand one <file>/<folder> -> list of (abs_source, dest_posix, eff_priority).

    Single files get ``item.priority + offset``. Folder CONTENTS use the raw
    ``item.priority`` (the C# folder path does NOT add the phase offset)."""
    out: List[Tuple[str, str, int]] = []
    src_norm = item.source.replace("\\", "/").strip("/").lower()
    dest_norm = item.destination.replace("\\", "/").strip("/")

    if not item.is_folder:
        real = lower_index.get(src_norm)
        if real is None or real.is_dir():
            return out
        dest = dest_norm or real.name
        out.append((str(real), dest, item.priority + offset))
        return out

    strip_components = len(src_norm.split("/")) if src_norm else 0
    prefix = src_norm + "/" if src_norm else ""
    for low_path, real in lower_index.items():
        if real.is_dir():
            continue
        if src_norm and not low_path.startswith(prefix):
            continue
        rel_real = real.relative_to(package_root).as_posix()
        remainder = "/".join(rel_real.split("/")[strip_components:])
        dest = f"{dest_norm}/{remainder}" if dest_norm else remainder
        out.append((str(real), dest.strip("/"), item.priority))   # raw priority for folders
    return out


def resolve_install(config: FomodConfig, choices_data: Dict, package_root,
                    active_plugins=None) -> Tuple[List[FileOperation], ResolveReport]:
    """Resolve the files to install for a FOMOD given a collection's recorded choices.

    ``active_plugins`` is the set of plugins that will be active (the collection's
    declared plugins + the game's active masters). Used to evaluate fileDependency
    and dependency-type conditions exactly as Vortex's PluginCondition does.

    Returns (file_operations, report); one operation per destination, the winner of
    Vortex's priority/conflict rule.
    """
    package_root = Path(package_root)
    report = ResolveReport()
    active: Set[str] = {p.strip().lower() for p in (active_plugins or []) if p}

    lower_index: Dict[str, Path] = {}
    for p in package_root.rglob("*"):
        rel = str(p.relative_to(package_root)).replace("\\", "/").lower()
        lower_index[rel] = p

    flags: Dict[str, str] = {}
    flag_owner: Dict[str, int] = {}
    selected: Set[int] = set()           # ids of selected options
    by_step_group, by_group = _preset_index(choices_data)
    has_preset = bool(by_step_group)

    def enable(opt: Option):
        selected.add(id(opt))
        for fname, fval in opt.set_flags.items():
            flags[fname] = fval
            flag_owner[fname] = id(opt)

    def disable(opt: Option):
        selected.discard(id(opt))
        for fname in [f for f, o in flag_owner.items() if o == id(opt)]:
            flags.pop(fname, None)
            flag_owner.pop(fname, None)

    def is_preset(step: InstallStep, group: Group, opt: Option) -> bool:
        names = by_step_group.get((_norm(step.name), _norm(group.name)))
        if names is None:
            names = by_group.get(_norm(group.name))        # fallback: group-only match
        return bool(names) and _norm(opt.name) in names

    def group_has_preset(step: InstallStep, group: Group) -> bool:
        key = (_norm(step.name), _norm(group.name))
        return key in by_step_group or _norm(group.name) in by_group

    # 0. Mod prerequisites gate (unfulfilled -> nothing installs)
    if not _eval_dependency(config.mod_prerequisites, flags, active):
        report.prerequisites_failed = True
        return [], report

    # 1. fixSteps: a group with >1 Required option loosens AtMostOne/ExactlyOne
    for step in config.install_steps:
        for group in step.groups:
            req = sum(1 for o in group.options
                      if o.type_resolver.resolve(flags, active) == "Required")
            if req > 1 and group.group_type == "SelectAtMostOne":
                group.group_type = "SelectAny"
            elif req > 1 and group.group_type == "SelectExactlyOne":
                group.group_type = "SelectAtLeastOne"

    # 2. Preselect per VISIBLE step, in order (flags accumulate across steps)
    for step in config.install_steps:
        if step.visibility is not None and not _eval_dependency(step.visibility, flags, active):
            continue
        for group in step.groups:
            if any(id(o) in selected for o in group.options):
                continue
            set_first = group.group_type == "SelectExactlyOne"
            for opt in group.options:
                otype = opt.type_resolver.resolve(flags, active)
                preset_hit = is_preset(step, group, opt)
                if preset_hit and otype == "NotUsable":
                    opt.type_resolver = OptionTypeResolver(static_type="CouldBeUsable")
                    otype = "CouldBeUsable"
                enable_it = (otype == "Required"
                             or (not has_preset and otype == "Recommended")
                             or group.group_type == "SelectAll"
                             or preset_hit)
                if enable_it:
                    set_first = False
                    if group_has_preset(step, group) and not preset_hit:
                        disable(opt)         # group has a preset and this isn't in it
                    else:
                        enable(opt)
            if set_first and group.options:
                enable(group.options[0])
        # 3. fixSelected: force Required on, NotUsable off
        for group in step.groups:
            for opt in group.options:
                otype = opt.type_resolver.resolve(flags, active)
                if otype == "Required":
                    enable(opt)
                elif otype == "NotUsable":
                    disable(opt)

    report.flags_set = dict(flags)
    for step in config.install_steps:
        for group in step.groups:
            for opt in group.options:
                if id(opt) in selected:
                    report.chosen_plugins.append(opt.name)

    # 4. Build the file set across the three phases.
    #    required: -1e9, selected (+ alwaysInstall/installIfUsable of unselected): 0,
    #    conditional sets whose condition holds: +1e9.
    OFFSET = 10 ** 9
    phased: List[Tuple[FileItem, int]] = []
    for f in config.required_files:
        phased.append((f, -OFFSET))
    for step in config.install_steps:
        for group in step.groups:
            for opt in group.options:
                if id(opt) in selected:
                    for f in opt.files:
                        phased.append((f, 0))
                else:
                    otype = opt.type_resolver.resolve(flags, active)
                    for f in opt.files:
                        if f.always_install or (f.install_if_usable and otype != "NotUsable"):
                            phased.append((f, 0))
    for cond in config.conditional_installs:
        if _eval_dependency(cond.dependency, flags, active):
            report.conditional_patterns_applied += 1
            for f in cond.files:
                phased.append((f, OFFSET))

    # 5. Expand + resolve same-destination conflicts (higher priority wins; tie ->
    #    lexicographically greater source path).
    winners: Dict[str, Tuple[str, str, int]] = {}   # dest_key -> (abs_source, dest, prio)
    for item, offset in phased:
        for abs_source, dest, prio in _resolve_file_item(item, package_root, lower_index, offset):
            key = dest.lower()
            cur = winners.get(key)
            if cur is None or prio > cur[2] or (prio == cur[2] and abs_source > cur[0]):
                winners[key] = (abs_source, dest, prio)

    ops = [FileOperation(abs_source=a, destination=d, priority=p)
           for (a, d, p) in winners.values()]
    ops.sort(key=lambda o: (o.priority, o.destination))
    return ops, report


def _find_plugin_by_name(config: FomodConfig, name: str) -> Optional[Option]:
    """Lenient option-name match across every group (kept for callers/tests)."""
    target, tnorm = name.strip().lower(), _norm(name)
    allopts = [o for s in config.install_steps for g in s.groups for o in g.options]
    for o in allopts:
        if o.name.strip().lower() == target:
            return o
    if tnorm:
        for o in allopts:
            if _norm(o.name) == tnorm:
                return o
    return None


def find_moduleconfig(extract_path) -> Optional[Path]:
    """Locate ``fomod/ModuleConfig.xml`` (case-insensitive) under an extracted archive."""
    extract_path = Path(extract_path)
    for p in extract_path.rglob("*"):
        if p.is_file() and p.name.lower() == "moduleconfig.xml" \
                and p.parent.name.lower() == "fomod":
            return p
    return None
