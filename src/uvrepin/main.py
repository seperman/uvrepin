#!/usr/bin/env python3
"""
main.py

Repin all *direct* deps in pyproject.toml to the latest exact versions using uv 0.7.8.
Dry run prints: NAME (GROUP)  FROM -> TO

Usage:
  python main.py --dry-run
  python main.py            # update pyproject.toml (no env changes)
  python main.py --sync     # also update the environment
  python main.py --only-groups main,dev
  python main.py --pre      # allow pre-releases
  python main.py --index https://pypi.org/simple   # repeatable
"""
import argparse, json, os, pathlib, re, shlex, subprocess, sys, urllib.request
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tomllib  # Python 3.11+
except Exception:
    sys.stderr.write("Needs Python 3.11+ (tomllib).\n"); sys.exit(1)

PYPROJECT = pathlib.Path("pyproject.toml")

@dataclass
class WorkspaceConflict:
    """Represents a package version conflict across workspace members."""
    package_name: str
    extra_name: str
    conflicts: dict[str, str]  # member_name -> version

@dataclass 
class ConflictResolution:
    """Represents the resolution plan for workspace conflicts."""
    extra_name: str
    conflicts: list[WorkspaceConflict]
    target_versions: dict[str, str]  # package_name -> target_version
    affected_members: set[str]

def die(msg: str, code: int = 1):
    sys.stderr.write(msg.rstrip()+"\n"); raise SystemExit(code)

def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

# Parse "pkg[extra]==1.2.3; marker"
_OP = r"(==|!=|<=|>=|~=|===|<|>)"
def parse_req(req: str):
    s = req.strip()
    if not s or s.startswith("#"): return None
    if "@" in s or s.startswith(("file:", "path:", "git+", "hg+", "svn+")):
        return ("SKIP", "", None, None)
    marker = None
    if ";" in s:
        left, marker = s.split(";", 1)
        marker = marker.strip()
    else:
        left = s
    m = re.match(
        r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?P<extras>\[[^\]]+\])?"
        r"\s*(?P<op>%s)?\s*(?P<ver>[^;\s]+)?\s*$" % _OP,
        left.strip(),
    )
    if not m: return None
    name = m.group("name"); extras = m.group("extras") or ""
    op = m.group("op"); ver = m.group("ver") if op == "==" else None
    return (name, extras, ver, marker)

def read_pyproject():
    if not PYPROJECT.exists(): die(f"Couldn't find {PYPROJECT.resolve()}")
    with PYPROJECT.open("rb") as f: return tomllib.load(f)

def gather_direct(data: dict) -> tuple[dict[str|None, list[dict]], dict[str, bool]]:
    """Return (groups_dict, is_optional_dict) where is_optional_dict tracks which groups are optional-dependencies."""
    out = {}
    is_optional = {}
    proj = data.get("project", {})
    deps = proj.get("dependencies", []) or []
    if deps:
        out[None] = []
        for r in deps:
            p = parse_req(r)
            if p and p[0] != "SKIP":
                name, extras, pinned, marker = p
                out[None].append(dict(raw=r, name=name, extras=extras, pinned=pinned, marker=marker))
    
    # Handle project.optional-dependencies
    optional_deps = proj.get("optional-dependencies", {}) or {}
    for gname, arr in optional_deps.items():
        if not isinstance(arr, list): continue
        group = []
        for r in arr:
            p = parse_req(r)
            if p and p[0] != "SKIP":
                name, extras, pinned, marker = p
                group.append(dict(raw=r, name=name, extras=extras, pinned=pinned, marker=marker))
        if group:
            out[gname] = group
            is_optional[gname] = True
    
    # Handle dependency-groups (PEP 735)
    dep_groups = data.get("dependency-groups", {}) or {}
    for gname, arr in dep_groups.items():
        if not isinstance(arr, list): continue
        group = []
        for r in arr:
            p = parse_req(r)
            if p and p[0] != "SKIP":
                name, extras, pinned, marker = p
                group.append(dict(raw=r, name=name, extras=extras, pinned=pinned, marker=marker))
        if group:
            out[gname] = group
            is_optional[gname] = False
    return out, is_optional

class UvRunner:
    """Abstraction for running uv commands, making them easier to mock in tests."""
    
    def run(self, *args: str, capture=False, check=True):
        return subprocess.run(args, text=True, capture_output=capture, check=check)

# Global instance for ease of use
uv_runner = UvRunner()

def run(*args: str, capture=False, check=True):
    return uv_runner.run(*args, capture=capture, check=check)

def ensure_uv():
    try: run("uv", "--version", check=True)
    except Exception: die("uv not found on PATH.")

def parse_workspace_conflict(stderr: str) -> Optional[list[WorkspaceConflict]]:
    """Parse workspace conflict from uv stderr output."""
    if "No solution found when resolving dependencies" not in stderr:
        return None
    
    conflicts = []
    
    # Normalize whitespace for easier pattern matching
    normalized = re.sub(r'\s+', ' ', stderr)
    
    # Pattern to match conflicts like:
    # Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0
    conflict_pattern1 = r"Because ([^[]+)\[([^\]]+)\] depends on ([^=]+)==([^\s]+) and ([^[]+)\[([^\]]+)\] depends on ([^=]+)==([^\s,]+)"
    
    # Pattern to match conflicts like the new uv format:
    # Because common depends on pydantic==2.11.7 and qluster-sdk[dev] depends on pydantic==2.11.5, we can conclude that common[dev] and qluster-sdk[dev] are incompatible.
    conflict_pattern2 = r"Because ([a-zA-Z0-9_-]+) depends on ([^=]+)==([^\s,]+) and ([a-zA-Z0-9_-]+)\[([^\]]+)\] depends on ([^=]+)==([^\s,]+).*?([a-zA-Z0-9_-]+)\[(\w+)\] and ([a-zA-Z0-9_-]+)\[(\w+)\] are incompatible"
    
    # Try first pattern (original format)
    for match in re.finditer(conflict_pattern1, normalized):
        member1, extra1, pkg1, ver1, member2, extra2, pkg2, ver2 = match.groups()
        
        # Only handle same extra name and same package
        if extra1 == extra2 and pkg1 == pkg2:
            conflicts.append(WorkspaceConflict(
                package_name=pkg1.strip(),
                extra_name=extra1.strip(), 
                conflicts={
                    member1.strip(): ver1.strip(),
                    member2.strip(): ver2.strip()
                }
            ))
    
    # Try second pattern (newer uv format)
    for match in re.finditer(conflict_pattern2, normalized):
        member1, pkg1, ver1, member2, extra2, pkg2, ver2, member1_extra, extra1, member2_extra, extra2_full = match.groups()
        
        # Verify the incompatible part matches our members and the extra names match
        if (member1_extra == member1 and member2_extra == member2 and 
            extra1 == extra2_full and pkg1 == pkg2):
            conflicts.append(WorkspaceConflict(
                package_name=pkg1.strip(),
                extra_name=extra1.strip(),
                conflicts={
                    member1.strip(): ver1.strip(),
                    member2.strip(): ver2.strip()
                }
            ))
    
    return conflicts

def get_latest_version(package_name: str, indexes: list[str], allow_pre: bool) -> str:
    """Get the latest version of a package by querying PyPI directly."""
    version = query_pypi_latest(package_name, allow_pre)
    return version if version else "unknown"

def determine_target_versions(conflicts: list[WorkspaceConflict], policy: str = "latest") -> dict[str, str]:
    """Determine target versions for conflicting packages."""
    target_versions = {}
    
    for conflict in conflicts:
        if policy == "latest":
            # Use the latest available version
            latest = get_latest_version(conflict.package_name, [], False)
            if latest != "unknown":
                target_versions[conflict.package_name] = latest
            else:
                # Fallback to max of existing versions
                versions = list(conflict.conflicts.values())
                target_versions[conflict.package_name] = max(versions)
        elif policy == "max":
            # Use the highest version among existing pins
            versions = list(conflict.conflicts.values()) 
            target_versions[conflict.package_name] = max(versions)
        else:
            raise ValueError(f"Unknown policy: {policy}")
    
    return target_versions

def is_ci_environment() -> bool:
    """Check if running in CI environment."""
    return os.getenv("CI", "").lower() in ("true", "1", "yes")

def prompt_user_for_conflict_resolution(conflicts: list[WorkspaceConflict], target_versions: dict[str, str]) -> bool:
    """Prompt user to resolve workspace conflicts. Returns True if user accepts."""
    if not conflicts:
        return False
    
    extra_name = conflicts[0].extra_name
    member_count = len(set().union(*[c.conflicts.keys() for c in conflicts]))
    
    print(f"\nConflicts detected in extra \"{extra_name}\" across {member_count} members:")
    
    for conflict in conflicts:
        member_versions = []
        for member, version in conflict.conflicts.items():
            member_versions.append(f"{member}(=={version})")
        conflict_str = " ↔ ".join(member_versions)
        target_version = target_versions.get(conflict.package_name, "unknown")
        print(f"  {conflict.package_name}: {conflict_str} → {target_version}")
    
    print(f"Align all pyproject.toml files to target versions (latest) and retry lock? [y/N] ", end="")
    response = input().strip().lower()
    return response in ('y', 'yes')

def show_manual_resolution_help(conflicts: list[WorkspaceConflict]) -> None:
    """Show manual resolution commands when user declines auto-resolution."""
    print("\nTo manually resolve these conflicts, align the versions in each member's pyproject.toml:")
    
    extra_name = conflicts[0].extra_name if conflicts else "dev"
    affected_members = set().union(*[c.conflicts.keys() for c in conflicts])
    
    print(f"\nSuggested commands to align extra '{extra_name}':")
    for member in sorted(affected_members):
        for conflict in conflicts:
            if member in conflict.conflicts:
                print(f"  uv add --project {member} --optional {extra_name} {conflict.package_name}==<target_version>")
    
    print("\nThen run: uv lock")

def find_package_location_in_member(member: str, package_name: str) -> str | None:
    """Find where a package is defined in a workspace member's pyproject.toml.

    Returns:
        None if package is in main dependencies
        Group name (str) if package is in optional-dependencies or dependency-groups
    """
    member_pyproject = pathlib.Path(member) / "pyproject.toml"
    if not member_pyproject.exists():
        return None

    try:
        with member_pyproject.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None

    norm_name = pep503(package_name)
    proj = data.get("project", {})

    # Check main dependencies first
    main_deps = proj.get("dependencies", []) or []
    for dep in main_deps:
        parsed = parse_req(dep)
        if parsed and parsed[0] != "SKIP" and pep503(parsed[0]) == norm_name:
            return None  # Found in main deps

    # Check optional-dependencies
    optional_deps = proj.get("optional-dependencies", {}) or {}
    for group_name, deps in optional_deps.items():
        if not isinstance(deps, list):
            continue
        for dep in deps:
            parsed = parse_req(dep)
            if parsed and parsed[0] != "SKIP" and pep503(parsed[0]) == norm_name:
                return group_name  # Found in optional group

    # Check dependency-groups (PEP 735)
    dep_groups = data.get("dependency-groups", {}) or {}
    for group_name, deps in dep_groups.items():
        if not isinstance(deps, list):
            continue
        for dep in deps:
            parsed = parse_req(dep)
            if parsed and parsed[0] != "SKIP" and pep503(parsed[0]) == norm_name:
                return f"group:{group_name}"  # Found in dependency group

    return None  # Not found, assume main


def align_workspace_members(resolution: ConflictResolution, sync: bool, indexes: list[str], allow_pre: bool) -> bool:
    """Align workspace members to resolve conflicts. Returns True if successful."""
    print("\nAligning workspace members...")

    # Stage changes in each affected member
    for member in sorted(resolution.affected_members):
        # Group specs by their location (main, optional, or dependency-group)
        main_specs = []
        optional_specs: dict[str, list[str]] = {}  # group_name -> specs
        group_specs: dict[str, list[str]] = {}  # group_name -> specs

        for conflict in resolution.conflicts:
            if member in conflict.conflicts:
                target_version = resolution.target_versions[conflict.package_name]
                spec = f"{conflict.package_name}=={target_version}"

                location = find_package_location_in_member(member, conflict.package_name)
                if location is None:
                    main_specs.append(spec)
                elif location.startswith("group:"):
                    group_name = location[6:]
                    group_specs.setdefault(group_name, []).append(spec)
                else:
                    optional_specs.setdefault(location, []).append(spec)

        # Run uv add for main dependencies (use --frozen to skip resolution until all members updated)
        if main_specs:
            cmd = ["uv", "add", "--project", member, "--frozen"] + main_specs
            print("Running:", " ".join(shlex.quote(x) for x in cmd))
            try:
                result = run(*cmd, capture=True, check=False)
                if result.returncode != 0:
                    print(f"Failed to stage main deps for member '{member}'")
                    if result.stderr:
                        sys.stderr.write(result.stderr)
                    return False
            except subprocess.CalledProcessError as e:
                print(f"Failed to stage main deps for member '{member}': {e}")
                return False

        # Run uv add for optional dependencies
        for opt_group, specs in optional_specs.items():
            cmd = ["uv", "add", "--project", member, "--frozen", "--optional", opt_group] + specs
            print("Running:", " ".join(shlex.quote(x) for x in cmd))
            try:
                result = run(*cmd, capture=True, check=False)
                if result.returncode != 0:
                    print(f"Failed to stage optional deps for member '{member}'")
                    if result.stderr:
                        sys.stderr.write(result.stderr)
                    return False
            except subprocess.CalledProcessError as e:
                print(f"Failed to stage optional deps for member '{member}': {e}")
                return False

        # Run uv add for dependency groups
        for dep_group, specs in group_specs.items():
            cmd = ["uv", "add", "--project", member, "--frozen", "--group", dep_group] + specs
            print("Running:", " ".join(shlex.quote(x) for x in cmd))
            try:
                result = run(*cmd, capture=True, check=False)
                if result.returncode != 0:
                    print(f"Failed to stage group deps for member '{member}'")
                    if result.stderr:
                        sys.stderr.write(result.stderr)
                    return False
            except subprocess.CalledProcessError as e:
                print(f"Failed to stage group deps for member '{member}': {e}")
                return False

    # Run uv lock
    print("Running: uv lock")
    try:
        result = run("uv", "lock", capture=True, check=False)
        if result.returncode != 0:
            print("uv lock failed after alignment. Files have been modified.")
            if result.stderr:
                sys.stderr.write(result.stderr)
            return False
    except subprocess.CalledProcessError as e:
        print(f"uv lock failed after alignment: {e}")
        print("Files have been modified but lock failed.")
        return False

    # Optionally sync
    if sync:
        print("Running: uv sync")
        try:
            result = run("uv", "sync", capture=True, check=False)
            if result.returncode != 0:
                print("uv sync failed but lock succeeded. Environment may be inconsistent.")
                if result.stderr:
                    sys.stderr.write(result.stderr)
                return False
        except subprocess.CalledProcessError as e:
            print(f"uv sync failed: {e}")
            print("Lock succeeded but sync failed. Environment may be inconsistent.")
            return False

    return True

def parse_outdated_table(text: str) -> dict[str, str]:
    """Parse `uv pip list --outdated` into {normalized_name: latest_version}."""
    latest = {}
    lines = [ln for ln in (ln.strip() for ln in text.splitlines()) if ln]
    # find header row
    start = 0
    for i, ln in enumerate(lines):
        if re.search(r"\bPackage\b", ln) and re.search(r"\bLatest\b", ln):
            start = i + 1; break
    for ln in lines[start:]:
        if set(ln) == {"-"}: continue
        cols = re.split(r"\s{2,}", ln)
        if len(cols) < 3: continue
        name, _installed, latest_ver = cols[:3]
        # Clean up version string - remove trailing non-version text like 'wheel'
        latest_ver = latest_ver.split()[0] if latest_ver else latest_ver
        latest[pep503(name)] = latest_ver
    return latest


def query_pypi_latest(package_name: str, allow_pre: bool = False) -> str | None:
    """Query PyPI API directly to get the latest version of a package.

    Returns the latest stable version, or latest pre-release if allow_pre is True.
    Returns None if the package cannot be found or there's an error.
    """
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if allow_pre:
                # Return the absolute latest version
                return data["info"]["version"]
            else:
                # Filter out pre-releases and return latest stable
                latest = data["info"]["version"]
                releases = data.get("releases", {})
                # Check if latest is a pre-release
                pre_patterns = re.compile(r"(a|b|rc|alpha|beta|dev|pre)\d*", re.IGNORECASE)
                if pre_patterns.search(latest):
                    # Find the latest stable version
                    stable_versions = []
                    for ver in releases.keys():
                        if not pre_patterns.search(ver) and releases[ver]:  # has files
                            stable_versions.append(ver)
                    if stable_versions:
                        # Sort by version (simple string sort works for most cases)
                        # For more accurate sorting, would need packaging.version
                        try:
                            from packaging.version import Version
                            stable_versions.sort(key=Version, reverse=True)
                        except ImportError:
                            stable_versions.sort(reverse=True)
                        return stable_versions[0]
                return latest
    except Exception:
        return None


def query_pypi_batch(package_names: list[str], allow_pre: bool = False, max_workers: int = 10) -> dict[str, str]:
    """Query PyPI for latest versions of multiple packages in parallel.

    Returns a dict mapping normalized package names to their latest versions.
    """
    latest_map = {}

    def fetch_one(name: str) -> tuple[str, str | None]:
        return (pep503(name), query_pypi_latest(name, allow_pre))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, name): name for name in package_names}
        for future in as_completed(futures):
            try:
                norm_name, version = future.result()
                if version:
                    latest_map[norm_name] = version
            except Exception:
                pass  # Skip packages that fail

    return latest_map

def build_uv_add_base(group: str|None, frozen: bool, allow_pre: bool, indexes: list[str], is_optional: bool = False) -> list[str]:
    """Build the base uv add command.

    Args:
        frozen: If True, use --frozen to skip resolution (for workspace compatibility).
                The caller must run `uv lock` after all updates.
    """
    args = ["uv", "add"]
    if frozen:
        args.append("--frozen")
    if group is not None:
        if is_optional:
            args += ["--optional", group]
        else:
            args += ["--group", group]
    if allow_pre: args += ["--prerelease", "always"]
    for idx in indexes: args += ["--index", idx]
    return args

def main():
    ap = argparse.ArgumentParser(description="Repin direct deps to latest exact versions with uv 0.7.8.")
    ap.add_argument("--dry-run", action="store_true", help="Only show what would change.")
    ap.add_argument("--sync", action="store_true", help="Also update the environment.")
    ap.add_argument("--only-groups", default="", help="Comma list; use 'main' for [project.dependencies].")
    ap.add_argument("--pre", action="store_true", help="Include pre-releases.")
    ap.add_argument("--index", action="append", default=[], help="Additional index URL(s).")
    ap.add_argument("--yes", "-y", action="store_true", help="Auto-accept workspace conflict resolution prompts.")
    args = ap.parse_args()

    ensure_uv()
    data = read_pyproject()
    groups, is_optional_map = gather_direct(data)
    if not groups:
        print("No direct dependencies found."); return 0

    if args.only_groups:
        wanted = {g.strip() for g in args.only_groups.split(",") if g.strip()}
        groups = {g: lst for g, lst in groups.items()
                  if (("main" in wanted and g is None) or (g is not None and g in wanted))}
        if not groups:
            print("No matching groups after --only-groups."); return 0

    # Collect all pinned package names to query PyPI
    pinned_packages = set()
    for gname, deps in groups.items():
        for d in deps:
            if d.get("pinned"):
                pinned_packages.add(d["name"])

    if not pinned_packages:
        print("No pinned dependencies (==version) found. Nothing to update.")
        return 0

    # Query PyPI directly to get latest versions (works even if packages aren't installed)
    print(f"Querying PyPI for latest versions of {len(pinned_packages)} packages...")
    latest_map = query_pypi_batch(list(pinned_packages), allow_pre=args.pre)

    if not latest_map:
        print("Failed to query PyPI for any packages. Check your network connection.")
        return 1

    # Build plan: only deps that are pinned (==) and have a newer latest known.
    plan = []  # (group, dep_dict, latest_ver)
    for gname, deps in groups.items():
        for d in deps:
            if not d.get("pinned"): continue
            latest = latest_map.get(pep503(d["name"]))
            if latest and latest != d["pinned"]:
                plan.append((gname, d, latest))

    # Dry-run output
    if args.dry_run:
        if not plan:
            print("Dry run: all pinned dependencies are already at their latest versions.")
            return 0
        print("\nDry run — would update these direct dependencies:\n")
        print("GROUP".ljust(12), "PACKAGE".ljust(38), "FROM".ljust(18), "TO")
        print("-"*86)
        for gname, d, latest in plan:
            group = "main" if gname is None else gname
            pkg = d["name"] + d["extras"]
            if d["marker"]: pkg += f"; {d['marker']}"
            print(group.ljust(12), pkg.ljust(38), (d["pinned"] or "?").ljust(18), latest)
        print("\n(No files changed.)")
        return 0

    if not plan:
        print("All pinned dependencies are already at their latest versions. Nothing to do.")
        return 0

    # Execute per-group with explicit ==version pins.
    # Use --frozen to skip resolution during updates (important for workspaces).
    # We'll run uv lock once at the end after all pyproject.toml files are updated.
    rc = 0
    for gname in list(groups.keys()):
        to_update = [(d, latest) for (g, d, latest) in plan if g == gname]
        if not to_update: continue
        is_opt = is_optional_map.get(gname, False) if gname is not None else False
        base = build_uv_add_base(gname, frozen=True, allow_pre=args.pre, indexes=args.index, is_optional=is_opt)
        reqs = []
        for d, latest in to_update:
            spec = d["name"] + d["extras"] + f"=={latest}"
            if d["marker"]: spec += f"; {d['marker']}"
            reqs.append(spec)
        cmd = base + reqs
        print("Running:", " ".join(shlex.quote(x) for x in cmd))
        try:
            res = run(*cmd, capture=True, check=False)
            if res.returncode != 0:
                print(f"Failed to update dependencies")
                if res.stderr:
                    sys.stderr.write(res.stderr)
                rc = 1
            
        except subprocess.CalledProcessError as e:
            rc = rc or e.returncode
            if e.stderr:
                sys.stderr.write(e.stderr)

    if rc != 0:
        die("One or more uv add commands failed. See output above.", rc)

    # Run uv lock to resolve dependencies after all pyproject.toml files updated
    print("Running: uv lock")
    try:
        res = run("uv", "lock", capture=True, check=False)
        if res.returncode != 0:
            print("uv lock failed. pyproject.toml files have been updated but lock failed.")
            if res.stderr:
                sys.stderr.write(res.stderr)
            return 1
    except subprocess.CalledProcessError as e:
        print(f"uv lock failed: {e}")
        return 1

    # Optionally sync
    if args.sync:
        print("Running: uv sync")
        try:
            res = run("uv", "sync", capture=True, check=False)
            if res.returncode != 0:
                print("uv sync failed. Lock succeeded but environment not synced.")
                if res.stderr:
                    sys.stderr.write(res.stderr)
                return 1
        except subprocess.CalledProcessError as e:
            print(f"uv sync failed: {e}")
            return 1

    print("\nDone. pyproject.toml updated{}."
          .format(" and environment synced" if args.sync else " (run `uv sync` to update environment)"))
    return 0

if __name__ == "__main__":
    main()
