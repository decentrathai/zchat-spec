#!/usr/bin/env python3
"""parity.py: the registry tool of the ZChat cross-platform parity rule (W-236; plan D.2, D.3, D.12, annex A6).

Step P0, slice A: init, release add, area set|exempt, add, baseline, except, validate [--approvals], render, show.
Step P0, slice B: check-commit (the commit-msg hook's judge), coverage, and classify(), the default-deny
classification. Both commands read the registry at the tree's parity/SPEC_PIN from git objects, never from the clone's
working tree, and fail closed.

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
DEFAULT_WAIVERS = [HOME / "zchat-evidence/pm/WAIVERS.md", HOME / "zchat-evidence/pm/WAIVERS_IOS.md"]


def default_lock():
    return HOME / ".config/zchat-qa/locks/parity.lock"
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
TEST_SOURCE_SET = re.compile(r"^(test|androidTest|testFixtures|[A-Za-z0-9]*Test|\*Test)$")


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
    return parse_log(path.read_text(encoding="utf-8"))


def parse_log(text):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
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
    def read(rel):
        try:
            return (spec / "parity" / rel).read_bytes()
        except OSError:
            return None
    rdir = spec / "parity" / "releases"
    have = [f.relative_to(spec / "parity").as_posix() for f in sorted(rdir.rglob("*")) if f.is_file()] \
        if rdir.is_dir() else []
    return compare_with(state, read, have)


def compare_with(state, read, release_files):
    """`read(rel)` gives the bytes of parity/<rel> or None; `release_files` lists what parity/releases/ holds."""
    want = render(state)
    bad = [rel for rel, text in want.items() if read(rel) != text.encode("utf-8")]
    return bad + [rel for rel in release_files if rel not in want]


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
    """The registry lock. An injected path (ZCHAT_PARITY_LOCK, tests) may be created; the default lives in another
    session's protected tree and is only opened read-only if it already exists, never created."""

    def __init__(self):
        self.injected = bool(os.environ.get("ZCHAT_PARITY_LOCK"))
        self.path = Path(os.environ.get("ZCHAT_PARITY_LOCK") or default_lock())

    def __enter__(self):
        need(self.path.parent.is_dir(), "the lock directory %s does not exist" % self.path.parent)
        if self.injected:
            self.fh = open(self.path, "a")
        else:
            need(self.path.is_file(), "the registry lock %s does not exist; the tool never creates it there "
                 "(it is the Android session's tree: ask over the mailbox)" % self.path)
            self.fh = open(self.path, "r")
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


# ---------------------------------------------------------------- slice B: classification, check-commit, coverage

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PIN_FILE = "parity/SPEC_PIN"
PIN_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VENDORED = ("parity/testdata/", "parity/runner/")
# The tool's own fixed test rule (annex A7, plus the iOS test roots). It only names an exempt match "test"; a path
# that matches it and no exempt pattern stays unclassified.
TEST_PATTERNS = ("**/src/test/**", "**/src/androidTest/**", "**/src/*Test/**", "Tests/**", "tools/qa/tests/**",
                 "Packages/*/Tests/**", "App/Tests/**")
SKIP_MARKERS = ("XCTSkip", ".disabled(", "withKnownIssue")
ITEMS_RE = re.compile(r"^PAR-\d{4,}(?:\s*,\s*PAR-\d{4,})*$")
SYNC_RE = re.compile(r"^sync\s+[0-9a-f]{7,64}\.\.[0-9a-f]{7,64}$")
# What a git hook exports for the repo being committed; a call into the spec clone must not inherit it.
REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_COMMON_DIR", "GIT_PREFIX")


def git_raw(where, *args, spec_side=False):
    """(exit code, stdout bytes). Bytes, so a blob compares exactly (text mode would fold \\r\\n)."""
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    if spec_side:
        for k in REPO_VARS:
            env.pop(k, None)
    r = subprocess.run(["git", "-C", str(where), *args], env=env, capture_output=True)
    return r.returncode, r.stdout


def blob(where, spec_path, spec_side=False):
    """The bytes of `<rev>:<path>` (or `:<path>`, the index), None when there is no such blob."""
    rc, out = git_raw(where, "cat-file", "blob", spec_path, spec_side=spec_side)
    return out if rc == 0 else None


def zsplit(out):
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def is_test_path(path):
    return any(gmatch(p, path) for p in TEST_PATTERNS)


def classify(state, platform, path):
    """(tier, area) for an area match (the highest tier when several match), ("test"|"exempt", pattern) for an
    exempt match, (None, None) when nothing of `platform`'s lists matches (default deny)."""
    lists = state["areas"].get(platform) or {"areas": [], "exempt": []}
    hits = [e for e in lists["areas"] if gmatch(e["pattern"], path)]
    if hits:
        best = max(hits, key=lambda e: (TIERS.index(e["tier"]), e["pattern"]))
        return best["tier"], best["area"]
    for e in lists["exempt"]:
        if gmatch(e["pattern"], path):
            return ("test" if is_test_path(path) else "exempt"), e["pattern"]
    return None, None


def is_shipped(state, iid, plat):
    return any(iid in s["proven"] for s in state["snapshots"].values() if s["platform"] == plat)


def exception_of(state, iid, plat):
    ex = [e for e in state["exceptions"] if e["item"] == iid and e["platform"] == plat]
    return ex[0] if ex else None


def read_pin(raw, where):
    need(raw is not None, "%s has no %s: every checked commit pins the spec commit it is judged by (run "
         "`parity.py sync`, or restore the file)" % (where, PIN_FILE))
    return raw.decode("utf-8", "replace").strip()


def load_at(spec, pin):
    """The registry at spec commit `pin`, from git objects only: replayed and compared like `validate`."""
    need(spec.is_dir(), "no spec clone at %s (clone the spec; the check fails closed)" % spec)
    need(PIN_RE.match(pin), "%s holds %r, not a full spec commit id" % (PIN_FILE, pin))
    rc, out = git_raw(spec, "cat-file", "-t", pin, spec_side=True)
    need(rc == 0 and out.strip() == b"commit",
         "the spec clone %s does not hold the pinned commit %s (fetch the spec, or fix %s)" % (spec, pin, PIN_FILE))

    def read(rel):
        return blob(spec, "%s:parity/%s" % (pin, rel), spec_side=True)
    log = read(LOG)
    need(log is not None, "the spec at pin %s has no parity/%s" % (pin[:12], LOG))
    state = replay(parse_log(log.decode("utf-8")))
    rc, out = git_raw(spec, "ls-tree", "-r", "--name-only", "-z", pin, "--", "parity/releases/", spec_side=True)
    need(rc == 0, "cannot list parity/releases/ at pin %s" % pin[:12])
    bad = compare_with(state, read, [p[len("parity/"):] for p in zsplit(out)])
    need(not bad, "the registry at pin %s does not equal its log replay: %s" % (pin[:12], ", ".join(bad)))
    return state


def staged_changes(repo):
    """(base, [(status, old, new)]): the index against HEAD (the empty tree before the first commit). `old` is None
    for an added file, `new` None for a deleted one; a rename or copy gives both names."""
    rc, out = git_raw(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    base = out.decode().strip() if rc == 0 else EMPTY_TREE
    rc, out = git_raw(repo, "diff-index", "--cached", "-M", "--name-status", "-z", base, "--")
    need(rc == 0, "cannot read the staged changes of %s" % repo)
    f, out, i = zsplit(out), [], 0
    while i < len(f):
        st = f[i][0]
        if st in "RC":
            out.append((st, f[i + 1], f[i + 2]))
            i += 3
        else:
            out.append((st, None if st == "A" else f[i + 1], None if st == "D" else f[i + 1]))
            i += 2
    return base, out


def parity_trailer(text):
    """The value of the one `Parity:` line, None when there is none. Lines starting with '#' are dropped, as git does."""
    vals = [m.group(1).strip() for m in (re.match(r"^Parity:\s*(.*)$", line) for line in text.splitlines()
                                         if not line.startswith("#")) if m]
    need(len(vals) <= 1, "more than one Parity line: name every item in one `Parity: PAR-n, PAR-m`")
    return vals[0] if vals else None


# A named test is SwiftPM's `<Target>.<Type>/<method>`; Swift Testing ids carry `()` or argument labels after the
# method, which are dropped. `func <method>` is the declaration in both XCTest and Swift Testing (`@Test func`).
def parse_test(name):
    m = re.match(r"^(?:([^./]+)\.)?(?:([^/]+)/)?([^/(]+)(?:\(.*\))?$", name)
    if not m:
        return None
    target, typ, method = m.groups()
    return target, typ.split(".")[-1] if typ else None, method


def func_decl(text, method):
    return re.search(r"\bfunc\s+" + re.escape(method) + r"(?![A-Za-z0-9_])", text)


def holds(text, path, t):
    target, typ, method = t
    if target and "/%s/" % target not in "/" + path:
        return False
    if typ and not re.search(r"\b(?:class|struct|actor|enum|extension)\s+" + re.escape(typ) + r"\b", text):
        return False
    return func_decl(text, method) is not None


def has_test_attr(text, method):
    """Swift Testing: the `@Test` attribute between the previous declaration and `func <method>`."""
    m = func_decl(text, method)
    head = text[:m.start()]
    cut = max(head.rfind("}"), head.rfind("{"), head.rfind("func "))
    return "@Test" in head[cut + 1:]


def markers(text):
    return sum(text.count(s) for s in SKIP_MARKERS)


def named_test_problem(state, platform, repo, base, changes):
    named = []
    for iid in sorted(state["items"]):
        side = state["items"][iid]["platforms"].get(platform)
        for name in (side or {}).get("tests", []):
            t = parse_test(name)
            if t:
                named.append((iid, name, t))
    if not named:
        return None
    for st, old, new in changes:
        if old is None:
            continue
        raw = blob(repo, "%s:%s" % (base, old))
        if raw is None:
            continue
        before = raw.decode("utf-8", "replace")
        raw = blob(repo, ":" + new) if new else None
        after = raw.decode("utf-8", "replace") if raw is not None else None
        for iid, name, t in named:
            if not holds(before, old, t):
                continue
            if after is None or not holds(after, new, t):
                what = "deletes or renames"
            elif markers(after) > markers(before) or (has_test_attr(before, t[2]) and not has_test_attr(after, t[2])):
                what = "disables"
            else:
                continue
            return ("this commit %s %s's named test %s (in %s). A test an item names stays, runs and keeps its name; "
                    "change the item first (`parity.py change`, step P1)" % (what, iid, name, old))
    return None


def check_commit(spec, platform, repo, text):
    """None when the staged commit may be made, else ParityError(why). The order is the test file's contract."""
    need(platform in PLATFORMS, "unknown platform %r" % platform)
    pin = read_pin(blob(repo, ":" + PIN_FILE), "the staged tree")
    state = load_at(spec, pin)
    base, changes = staged_changes(repo)
    paths = sorted({p for _, o, n in changes for p in (o, n) if p})
    trailer = parity_trailer(text)
    vend = [p for p in paths if p.startswith(VENDORED)]
    need(not vend or (trailer is not None and SYNC_RE.match(trailer)),
         "a change to the vendored files %s: they change only through `parity.py sync` (trailer `Parity: sync "
         "<old>..<new>`); restore them" % ", ".join(vend))
    for st, old, new in changes:
        if st in "ARC" and classify(state, platform, new)[0] is None:
            raise ParityError("%s has no area and is on no exempt list of %s at the pin (default deny): classify it "
                              "with `parity.py area set` or `area exempt` in the spec, then sync" % (new, platform))
    why = named_test_problem(state, platform, repo, base, changes)
    need(why is None, why)
    hits = {}
    for p in paths:
        cls, name = classify(state, platform, p)
        if cls in TIERS:
            hits[name] = cls
    if not hits:
        return "no area hit (test and exempt paths only)"
    hit_text = ", ".join("%s (%s)" % (a, hits[a]) for a in sorted(hits))
    need(trailer is not None, "no Parity trailer; this commit hits %s. Add `Parity: PAR-n[, PAR-m]` naming the "
         "items these changes belong to (parity/PARITY.md at the pin), or add an item first" % hit_text)
    need(not trailer.startswith("n/a"), "`Parity: n/a` is refused on product paths in step P0; this commit hits %s: "
         "name an item, or add one (kind refactor for a change without behaviour)" % hit_text)
    need(not SYNC_RE.match(trailer), "a `Parity: sync` commit changes parity/ only; this one hits %s" % hit_text)
    need(ITEMS_RE.match(trailer), "the Parity trailer %r is not `PAR-n[, PAR-m]`" % trailer)
    ids = [i.strip() for i in trailer.split(",")]
    unknown = [i for i in ids if i not in state["items"]]
    need(not unknown, "unknown item %s at the staged pin %s (an item added to the spec later needs `parity.py sync` "
         "first)" % (", ".join(unknown), pin[:12]))
    covered = {state["items"][i]["area"] for i in ids}
    missing = sorted(set(hits) - covered)
    need(not missing, "%s does not cover %s: name an item of each area hit" % (", ".join(ids), ", ".join(missing)))
    for iid in ids:
        it = state["items"][iid]
        side = it["platforms"][platform]
        need(side is not None, "%s is null on %s: it cannot exist here, so it cannot carry this change" % (iid, platform))
        ex = exception_of(state, iid, platform)
        need(ex is None, "%s is excepted on %s (%s, to %s): an excepted item carries no change"
             % (iid, platform, ex and ex["waiver"], ex and ex["last_release"]))
        need(not is_shipped(state, iid, platform),
             "%s has shipped on %s: a shipped behaviour is changed through `parity.py change` (step P1)" % (iid, platform))
        for p in PLATFORMS:
            s = it["platforms"][p]
            if p == platform or s is None or is_shipped(state, iid, p) or exception_of(state, iid, p):
                continue
            nxt = next_releases(state, p)
            need(s["due"] in nxt and s["register"],
                 "%s: the %s side is open with due %s, which is not among %s's next releases (%s): give %s a due "
                 "release and a register id, or an exception, before this change lands"
                 % (iid, p, s["due"], p, ", ".join(nxt) or "none", p))
    return "%s cover %s" % (", ".join(ids), hit_text)


def coverage(spec, platform, repo):
    """The unclassified files of `repo`'s HEAD, judged at HEAD's pin."""
    need(platform in PLATFORMS, "unknown platform %r" % platform)
    state = load_at(spec, read_pin(blob(repo, "HEAD:" + PIN_FILE), "HEAD"))
    rc, out = git_raw(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    need(rc == 0, "cannot list the files of %s at HEAD" % repo)
    files = zsplit(out)
    return files, [p for p in files if classify(state, platform, p)[0] is None]


def sync(spec, repo, pin):
    """Write `pin` to <repo>/parity/SPEC_PIN and stage that path only; every check runs before any write. Never
    commits. Every git call strips REPO_VARS, on the clone side and on the tree side."""
    need(isinstance(pin, str) and PIN_RE.match(pin), "--pin %r is not a full 40- or 64-hex spec commit id" % (pin,))
    rc, out = git_raw(repo, "rev-parse", "--show-toplevel", spec_side=True)
    top = out.decode("utf-8", "surrogateescape").strip()
    need(rc == 0 and top and Path(top).resolve() == Path(repo).resolve(),
         "%s is not the top of a git work tree" % repo)
    need(spec.is_dir(), "no spec clone at %s" % spec)
    rc, out = git_raw(spec, "cat-file", "-t", pin, spec_side=True)
    need(rc == 0 and out.strip() == b"commit", "the spec clone %s holds no commit %s (fetch the spec)" % (spec, pin))
    rc, _ = git_raw(spec, "merge-base", "--is-ancestor", pin, "refs/heads/" + TRUNK, spec_side=True)
    need(rc == 0, "%s is not reachable from the spec's %s: only a %s commit can be pinned" % (pin, TRUNK, TRUNK))
    load_at(spec, pin)
    f = Path(repo) / PIN_FILE
    old = f.read_bytes() if f.exists() else None
    if old is not None:
        cur = old.decode("utf-8", "replace").strip()
        need(PIN_RE.match(cur), "%s holds %r, not a full spec commit id: the pin cannot be judged for moving back"
             % (PIN_FILE, cur))
        rc, out = git_raw(spec, "cat-file", "-t", cur, spec_side=True)
        need(rc == 0 and out.strip() == b"commit",
             "the spec clone %s does not hold the current pin %s (fetch the spec)" % (spec, cur))
        rc, _ = git_raw(spec, "merge-base", "--is-ancestor", pin, cur, spec_side=True)
        need(rc != 0 or pin == cur, "%s is an ancestor of the current pin %s: the pin never moves back" % (pin, cur))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes((pin + "\n").encode("ascii"))
    rc, _ = git_raw(repo, "add", "--", PIN_FILE, spec_side=True)
    if rc != 0:
        if old is None:
            f.unlink()
        else:
            f.write_bytes(old)
        raise ParityError("git add %s failed in %s" % (PIN_FILE, repo))


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
    cc = sp.add_parser("check-commit", help="judge the staged commit of --repo (the commit-msg hook)")
    cv = sp.add_parser("coverage", help="every file of --repo's HEAD is classified")
    for p in (cc, cv):
        p.add_argument("--platform", required=True)
        p.add_argument("--repo", required=True)
    cc.add_argument("--msg-file", required=True)
    sy = sp.add_parser("sync", help="write and stage --repo's parity/SPEC_PIN (never commits)")
    sy.add_argument("--repo", required=True)
    sy.add_argument("--pin", required=True)
    ns = ap.parse_args(argv)
    spec = Path(ns.spec)

    if ns.cmd == "sync":
        try:  # every failure blocks, and nothing in --repo changes
            sync(spec, Path(ns.repo), ns.pin)
            print("SYNC OK %s" % ns.pin)
            return 0
        except ParityError as e:
            print("BLOCKED: %s" % e)
        except Exception as e:  # noqa: BLE001
            print("BLOCKED: cannot sync (%s: %s)" % (type(e).__name__, e))
        return 1

    if ns.cmd == "check-commit":
        try:  # every failure to judge blocks (fails closed)
            text = Path(ns.msg_file).read_text(encoding="utf-8")
            print("PARITY PASS: %s" % check_commit(spec, ns.platform, Path(ns.repo), text))
            return 0
        except ParityError as e:
            print("BLOCKED: %s" % e)
        except Exception as e:  # noqa: BLE001
            print("BLOCKED: cannot judge the commit (%s: %s)" % (type(e).__name__, e))
        return 1
    if ns.cmd == "coverage":
        try:
            files, bad = coverage(spec, ns.platform, Path(ns.repo))
        except Exception as e:  # noqa: BLE001
            print("COVERAGE FAIL: cannot judge (%s)" % e)
            return 1
        if bad:
            print("COVERAGE FAIL: %d of %d files of %s have no area and no exempt pattern:\n  %s"
                  % (len(bad), len(files), ns.platform, "\n  ".join(bad)))
            return 1
        print("COVERAGE OK: %d files classified" % len(files))
        return 0

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
