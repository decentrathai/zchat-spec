#!/usr/bin/env python3
"""parity.py: the registry tool of the ZChat cross-platform parity rule (W-236; plan D.2, D.3, D.12, annex A6).

Step P0, slice A: init, release add, area set|exempt, add, baseline, except, validate [--approvals], render, show.

How it holds together:
- parity/PARITY.log is the only source. Every normative file (PARITY.json, areas.json, releases.json, exceptions.json),
  every release snapshot under parity/releases/ and the view PARITY.md are a pure function of the log. `validate`
  replays the log from nothing, re-checks every rule each op was written under, and compares every file byte for byte.
- Every write op takes the lock, refuses unless the spec repo is on `trunk` with clean registry state that replays,
  applies one op, writes the files, appends one log line and makes one commit, all under the lock.
- Paths outside the spec repo are injectable: ZCHAT_PARITY_LOCK, ZCHAT_PARITY_CACHE, ZCHAT_WAIVERS (':'-separated).
- Standard library only. Every git call runs with GIT_NO_REPLACE_OBJECTS=1.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
PLATFORMS = ("android", "ios", "backend")
KINDS = ("behaviour", "wire", "crossing", "money_rule", "endpoint", "text", "pin", "fix", "refactor")
CASE_KINDS = ("wire", "crossing", "endpoint", "pin", "text")
MONEY_SHAPES = ("number", "table", "other")
TIERS = ("local", "mixed", "hard")  # order matters: a lower index is a lower tier
RELEASE_CLASSES = ("internal", "outward")
TRUNK = "trunk"
HOME = Path(os.environ.get("HOME", "/home/yourt"))
DEFAULT_SPEC = HOME / "zchat-spec"
DEFAULT_LOCK = HOME / ".config/zchat-qa/locks/parity.lock"
DEFAULT_WAIVERS = [HOME / "zchat-evidence/pm/WAIVERS.md", HOME / "zchat-evidence/pm/WAIVERS_IOS.md"]
NORMATIVE = ("PARITY.json", "areas.json", "releases.json", "exceptions.json")
VIEW = "PARITY.md"
LOG = "PARITY.log"
ID_RE = re.compile(r"^PAR-\d{4,}$")
WAIVER_RE = re.compile(r"^(W|WI)-\d+$")
REGISTER_RE = re.compile(r"^(ZC-\d+|IOSP-\d+)$")
MIN_REASON = 15


class ParityError(Exception):
    """A refusal. The message says why and, where there is one, the way forward."""


# ---------------------------------------------------------------- small pure helpers

def vkey(v):
    """Version compare key (X4 test 35): dotted integers only, so 1.10.0 sorts after 1.9.0."""
    if not isinstance(v, str) or not re.fullmatch(r"\d+(\.\d+)*", v):
        raise ParityError("not a version: %r (dotted integers only)" % (v,))
    return tuple(int(x) for x in v.split("."))


def canon(obj):
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def glob_rx(pattern):
    """A path pattern relative to the repo root: `**` any depth (also none), `*` and `?` within one segment."""
    if not pattern or pattern.startswith("/") or "\\" in pattern or "//" in pattern:
        raise ParityError("bad pattern %r" % pattern)
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def gmatch(pattern, path):
    return bool(glob_rx(pattern).match(path))


# Paths that can never be exempt (annex A7): a non-test `src/<name>/` path and build files. A pattern is refused when
# it matches any probe, or names a literal `src/<x>/` segment that is not a test source set.
NEVER_EXEMPT_PROBES = [
    "src/main/A.kt", "app/src/main/A.kt", "app/src/main/res/values/strings.xml", "app/src/release/A.kt",
    "ui-lib/src/main/java/x/money/A.kt", "lib/src/debug/A.kt", "src/routes/a.ts", "apps/backend/src/routes/a.ts",
    "apps/backend/src/index.ts", "build.gradle.kts", "app/build.gradle.kts", "settings.gradle.kts",
    "gradle.properties", "gradle/libs.versions.toml", "proguard-rules.pro", "app/proguard-rules.pro",
    "buildSrc/src/main/kotlin/A.kt", "buildSrc/build.gradle.kts", "build-conventions-secant/src/main/A.kt",
    "Package.swift", "Packages/ZChatCore/Package.swift", "Package.resolved", "App.xcworkspace/Package.resolved",
    "Packages/ZChatCore/Sources/ZChatCore/A.swift", "Packages/ZChatUI/Sources/ZChatUI/A.swift",
    "App/Sources/A.swift", "Extensions/Notify/A.swift",
    "project.yml", "Config/Base.xcconfig", "App/App.entitlements", "App/Info.plist", ".xcode-version",
    ".github/workflows/release.yml", ".github/workflows/upload.yml", ".github/workflows/release-testflight.yml",
    "prisma/schema.prisma", "apps/backend/prisma/schema.prisma", "scripts/deploy.sh",
    "apps/backend/scripts/migrate.ts", "package.json", "apps/backend/package.json", "package-lock.json",
    "apps/backend/package-lock.json", "pnpm-lock.yaml", "yarn.lock",
]
TEST_SOURCE_SET = re.compile(r"^(test|androidTest|[A-Za-z0-9]*Test|testFixtures)$")


def never_exempt_reason(pattern):
    for probe in NEVER_EXEMPT_PROBES:
        if gmatch(pattern, probe):
            return "it would exempt %s (a product source or build file can never be exempt)" % probe
    segs = pattern.split("/")
    for a, b in zip(segs, segs[1:]):
        if a == "src" and not TEST_SOURCE_SET.match(b):
            return "it names src/%s/, which is not a test source set" % b
    return None


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def waiver_files():
    env = os.environ.get("ZCHAT_WAIVERS")
    return [Path(p) for p in env.split(":") if p] if env else list(DEFAULT_WAIVERS)


def waiver_section(wid):
    """The verbatim section `## <wid> ...` up to the next `## `, from the first waivers file that has it."""
    head = re.compile(r"^## " + re.escape(wid) + r"(\s|$)")
    for f in waiver_files():
        try:
            lines = f.read_text(encoding="utf-8").splitlines(True)
        except OSError:
            continue
        for i, line in enumerate(lines):
            if head.match(line):
                j = i + 1
                while j < len(lines) and not lines[j].startswith("## "):
                    j += 1
                return "".join(lines[i:j]).rstrip("\n") + "\n"
    return None


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the state and the pure ops

def empty_state():
    return {"items": {}, "areas": {p: {"areas": [], "exempt": []} for p in PLATFORMS},
            "releases": {p: [] for p in PLATFORMS}, "exceptions": [], "snapshots": {}}


def releases_of(state, plat):
    return [r["version"] for r in state["releases"][plat]]


def shipped_versions(state, plat):
    return sorted((s["version"] for s in state["snapshots"].values() if s["platform"] == plat), key=vkey)


def latest_shipped(state, plat):
    v = shipped_versions(state, plat)
    return v[-1] if v else None


def next_releases(state, plat):
    last = latest_shipped(state, plat)
    return [v for v in releases_of(state, plat) if last is None or vkey(v) > vkey(last)]


def known_areas(state):
    return {a["area"] for p in PLATFORMS for a in state["areas"][p]["areas"]}


def next_id(state):
    n = max([int(i.split("-")[1]) for i in state["items"]] or [0])
    return "PAR-%04d" % (n + 1)


def need(cond, msg):
    if not cond:
        raise ParityError(msg)


def op_init(state, a):
    need(a == {"schema": SCHEMA}, "init takes schema %d" % SCHEMA)
    return state


def op_release_add(state, a):
    plat, ver, cls = a.get("platform"), a.get("version"), a.get("class")
    need(plat in PLATFORMS, "unknown platform %r" % plat)
    vkey(ver)
    need(cls in RELEASE_CLASSES, "class is internal or outward")
    need(ver not in releases_of(state, plat), "%s %s is already listed" % (plat, ver))
    last = latest_shipped(state, plat)
    need(last is None or vkey(ver) > vkey(last), "%s %s is not after the shipped %s" % (plat, ver, last))
    rel = state["releases"][plat] + [{"version": ver, "class": cls}]
    state["releases"][plat] = sorted(rel, key=lambda r: vkey(r["version"]))
    return state


def _area_entry(state, plat, pattern):
    for e in state["areas"][plat]["areas"]:
        if e["pattern"] == pattern:
            return e
    return None


def op_area_set(state, a):
    plat, pat, area, tier = a.get("platform"), a.get("pattern"), a.get("area"), a.get("tier")
    need(plat in PLATFORMS, "unknown platform %r" % plat)
    glob_rx(pat)
    need(isinstance(area, str) and re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*", area or ""),
         "area names are lower-case dotted words")
    need(tier in TIERS, "tier is hard, mixed or local")
    need(all(e["pattern"] != pat for e in state["areas"][plat]["exempt"]),
         "%s is on the exempt list; moving it into an area is `area set` after `area unexempt` (step P1)" % pat)
    old = _area_entry(state, plat, pat)
    if old:
        need(TIERS.index(tier) >= TIERS.index(old["tier"]),
             "a move from %s to %s is a tier downgrade: it needs an upheld area-review verdict (step P1)"
             % (old["tier"], tier))
        old.update(area=area, tier=tier)
    else:
        state["areas"][plat]["areas"].append({"pattern": pat, "area": area, "tier": tier})
        state["areas"][plat]["areas"].sort(key=lambda e: e["pattern"])
    return state


def op_area_exempt(state, a):
    plat, pat, reason = a.get("platform"), a.get("pattern"), a.get("reason")
    need(plat in PLATFORMS, "unknown platform %r" % plat)
    glob_rx(pat)
    need(isinstance(reason, str) and len(reason.strip()) >= MIN_REASON,
         "an exemption needs a reason of %d characters or more" % MIN_REASON)
    why = never_exempt_reason(pat)
    need(why is None, "%s cannot be exempt: %s" % (pat, why))
    need(_area_entry(state, plat, pat) is None,
         "%s is classified; moving it onto the exempt list needs an upheld area-review verdict (step P1)" % pat)
    need(all(e["pattern"] != pat for e in state["areas"][plat]["exempt"]), "%s is already exempt" % pat)
    state["areas"][plat]["exempt"].append({"pattern": pat, "reason": reason})
    state["areas"][plat]["exempt"].sort(key=lambda e: e["pattern"])
    return state


def op_baseline(state, a):
    plat, ver = a.get("platform"), a.get("version")
    need(plat in PLATFORMS, "unknown platform %r" % plat)
    need(ver in releases_of(state, plat), "%s %s is not in releases.json" % (plat, ver))
    need(not shipped_versions(state, plat), "%s already has a release snapshot" % plat)
    rel = [r for r in state["releases"][plat] if r["version"] == ver][0]
    need(rel["class"] == "outward", "a baseline is an outward release")
    state["snapshots"]["%s-%s" % (plat, ver)] = {"platform": plat, "version": ver, "baseline": True, "proven": []}
    return state


def op_add(state, a):
    iid = a.get("id")
    need(iid == next_id(state), "the next id is %s, not %s" % (next_id(state), iid))
    kind, area, title = a.get("kind"), a.get("area"), a.get("title")
    need(kind in KINDS, "kind is one of %s" % ", ".join(KINDS))
    need(area in known_areas(state), "area %r is not in areas.json (add it with `area set` first)" % area)
    need(isinstance(title, str) and len(title.strip()) >= 8, "a title of 8 characters or more")
    src = a.get("source")
    need(isinstance(src, list) and src and all(isinstance(s, str) and s for s in src),
         "name at least one source (a W, WI or ZC id, or a file)")
    need(a.get("supersedes") is None, "superseding items are made by `parity.py change` (step P1)")
    sides = a.get("sides")
    need(isinstance(sides, dict) and set(sides) == set(PLATFORMS),
         "name every platform: --due/--register for each that has it, --null for each where it cannot exist")
    base = a.get("baseline_shipped") or []
    need(isinstance(base, list), "baseline_shipped is a list")
    shape = a.get("money_shape")
    if kind == "money_rule":
        need(shape in MONEY_SHAPES, "a money_rule says --money-shape number, table or other")
    else:
        need(shape is None, "--money-shape is for money_rule items")
    if kind == "fix":
        need(REGISTER_RE.match(a.get("fix_register") or ""), "a fix item names --fix-register ZC-####")
        need(a.get("found_on") in PLATFORMS and sides.get(a.get("found_on")) is not None,
             "a fix item names --found-on, a platform the item is on")
    else:
        need(a.get("fix_register") is None and a.get("found_on") is None, "--fix-register and --found-on are for fix")
    needs_cases = kind in CASE_KINDS or (kind == "money_rule" and shape in ("number", "table"))
    on_some = False
    for plat in PLATFORMS:
        s = sides[plat]
        if s is None:
            need(plat not in base, "%s is null and baseline-shipped at once" % plat)
            continue
        on_some = True
        need(isinstance(s, dict) and set(s) == {"due", "register", "tests", "cases"}, "bad side for %s" % plat)
        due = s["due"]
        need(due in releases_of(state, plat), "%s due %r is not a release in releases.json" % (plat, due))
        need(all(isinstance(t, str) and t for t in s["tests"]) and all(isinstance(c, str) and c for c in s["cases"]),
             "tests and cases are names")
        if needs_cases:
            need(s["cases"], "a %s item names vector cases for %s, not only tests" % (kind, plat))
        if plat in base:
            snap = state["snapshots"].get("%s-%s" % (plat, due))
            need(snap is not None and snap.get("baseline") and shipped_versions(state, plat) == [due],
                 "baseline-shipped needs %s's baseline snapshot at %s and no later snapshot" % (plat, due))
            need(kind != "fix", "a fix is never shipped at the baseline")
            continue
        need(REGISTER_RE.match(s["register"] or ""), "the open %s side needs --register (ZC-#### or IOSP-#)" % plat)
        nxt = next_releases(state, plat)
        need(due in nxt, "%s due %s is not after the shipped %s" % (plat, due, latest_shipped(state, plat)))
        limited = kind == "fix" or latest_shipped(state, plat) is not None
        if limited:
            need(due in nxt[:2], "%s due %s is further than %s's next two releases (%s)"
                 % (plat, due, plat, ", ".join(nxt[:2])))
    need(on_some, "an item on no platform")
    item = {"id": iid, "kind": kind, "area": area, "title": title, "source": src, "supersedes": None,
            "platforms": sides}
    if shape:
        item["money_shape"] = shape
    if kind == "fix":
        item["register"], item["found_on"] = a["fix_register"], a["found_on"]
    state["items"][iid] = item
    for plat in base:
        snap = state["snapshots"]["%s-%s" % (plat, sides[plat]["due"])]
        snap["proven"] = sorted(set(snap["proven"]) | {iid})
    return state


def op_except(state, a):
    iid, plat, wid, last, h = a.get("item"), a.get("platform"), a.get("waiver"), a.get("last_release"), \
        a.get("waiver_sha256")
    need(iid in state["items"], "unknown item %s" % iid)
    need(plat in PLATFORMS, "unknown platform %r" % plat)
    need(state["items"][iid]["platforms"][plat] is not None, "%s is null on %s: nothing to except" % (iid, plat))
    need(WAIVER_RE.match(wid or ""), "a waiver id is W-# or WI-#")
    need(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h), "the waiver text hash is missing")
    need(last in releases_of(state, plat), "%s %r is not a release in releases.json" % (plat, last))
    need(all(not (e["item"] == iid and e["platform"] == plat) for e in state["exceptions"]),
         "%s already has an exception on %s" % (iid, plat))
    snap = [s for s in state["snapshots"].values() if s["platform"] == plat and iid in s["proven"]]
    need(not snap, "%s shipped on %s: a shipped item is changed through `parity.py change` (step P1)" % (iid, plat))
    state["exceptions"].append({"item": iid, "platform": plat, "waiver": wid, "waiver_sha256": h,
                                "last_release": last})
    state["exceptions"].sort(key=lambda e: (e["item"], e["platform"]))
    return state


OPS = {"init": op_init, "release-add": op_release_add, "area-set": op_area_set, "area-exempt": op_area_exempt,
       "baseline": op_baseline, "add": op_add, "except": op_except}


def apply(state, entry):
    op = entry.get("op")
    need(op in OPS, "unknown op %r" % op)
    args = entry.get("args")
    need(isinstance(args, dict), "op args are an object")
    return OPS[op](state, json.loads(json.dumps(args)))


# ---------------------------------------------------------------- rendering

def render(state):
    files = {
        "PARITY.json": canon({"schema": SCHEMA, "items": [state["items"][k] for k in sorted(state["items"])]}),
        "areas.json": canon({"schema": SCHEMA, "platforms": state["areas"]}),
        "releases.json": canon({"schema": SCHEMA, "platforms": state["releases"]}),
        "exceptions.json": canon({"schema": SCHEMA, "exceptions": state["exceptions"]}),
    }
    for name, snap in sorted(state["snapshots"].items()):
        files["releases/%s.json" % name] = canon(dict(snap, schema=SCHEMA))
    files[VIEW] = render_md(state)
    return files


def side_text(state, iid, plat, side):
    if side is None:
        return "null"
    if any(iid in s["proven"] for s in state["snapshots"].values() if s["platform"] == plat):
        return "shipped"
    ex = [e for e in state["exceptions"] if e["item"] == iid and e["platform"] == plat]
    if ex:
        return "excepted (%s, to %s)" % (ex[0]["waiver"], ex[0]["last_release"])
    return "due %s, %s" % (side["due"], side["register"])


def render_md(state):
    out = ["# Parity registry (generated by tools/parity.py; do not edit)", "",
           "Per-platform state here is what the registry alone says. Proven at a commit is computed by "
           "`parity.py status` from test results.", "",
           "| Id | Kind | Area | Title | Android | iOS | Backend |", "|---|---|---|---|---|---|---|"]
    for iid in sorted(state["items"]):
        it = state["items"][iid]
        cells = [side_text(state, iid, p, it["platforms"][p]) for p in PLATFORMS]
        out.append("| %s | %s | %s | %s | %s |" % (iid, it["kind"], it["area"], it["title"].replace("|", "/"),
                                                   " | ".join(cells)))
    out += ["", "Releases: " + "; ".join("%s %s" % (p, ", ".join(releases_of(state, p)) or "none")
                                         for p in PLATFORMS), ""]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the repo

def git(spec, *args, check=True):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    r = subprocess.run(["git", "-C", str(spec), *args], env=env, capture_output=True, text=True)
    if check and r.returncode:
        raise ParityError("git %s: %s" % (" ".join(args[:2]), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout


def read_log(spec):
    path = spec / "parity" / LOG
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            e = json.loads(line)
        except ValueError:
            raise ParityError("%s line %d is not JSON" % (LOG, n))
        need(isinstance(e, dict) and e.get("seq") == n, "%s line %d: seq is not %d" % (LOG, n, n))
        out.append(e)
    return out


def replay(entries):
    need(entries, "the log is empty: run `parity.py init`")
    need(entries[0].get("op") == "init", "the log does not start with init")
    state = empty_state()
    for e in entries:
        try:
            state = apply(state, e)
        except ParityError as err:
            raise ParityError("log line %s (%s) breaks a rule: %s" % (e.get("seq"), e.get("op"), err))
    return state


def compare(spec, state):
    """Every rendered file equals the file on disk, and parity/releases/ holds nothing else."""
    want = render(state)
    bad = []
    for rel, text in want.items():
        f = spec / "parity" / rel
        try:
            if f.read_bytes() != text.encode("utf-8"):
                bad.append(rel)
        except OSError:
            bad.append(rel)
    rdir = spec / "parity" / "releases"
    if rdir.is_dir():
        for f in sorted(rdir.rglob("*")):
            rel = f.relative_to(spec / "parity").as_posix()
            if f.is_file() and rel not in want:
                bad.append(rel)
    return bad


def load(spec):
    need(spec.is_dir(), "no spec clone at %s" % spec)
    state = replay(read_log(spec))
    bad = compare(spec, state)
    need(not bad, "these files do not equal the log replay: %s" % ", ".join(bad))
    return state


def approvals_missing(state):
    out = []
    for e in state["exceptions"]:
        text = waiver_section(e["waiver"])
        if text is None:
            out.append("%s (exception %s %s): not in any waivers file" % (e["waiver"], e["item"], e["platform"]))
        elif sha256(text) != e["waiver_sha256"]:
            out.append("%s (exception %s %s): the waiver text changed since it was recorded"
                       % (e["waiver"], e["item"], e["platform"]))
    return out


class Lock:
    def __init__(self):
        self.path = Path(os.environ.get("ZCHAT_PARITY_LOCK") or DEFAULT_LOCK)

    def __enter__(self):
        need(self.path.parent.is_dir(), "the lock directory %s does not exist" % self.path.parent)
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def write_op(spec, op, args, summary):
    """One op under the lock: check, apply, write, log, commit. Rolls the files back if the commit fails."""
    with Lock():
        need(spec.is_dir(), "no spec clone at %s" % spec)
        branch = git(spec, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
        need(branch == TRUNK, "registry ops run on %s only (this is %r)" % (TRUNK, branch or "detached"))
        dirty = git(spec, "status", "--porcelain", "--untracked-files=all", "--", "parity").strip()
        need(not dirty, "uncommitted registry state under parity/: commit or remove it first")
        entries = read_log(spec)
        if op == "init":
            need(not entries and not (spec / "parity").exists(), "the registry already exists")
            state = empty_state()
        else:
            state = load(spec)
        if op == "add":
            args["id"] = next_id(state)
        entry = {"seq": len(entries) + 1, "op": op, "args": args, "at": now_iso(),
                 "by": os.environ.get("ZCHAT_PARITY_BY", "ios-session")}
        state = apply(state, entry)
        pdir = spec / "parity"
        try:
            for rel, text in render(state).items():
                f = pdir / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8")
            with open(pdir / LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
            git(spec, "add", "-A", "--", "parity")
            msg = "parity: %s %s\n\nMade by tools/parity.py under the registry lock (log seq %d).\n" % (
                op, summary(args), entry["seq"])
            git(spec, "commit", "-q", "-m", msg)
        except BaseException:
            git(spec, "reset", "-q", "--", "parity", check=False)
            git(spec, "checkout", "-q", "--", "parity", check=False)
            git(spec, "clean", "-q", "-fd", "--", "parity", check=False)
            raise
        return args


# ---------------------------------------------------------------- the command line

def kv(values, what):
    out = {}
    for v in values or []:
        need("=" in v, "%s takes platform=value, not %r" % (what, v))
        p, val = v.split("=", 1)
        need(p in PLATFORMS, "unknown platform %r in %s" % (p, what))
        out.setdefault(p, []).append(val)
    return out


def build_sides(ns):
    due, reg, tests, cases = kv(ns.due, "--due"), kv(ns.register, "--register"), kv(ns.test, "--test"), \
        kv(ns.case, "--case")
    nulls = set(ns.null or [])
    sides = {}
    for p in PLATFORMS:
        if p in nulls:
            need(p not in due and p not in reg and p not in tests and p not in cases, "%s is --null and has values" % p)
            sides[p] = None
            continue
        need(p in due, "%s: give --due %s=<release> or --null %s" % (p, p, p))
        need(len(due[p]) == 1 and len(reg.get(p, [None])) == 1, "one --due and one --register per platform")
        sides[p] = {"due": due[p][0], "register": reg.get(p, [None])[0], "tests": tests.get(p, []),
                    "cases": cases.get(p, [])}
    return sides


def main(argv=None):
    ap = argparse.ArgumentParser(prog="parity.py", description=__doc__.splitlines()[0])
    ap.add_argument("--spec", default=os.environ.get("ZCHAT_SPEC") or str(DEFAULT_SPEC))
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init")
    rel = sp.add_parser("release").add_subparsers(dest="sub", required=True).add_parser("add")
    rel.add_argument("--platform", required=True)
    rel.add_argument("--version", required=True)
    rel.add_argument("--class", dest="cls", required=True)
    area = sp.add_parser("area").add_subparsers(dest="sub", required=True)
    aset = area.add_parser("set")
    aex = area.add_parser("exempt")
    for p in (aset, aex):
        p.add_argument("--platform", required=True)
        p.add_argument("--pattern", required=True)
    aset.add_argument("--area", required=True)
    aset.add_argument("--tier", required=True)
    aex.add_argument("--reason", required=True)
    b = sp.add_parser("baseline")
    b.add_argument("--platform", required=True)
    b.add_argument("--version", required=True)
    add = sp.add_parser("add")
    add.add_argument("--kind", required=True)
    add.add_argument("--area", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--source", action="append", required=True)
    for f in ("--due", "--register", "--test", "--case", "--null", "--baseline-shipped"):
        add.add_argument(f, action="append")
    add.add_argument("--money-shape")
    add.add_argument("--fix-register")
    add.add_argument("--found-on")
    ex = sp.add_parser("except")
    ex.add_argument("--item", required=True)
    ex.add_argument("--platform", required=True)
    ex.add_argument("--waiver", required=True)
    ex.add_argument("--last-release", required=True)
    v = sp.add_parser("validate")
    v.add_argument("--approvals", action="store_true", help="also check every exception against the waivers files")
    sp.add_parser("render")
    sh = sp.add_parser("show")
    sh.add_argument("item")
    ns = ap.parse_args(argv)
    spec = Path(ns.spec)

    try:
        if ns.cmd == "validate":
            try:
                state = load(spec)
            except ParityError as e:
                print("VALIDATE FAIL: %s" % e)
                return 1
            print("VALIDATE OK: %d log lines, %d items" % (len(read_log(spec)), len(state["items"])))
            if ns.approvals:
                miss = approvals_missing(state)
                if miss:
                    print("APPROVALS FAIL:\n  " + "\n  ".join(miss))
                    return 1
                print("APPROVALS OK: %d exceptions" % len(state["exceptions"]))
            return 0
        if ns.cmd == "render":
            state = load(spec)
            for rel, text in render(state).items():
                print("%s %s" % (hashlib.sha256(text.encode()).hexdigest()[:12], rel))
            return 0
        if ns.cmd == "show":
            state = load(spec)
            need(ns.item in state["items"], "unknown item %s" % ns.item)
            print(canon(state["items"][ns.item]), end="")
            return 0
        if ns.cmd == "init":
            write_op(spec, "init", {"schema": SCHEMA}, lambda a: "schema %d" % SCHEMA)
        elif ns.cmd == "release":
            write_op(spec, "release-add", {"platform": ns.platform, "version": ns.version, "class": ns.cls},
                     lambda a: "%s %s (%s)" % (a["platform"], a["version"], a["class"]))
        elif ns.cmd == "area" and ns.sub == "set":
            write_op(spec, "area-set", {"platform": ns.platform, "pattern": ns.pattern, "area": ns.area,
                                        "tier": ns.tier},
                     lambda a: "%s %s -> %s (%s)" % (a["platform"], a["pattern"], a["area"], a["tier"]))
        elif ns.cmd == "area":
            write_op(spec, "area-exempt", {"platform": ns.platform, "pattern": ns.pattern, "reason": ns.reason},
                     lambda a: "%s exempt %s" % (a["platform"], a["pattern"]))
        elif ns.cmd == "baseline":
            write_op(spec, "baseline", {"platform": ns.platform, "version": ns.version},
                     lambda a: "%s %s" % (a["platform"], a["version"]))
        elif ns.cmd == "add":
            args = {"kind": ns.kind, "area": ns.area, "title": ns.title, "source": ns.source, "supersedes": None,
                    "sides": build_sides(ns), "baseline_shipped": sorted(set(ns.baseline_shipped or [])),
                    "money_shape": ns.money_shape, "fix_register": ns.fix_register, "found_on": ns.found_on}
            done = write_op(spec, "add", args, lambda a: "%s %s: %s" % (a["id"], a["kind"], a["title"]))
            print("ADDED %s" % done["id"])
            return 0
        elif ns.cmd == "except":
            text = waiver_section(ns.waiver)
            need(text is not None, "%s is not in any waivers file (%s)"
                 % (ns.waiver, ", ".join(str(f) for f in waiver_files())))
            write_op(spec, "except", {"item": ns.item, "platform": ns.platform, "waiver": ns.waiver,
                                      "last_release": ns.last_release, "waiver_sha256": sha256(text)},
                     lambda a: "%s %s under %s to %s" % (a["item"], a["platform"], a["waiver"], a["last_release"]))
        print("OK")
        return 0
    except ParityError as e:
        print("REFUSED: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
