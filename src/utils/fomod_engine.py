"""
FOMOD engine: parse a mod's ``fomod/ModuleConfig.xml`` and resolve which files
to install given the choices recorded in a Nexus collection.

This replaces the old heuristic folder-name matching with a real implementation
of the FOMOD installer model:

* Parse install steps -> option groups -> plugins, each with its file list and
  the condition flags it sets.
* Map the collection's recorded selections (group name + 0-based plugin ``idx``)
  onto the parsed plugins. The collection stores exactly which plugins the
  collection author chose, so we don't need to evaluate the interactive UI.
* Resolve the concrete set of files to install: always-installed required files,
  the files of every chosen plugin, and any conditional file installs whose flag
  dependencies are satisfied by the chosen plugins. Folder sources are expanded
  to individual files and source paths are matched case-insensitively against the
  extracted archive (FOMOD paths are Windows-style and case-insensitive).

The engine is intentionally free of installer side effects: it returns a list of
:class:`FileOperation` (absolute source path -> destination relative path, with a
priority for overwrite ordering). The caller performs the actual copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ModuleConfig.xml comes from third-party mod archives, so parse it with the
# hardened defusedxml parser (protects against XXE / billion-laughs). Fall back
# to the stdlib parser if defusedxml isn't installed, following the project's
# defensive-import convention.
try:
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover - defusedxml is a listed dependency
    import xml.etree.ElementTree as ET


# --------------------------------------------------------------------------- #
# Parsed model
# --------------------------------------------------------------------------- #
@dataclass
class FileItem:
    """A <file> or <folder> entry from ModuleConfig.xml."""
    source: str
    destination: str
    priority: int = 0
    is_folder: bool = False
    always_install: bool = False
    install_if_usable: bool = False


@dataclass
class Plugin:
    """A single selectable option within a group."""
    name: str
    files: List[FileItem] = field(default_factory=list)
    set_flags: Dict[str, str] = field(default_factory=dict)


@dataclass
class Group:
    name: str
    group_type: str  # SelectExactlyOne, SelectAtMostOne, SelectAny, SelectAll, SelectAtLeastOne
    plugins: List[Plugin] = field(default_factory=list)


@dataclass
class InstallStep:
    name: str
    groups: List[Group] = field(default_factory=list)


@dataclass
class ConditionalInstall:
    """A <pattern> inside <conditionalFileInstalls>: install files if flags match."""
    required_flags: Dict[str, str]
    files: List[FileItem] = field(default_factory=list)


@dataclass
class FomodConfig:
    module_name: str = ""
    install_steps: List[InstallStep] = field(default_factory=list)
    required_files: List[FileItem] = field(default_factory=list)
    conditional_installs: List[ConditionalInstall] = field(default_factory=list)


@dataclass
class FileOperation:
    """A concrete copy operation produced by the resolver."""
    abs_source: str       # absolute path to the file inside the extracted archive
    destination: str      # path relative to the mod's data root (forward slashes)
    priority: int = 0


# --------------------------------------------------------------------------- #
# XML parsing
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    """Strip any XML namespace from a tag name."""
    return tag.rsplit('}', 1)[-1]


def _find(elem, name):
    for child in elem:
        if _localname(child.tag) == name:
            return child
    return None


def _findall(elem, name):
    return [c for c in elem if _localname(c.tag) == name]


def _parse_files(files_elem) -> List[FileItem]:
    """Parse a <files> container of <file>/<folder> entries."""
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
            # FOMOD: a missing destination installs to the same relative path
            destination = source
        try:
            priority = int(child.get("priority", "0"))
        except ValueError:
            priority = 0
        items.append(FileItem(
            source=source,
            destination=destination,
            priority=priority,
            is_folder=(kind == "folder"),
            always_install=child.get("alwaysInstall", "false").lower() == "true",
            install_if_usable=child.get("installIfUsable", "false").lower() == "true",
        ))
    return items


def _parse_flag_deps(dependencies_elem) -> Dict[str, str]:
    """Collect flagDependency name->value pairs from a <dependencies> tree.

    fileDependency / gameDependency are ignored (we can't reliably know the full
    load-order state at install time); flag dependencies are self-contained
    within the FOMOD and are evaluated exactly.
    """
    flags: Dict[str, str] = {}
    if dependencies_elem is None:
        return flags
    for child in dependencies_elem:
        kind = _localname(child.tag)
        if kind == "flagDependency":
            flags[child.get("flag", "")] = child.get("value", "")
        elif kind == "dependencies":  # nested
            flags.update(_parse_flag_deps(child))
    return flags


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

    # Always-installed files
    req = _find(root, "requiredInstallFiles")
    config.required_files = _parse_files(req)

    # Install steps -> groups -> plugins
    steps_container = _find(root, "installSteps")
    if steps_container is not None:
        for step_elem in _findall(steps_container, "installStep"):
            step = InstallStep(name=step_elem.get("name", ""))
            groups_container = _find(step_elem, "optionalFileGroups")
            if groups_container is not None:
                for group_elem in _findall(groups_container, "group"):
                    group = Group(
                        name=group_elem.get("name", ""),
                        group_type=group_elem.get("type", ""),
                    )
                    plugins_container = _find(group_elem, "plugins")
                    if plugins_container is not None:
                        for plugin_elem in _findall(plugins_container, "plugin"):
                            plugin = Plugin(name=plugin_elem.get("name", ""))
                            plugin.files = _parse_files(_find(plugin_elem, "files"))
                            cond_flags = _find(plugin_elem, "conditionFlags")
                            if cond_flags is not None:
                                for flag in _findall(cond_flags, "flag"):
                                    plugin.set_flags[flag.get("name", "")] = (flag.text or "").strip()
                            group.plugins.append(plugin)
                    step.groups.append(group)
            config.install_steps.append(step)

    # Conditional file installs
    cond_container = _find(root, "conditionalFileInstalls")
    if cond_container is not None:
        patterns = _find(cond_container, "patterns")
        if patterns is not None:
            for pattern in _findall(patterns, "pattern"):
                deps = _find(pattern, "dependencies")
                config.conditional_installs.append(ConditionalInstall(
                    required_flags=_parse_flag_deps(deps),
                    files=_parse_files(_find(pattern, "files")),
                ))

    return config


# --------------------------------------------------------------------------- #
# Selection bridge + resolution
# --------------------------------------------------------------------------- #
def selections_from_collection(choices_data: Dict) -> List[Tuple[Optional[str], str, int, str]]:
    """Flatten a collection's ``choices`` block into selection tuples.

    Returns a list of ``(step_name, group_name, idx, choice_name)``. ``step_name``
    may be None if the collection didn't record it.
    """
    selections = []
    for option in choices_data.get("options", []):
        step_name = option.get("name")
        for group in option.get("groups", []):
            group_name = group.get("name", "")
            for choice in group.get("choices", []):
                selections.append((
                    step_name,
                    group_name,
                    int(choice.get("idx", -1)),
                    choice.get("name", ""),
                ))
    return selections


def _match_group(config: FomodConfig, step_name: Optional[str], group_name: str) -> Optional[Group]:
    """Find the parsed group matching a collection selection.

    Match on group name, disambiguating by step name when provided. Group names
    are compared case-insensitively.
    """
    gname = group_name.strip().lower()
    candidates = []
    for step in config.install_steps:
        for group in step.groups:
            if group.name.strip().lower() == gname:
                candidates.append((step, group))
    if not candidates:
        return None
    if len(candidates) == 1 or not step_name:
        return candidates[0][1]
    sname = step_name.strip().lower()
    for step, group in candidates:
        if step.name.strip().lower() == sname:
            return group
    return candidates[0][1]


@dataclass
class ResolveReport:
    """Diagnostics about how a resolution went, for logging."""
    chosen_plugins: List[str] = field(default_factory=list)
    unmatched_selections: List[str] = field(default_factory=list)
    flags_set: Dict[str, str] = field(default_factory=dict)
    conditional_patterns_applied: int = 0


def _resolve_file_item(item: FileItem, package_root: Path,
                       lower_index: Dict[str, Path]) -> List[FileOperation]:
    """Turn one <file>/<folder> entry into concrete FileOperations.

    Source paths are resolved case-insensitively against ``lower_index`` (a map of
    lowercased archive-relative path -> real path). Folders are expanded to their
    contained files, remapping each onto the destination.
    """
    ops: List[FileOperation] = []
    src_norm = item.source.replace("\\", "/").strip("/").lower()
    dest_norm = item.destination.replace("\\", "/").strip("/")

    if not item.is_folder:
        real = lower_index.get(src_norm)
        if real is None:
            return ops
        dest = dest_norm or real.name
        ops.append(FileOperation(str(real), dest, item.priority))
        return ops

    # Folder: copy every file under the source folder, remapping each file under
    # the destination while preserving the real (cased) sub-path.
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
        ops.append(FileOperation(str(real), dest.strip("/"), item.priority))
    return ops


def resolve_install(config: FomodConfig,
                    choices_data: Dict,
                    package_root) -> Tuple[List[FileOperation], ResolveReport]:
    """Resolve the full set of files to install for a FOMOD given collection choices.

    Args:
        config: parsed ModuleConfig.
        choices_data: the mod's ``choices`` block from the collection.
        package_root: directory containing the ``fomod`` folder (source paths are
            relative to this).

    Returns:
        (file_operations, report). ``file_operations`` is ordered by priority so a
        later op overwrites an earlier one writing to the same destination.
    """
    package_root = Path(package_root)
    report = ResolveReport()

    # Case-insensitive index of everything in the package
    lower_index: Dict[str, Path] = {}
    for p in package_root.rglob("*"):
        rel = str(p.relative_to(package_root)).replace("\\", "/").lower()
        lower_index[rel] = p

    selected_items: List[FileItem] = []
    flags: Dict[str, str] = {}

    # 1. Always-installed required files
    selected_items.extend(config.required_files)

    # 2. Chosen plugins (by idx, validated by name where possible)
    for step_name, group_name, idx, choice_name in selections_from_collection(choices_data):
        group = _match_group(config, step_name, group_name)
        if group is None or idx < 0 or idx >= len(group.plugins):
            # Fall back to matching by plugin name within any group
            plugin = _find_plugin_by_name(config, choice_name)
            if plugin is None:
                report.unmatched_selections.append(f"{group_name}[{idx}] {choice_name}")
                continue
        else:
            plugin = group.plugins[idx]
            # Validate idx against recorded name; prefer a name match if they diverge
            if choice_name and plugin.name.strip().lower() != choice_name.strip().lower():
                by_name = next((p for p in group.plugins
                                if p.name.strip().lower() == choice_name.strip().lower()), None)
                if by_name is not None:
                    plugin = by_name
        report.chosen_plugins.append(plugin.name)
        selected_items.extend(plugin.files)
        flags.update(plugin.set_flags)

    report.flags_set = dict(flags)

    # 3. Conditional installs whose flag dependencies are satisfied
    for cond in config.conditional_installs:
        if all(flags.get(k) == v for k, v in cond.required_flags.items()):
            selected_items.extend(cond.files)
            report.conditional_patterns_applied += 1

    # 4. Expand to concrete file operations, ordered by priority (stable)
    ops: List[FileOperation] = []
    for item in selected_items:
        ops.extend(_resolve_file_item(item, package_root, lower_index))
    ops.sort(key=lambda o: o.priority)
    return ops, report


def _find_plugin_by_name(config: FomodConfig, name: str) -> Optional[Plugin]:
    if not name:
        return None
    target = name.strip().lower()
    for step in config.install_steps:
        for group in step.groups:
            for plugin in group.plugins:
                if plugin.name.strip().lower() == target:
                    return plugin
    return None


def find_moduleconfig(extract_path) -> Optional[Path]:
    """Locate ``fomod/ModuleConfig.xml`` (case-insensitive) under an extracted archive."""
    extract_path = Path(extract_path)
    for p in extract_path.rglob("*"):
        if p.is_file() and p.name.lower() == "moduleconfig.xml" \
                and p.parent.name.lower() == "fomod":
            return p
    return None
