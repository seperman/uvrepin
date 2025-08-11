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
import argparse, pathlib, re, shlex, subprocess, sys

try:
    import tomllib  # Python 3.11+
except Exception:
    sys.stderr.write("Needs Python 3.11+ (tomllib).\n"); sys.exit(1)

PYPROJECT = pathlib.Path("pyproject.toml")

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

def gather_direct(data: dict) -> dict[str|None, list[dict]]:
    out = {}
    proj = data.get("project", {})
    deps = proj.get("dependencies", []) or []
    if deps:
        out[None] = []
        for r in deps:
            p = parse_req(r)
            if p and p[0] != "SKIP":
                name, extras, pinned, marker = p
                out[None].append(dict(raw=r, name=name, extras=extras, pinned=pinned, marker=marker))
    dep_groups = data.get("dependency-groups", {}) or {}
    for gname, arr in dep_groups.items():
        if not isinstance(arr, list): continue
        group = []
        for r in arr:
            p = parse_req(r)
            if p and p[0] != "SKIP":
                name, extras, pinned, marker = p
                group.append(dict(raw=r, name=name, extras=extras, pinned=pinned, marker=marker))
        if group: out[gname] = group
    return out

def run(*args: str, capture=False, check=True):
    return subprocess.run(args, text=True, capture_output=capture, check=check)

def ensure_uv():
    try: run("uv", "--version", check=True)
    except Exception: die("uv not found on PATH.")

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

def build_uv_add_base(group: str|None, sync: bool, allow_pre: bool, indexes: list[str]) -> list[str]:
    args = ["uv", "add"]
    if not sync: args.append("--no-sync")
    if group is not None: args += ["--group", group]
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
    args = ap.parse_args()

    ensure_uv()
    data = read_pyproject()
    groups = gather_direct(data)
    if not groups:
        print("No direct dependencies found."); return 0

    if args.only_groups:
        wanted = {g.strip() for g in args.only_groups.split(",") if g.strip()}
        groups = {g: lst for g, lst in groups.items()
                  if (("main" in wanted and g is None) or (g is not None and g in wanted))}
        if not groups:
            print("No matching groups after --only-groups."); return 0

    # Ask uv what's outdated to learn the "latest" versions.
    try:
        proc = run("uv", "pip", "list", "--outdated", capture=True)
        latest_map = parse_outdated_table(proc.stdout)
    except subprocess.CalledProcessError as e:
        die(f"Failed to run 'uv pip list --outdated':\n{e.stderr or e}", 2)

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
            print("Dry run: nothing to update (no outdated direct deps detected).\n"
                  "Tip: if some groups aren't installed (e.g. dev), run `uv sync --group <name>` and retry.")
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
        print("All direct dependencies appear up-to-date (based on installed groups). Nothing to do.")
        return 0

    # Execute per-group with explicit ==version pins (works on uv 0.7.8).
    rc = 0
    for gname in list(groups.keys()):
        to_update = [(d, latest) for (g, d, latest) in plan if g == gname]
        if not to_update: continue
        base = build_uv_add_base(gname, sync=args.sync, allow_pre=args.pre, indexes=args.index)
        reqs = []
        for d, latest in to_update:
            spec = d["name"] + d["extras"] + f"=={latest}"
            if d["marker"]: spec += f"; {d['marker']}"
            reqs.append(spec)
        cmd = base + reqs
        print("Running:", " ".join(shlex.quote(x) for x in cmd))
        res = subprocess.run(cmd)
        rc = rc or res.returncode

    if rc == 0:
        print("\nDone. pyproject.toml updated{}."
              .format(" and environment synced" if args.sync else " (environment unchanged)"))
    else:
        die("One or more uv commands failed. See output above.", rc)

if __name__ == "__main__":
    main()
