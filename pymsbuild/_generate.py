import os
import sys

from pathlib import PurePath, Path
from ._types import Package, PydFile, File, LiteralXML, Property, ItemDefinition, ConditionalValue
from ._writer import ProjectFileWriter

LIBPATH = Path(sys.base_prefix) / "libs"
INCPATH = Path(sys.base_prefix) / "include"
SEP = os.path.sep

class GroupSwitcher:
    def __init__(self, project):
        self.project = project
        self.tag = None
        self._cm = None

    def switch_to(self, tag):
        if tag == self.tag:
            return
        if self._cm:
            self._cm.__exit__(None, None, None)
            self._cm = None
        if tag:
            self._cm = self.project.group(tag)
            self._cm.__enter__()
        self.tag = tag

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if self._cm:
            return self._cm.__exit__(*exc_info)


def _resolve_wildcards(basename, source, pattern):
    """Find all files matching 'pattern' under 'source'.

Returns a sequence of (generated name, path), where each element may be
a Path, PurePath, or str. If there are no wildcards in 'pattern', the
generated name is 'basename' unmodified and the path is
"source / pattern". Otherwise, 'basename' is reduced by the same number
of segments as there are in 'pattern', and the generated name for each
file will be the recursive path appended to the shortened base.

If 'pattern' is an absolute path, it is split at the first wildcard
segment and the first part becomes 'source'. 'basename' must have at
least as many segments as remain in 'pattern'.
"""
    basename = PurePath(basename)
    pattern = PurePath(pattern)
    if not any("*" in p or "?" in p for p in pattern.parts):
        yield basename, source / pattern
        return

    if pattern.is_absolute():
        for i, p in enumerate(pattern.parts):
            if "*" in p or "?" in p:
                source = Path(*pattern.parts[:i])
                pattern = PurePath(*pattern.parts[i:])
                break

    if len(basename.parts) < len(pattern.parts):
        raise ValueError(f"basename ({basename}) should be at least as long as pattern ({pattern})")

    basename = PurePath(*basename.parts[:-len(pattern.parts)])
    roots = [(basename, source)]
    for i, r in enumerate(pattern.parts[:-1]):
        if r == "**":
            wildcards = str(PurePath(*pattern.parts[i + 1:]))
            yield from ((bn / p.relative_to(d), p)
                        for bn, d in roots
                        for p in d.rglob("*")
                        if not p.is_dir() and p.relative_to(d).match(wildcards))
            return
        roots = [((bn / p.name), p) for bn, d in roots for p in d.glob(r) if p.is_dir()]

    r = pattern.parts[-1]
    yield from ((bn / p.name, p) for bn, d in roots for p in d.glob(r) if not p.is_dir())


def _all_members(item, recurse_if=None, return_if=None, *, prefix=""):
    if not return_if or return_if(item):
        yield "{}{}".format(prefix, item.name), item
    if not recurse_if or recurse_if(item):
        for m in item.members:
            yield from _all_members(
                m,
                recurse_if,
                return_if,
                prefix="{}{}/".format(prefix, item.name),
            )


def _write_members(f, source_dir, members):
    with GroupSwitcher(f) as g:
        for n, p in members:
            if isinstance(p, File):
                g.switch_to("ItemGroup")
                wrote_any = bool(p.options.get("allow_none"))
                flat_char = p.options.get("flatten")
                name = p.options.get("Name")
                exclude = ()
                condition = None
                if getattr(p, "has_condition", False):
                    condition = getattr(p, "condition", None)
                    pattern = getattr(p, "exclude", None)
                    if pattern:
                        exclude = {p2 for n2, p2 in _resolve_wildcards(n, source_dir, pattern)}
                for n2, p2 in _resolve_wildcards(n, source_dir, p.source):
                    if p2 in exclude:
                        continue
                    if isinstance(name, str):
                        n2 = n2.with_name(name)
                    if flat_char is True:
                        n2 = n2.parts[-1]
                    elif isinstance(flat_char, str):
                        n2 = flat_char.join(n2.parts)
                    options = {
                        "SourceDir": source_dir,
                        **p.options,
                        "Name": n2,
                        "allow_none": None,
                        "flatten": None,
                    }
                    if name is not None and not isinstance(name, str):
                        options["Name"] = name
                    if condition:
                        p2 = ConditionalValue(p2, condition=condition)
                    f.add_item(p._ITEMNAME, p2, **options)
                    wrote_any = True
                if not wrote_any:
                    raise ValueError("failed to find any files for {} in {}".format(
                        p.source, source_dir))
            elif isinstance(p, Property):
                g.switch_to("PropertyGroup")
                f.add_property(p.name, p.value)
            elif isinstance(p, ItemDefinition):
                g.switch_to("ItemDefinitionGroup")
                with f.group(p.kind):
                    for k, v in p.options.items():
                        f.add_item_property(p.kind, k, v)
            elif isinstance(p, LiteralXML):
                g.switch_to(None)
                f.add_text(p.xml)
            elif hasattr(p, "write_member"):
                p.write_member(f, g)


def _generate_pyd(project, build_dir, root_dir):
    build_dir = Path(build_dir)
    proj = build_dir / "{}.proj".format(project.name)
    root_dir = Path(root_dir)
    source_dir = root_dir / project.source

    if project.project_file:
        return Path(project.project_file)

    tpath = project.options.get("TargetName", project.name) + (project.options.get("TargetExt") or ".pyd")
    tname, tdot, text = tpath.rpartition(".")
    with ProjectFileWriter(proj, tname, vc_platforms=True, root_namespace=project.name) as f:
        with f.group("PropertyGroup", Label="Globals"):
            f.add_property("SourceDir", ConditionalValue(source_dir, if_empty=True))
            f.add_property("SourceRootDir", ConditionalValue(root_dir, if_empty=True))
            f.add_property("__TargetExt", tdot + text)
            for k, v in project.options.items():
                if k not in {"ConfigurationType", "TargetExt"}:
                    f.add_property(k, v)
        f.add_import(f"$(PyMsbuildTargets){SEP}common.props")
        f.add_import(f"$(PyMsbuildTargets){SEP}cpp-default-$(Platform).props")
        with f.group("PropertyGroup", Label="Configuration"):
            f.add_property("ConfigurationType", project.options.get("ConfigurationType", "DynamicLibrary"))
            f.add_property("PlatformToolset", "$(DefaultPlatformToolset)")
            f.add_property("BasePlatformToolset", "$(DefaultPlatformToolset)")
            f.add_property("CharacterSet", "Unicode")
        f.add_import(f"$(PyMsbuildTargets){SEP}cpp-$(Platform).props")
        f.add_import(f"$(PyMsbuildTargets){SEP}pyd.props")

        _write_members(f, source_dir, _all_members(project, recurse_if=lambda m: m is project))
        for n, p in _all_members(project, recurse_if=lambda m: m is project, return_if=lambda m: isinstance(m, Package)):
            _write_members(
                f,
                source_dir,
                _all_members(p, recurse_if=lambda m: not isinstance(m, PydFile), prefix=f"{project.name}/")
            )

        f.add_import(f"$(PyMsbuildTargets){SEP}common.targets")
        f.add_import(f"$(PyMsbuildTargets){SEP}cpp-$(Platform).targets")
        f.add_import(f"$(PyMsbuildTargets){SEP}pyd.targets")

    return proj


def _generate_pyd_reference_metadata(relname, project, source_dir):
    output = project.options.get("TargetName", relname.stem) + project.options.get("TargetExt", ".pyd")
    tname, tdot, text = output.rpartition(".")
    return {
        **dict(
            TargetDir=relname.parent,
            IntDir="$(IntDir)" + tname,
        ),
        **project.options,
        **dict(
            TargetName=tname,
            TargetExt=tdot + text,
            SourceDir=source_dir / project.source,
        ),
    }


def generate(project, build_dir, source_dir, config_file=None):
    build_dir = Path(build_dir)
    root_dir = Path(source_dir)
    config_file = root_dir / (config_file or "_msbuild.py")
    source_dir = root_dir / project.source
    proj = build_dir / "{}.proj".format(project.name)

    if project.project_file:
        return Path(project.project_file)

    if isinstance(project, PydFile):
        return _generate_pyd(project, build_dir, root_dir)

    with ProjectFileWriter(proj, project.name) as f:
        with f.group("PropertyGroup"):
            f.add_property("SourceDir", ConditionalValue(source_dir, if_empty=True))
            f.add_property("SourceRootDir", ConditionalValue(root_dir, if_empty=True))
            for k, v in project.options.items():
                f.add_property(k, v)
        f.add_import(f"$(PyMsbuildTargets){SEP}common.props")
        f.add_import(f"$(PyMsbuildTargets){SEP}package.props")
        with f.group("ItemGroup", Label="ProjectReferences"):
            for n, p in _all_members(project, return_if=lambda m: m is not project and isinstance(m, PydFile)):
                fn = PurePath(n)
                pdir = _generate_pyd(p, build_dir, source_dir)
                try:
                    pdir = pdir.relative_to(proj.parent)
                except ValueError:
                    pass
                f.add_item(
                    "Project",
                    pdir,
                    Name=n,
                    **_generate_pyd_reference_metadata(fn, p, source_dir),
                )
        with f.group("ItemGroup", Label="Sdist metadata"):
            f.add_item("Sdist", build_dir / "PKG-INFO", RelativeSource="PKG-INFO")
            f.add_item("Sdist", config_file, RelativeSource="_msbuild.py")
            f.add_item("Sdist", root_dir / "pyproject.toml", RelativeSource="pyproject.toml")
        _write_members(f, source_dir, _all_members(
            project,
            recurse_if=lambda m: not isinstance(m, PydFile),
        ))
        f.add_import(f"$(PyMsbuildTargets){SEP}common.targets")
        f.add_import(f"$(PyMsbuildTargets){SEP}package.targets")

    return proj


def _write_metadata(f, key, value, source_dir):
    if not isinstance(value, str) and hasattr(value, "__iter__"):
        for v in value:
            _write_metadata(f, key, v, source_dir)
        return
    if isinstance(value, File):
        value = (source_dir / value.source).read_text(encoding="utf-8")
    if "\n" in value:
        value = value.replace("\n", "\n       |")
    print(key, value, sep=": ", file=f)


def _write_metadata_description(f, value, source_dir):
    if not isinstance(value, str) and hasattr(value, "__iter__"):
        for v in value:
            _write_metadata_description(f, v, source_dir)
        return
    if isinstance(value, File):
        value = (source_dir / value.source).read_text(encoding="utf-8")
    print(file=f)
    print(value, file=f)


def generate_distinfo(distinfo, build_dir, source_dir):
    build_dir.mkdir(parents=True, exist_ok=True)
    with (build_dir / "PKG-INFO").open("w", encoding="utf-8") as f:
        description = None
        for k, vv in distinfo.items():
            if k.casefold() == "description".casefold():
                description = vv
                continue
            _write_metadata(f, k, vv, source_dir)
        if description:
            _write_metadata_description(f, description, source_dir)

def readback_distinfo(pkg_info):
    distinfo = []
    description = None
    with open(pkg_info, "r", encoding="utf-8") as f:
        for line in f:
            if description is not None:
                description.append(line)
                continue
            line = line.rstrip()
            if not line:
                description = []
                continue
            if line.startswith(("       |", "        ")):
                distinfo[-1] = (distinfo[-1][0], distinfo[-1][1] + "\n" + line[8:])
                continue
            key, _, value = line.partition(":")
            distinfo.append((key.strip(), value.lstrip()))
    d = {}
    for k, v in distinfo:
        r = d.get(k, None)
        if isinstance(r, list):
            r.append(v)
        elif r is not None and r is not v:
            d[k] = [r, v]
        else:
            d[k] = v
    if description:
        d["Description"] = "".join(description).rstrip()
    return d
