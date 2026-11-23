"""Step P0: a minimal `parity.py sync` that writes and stages an app tree's parity/SPEC_PIN (plan tool step P0, D.2,
D.5, D.6; appendix X4 tests 19, 20, 41). The guard refuses hand edits of parity files, so the first SPEC_PIN of the
iOS tree can only come from this command.

The contract these tests pin (the implementer builds to it):

  parity.py --spec <clone> sync --repo <app tree> --pin <sha>
    exit 0, stdout holds "SYNC OK <sha>"   <repo>/parity/SPEC_PIN now holds exactly "<sha>\\n" and is staged
                                           (`git -C <repo> add parity/SPEC_PIN`, that path only); nothing is committed
    exit 1, stdout holds "BLOCKED: <why>"  nothing in <repo> changes: not the file, not the index, not HEAD
  Refused (BLOCKED):
   - <sha> that is not a full 40- or 64-hex id (an abbreviation, a ref name);
   - <sha> the clone does not hold, or that is not a commit (a tree id);
   - a commit that is not reachable from the clone's `trunk` (X4 test 41);
   - a commit whose registry does not replay and compare like `validate`, read from git objects at <sha> (as
     check-commit's load_at does; the clone's working tree does not count), including a commit with no registry;
   - a pin that moves back: <sha> is a strict ancestor of the pin in <repo>'s current parity/SPEC_PIN;
   - a <repo> that is not a git work tree.
  Out of scope here: the vendored vectors and runner copy (later), re-syncing to the same pin (undecided).

Then a commit that stages only parity/SPEC_PIN, message trailer `Parity: sync`, passes check-commit. That holds at
HEAD already, and not through the vendored carve-out: a bare `Parity: sync` does not match `sync <old>..<new>`; the
commit passes because parity/SPEC_PIN is an added file that the pin's `parity/**` exempt pattern classifies, so no
area is hit and the trailer is never read. test_a_staged_pin_only_commit_passes_check_commit pins that.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import TOOL, clean_env, git  # noqa: E402

PIN = "parity/SPEC_PIN"
SYNC_MSG = "chore(parity): pin the spec\n\nParity: sync\n"
_SHARED = {}


def run(env, spec, *args, expect=0):
    r = subprocess.run([sys.executable, "-I", str(TOOL), "--spec", str(spec), *args], env=env, capture_output=True,
                       text=True, timeout=60)
    if expect is not None and r.returncode != expect:
        raise AssertionError("parity.py %s: exit %d, expected %d\nstdout: %s\nstderr: %s"
                             % (" ".join(args), r.returncode, expect, r.stdout, r.stderr))
    return r


def build_spec(root):
    """The shared spec. Returns (spec, pins) with pins:
    start    the first commit on trunk, before `init` (no registry)
    old      a valid registry with releases, iOS areas and the iOS `parity/**` exempt pattern
    new      `old` plus one item (a descendant of `old` on trunk)
    side     `new` plus a README change on branch `side` only (valid registry, not reachable from trunk)
    bad      on trunk after `new`: a hand edit of PARITY.json (does not replay); it stays the trunk head
    tree     the tree id of `new` (held by the clone, not a commit)
    """
    env = clean_env(root)
    spec = Path(root) / "spec"
    spec.mkdir()
    pins = {}
    git(spec, "init", "-q", "-b", "trunk", env=env)
    (spec / "README.md").write_text("spec\n")
    git(spec, "add", "README.md", env=env)
    git(spec, "commit", "-q", "-m", "chore: start", env=env)
    pins["start"] = git(spec, "rev-parse", "HEAD", env=env)
    run(env, spec, "init")
    for plat, ver, cls in [("android", "2.13.1", "outward"), ("ios", "0.1.0", "internal"),
                           ("ios", "1.0.0", "outward"), ("backend", "2026.12", "outward")]:
        run(env, spec, "release", "add", "--platform", plat, "--version", ver, "--class", cls)
    run(env, spec, "area", "set", "--platform", "ios", "--pattern", "Packages/ZChatCore/Sources/Money/**",
        "--area", "money.consent", "--tier", "hard")
    for pat in ("parity/**", "docs/**"):
        run(env, spec, "area", "exempt", "--platform", "ios", "--pattern", pat, "--reason",
            "tests, harness, docs or own checks")
    run(env, spec, "validate")
    pins["old"] = git(spec, "rev-parse", "HEAD", env=env)
    run(env, spec, "add", "--kind", "behaviour", "--area", "money.consent", "--title", "A test behaviour",
        "--source", "W-236", "--due", "ios=1.0.0", "--register", "ios=IOSP-3", "--null", "android",
        "--null", "backend")
    run(env, spec, "validate")
    pins["new"] = git(spec, "rev-parse", "HEAD", env=env)
    git(spec, "switch", "-q", "-c", "side", env=env)
    (spec / "README.md").write_text("spec, side branch\n")
    git(spec, "commit", "-q", "-am", "docs: side note", env=env)
    pins["side"] = git(spec, "rev-parse", "HEAD", env=env)
    git(spec, "switch", "-q", "trunk", env=env)
    run(env, spec, "validate")
    f = spec / "parity" / "PARITY.json"
    f.write_text(f.read_text().replace("A test behaviour", "A softer behaviour", 1))
    git(spec, "commit", "-q", "-am", "hand edit", env=env)
    pins["bad"] = git(spec, "rev-parse", "HEAD", env=env)
    pins["tree"] = git(spec, "rev-parse", "%s^{tree}" % pins["new"], env=env)
    return spec, pins


def setUpModule():
    root = tempfile.mkdtemp(prefix="spec-sync-")
    _SHARED["root"] = root
    _SHARED["spec"], _SHARED["pins"] = build_spec(root)


def tearDownModule():
    shutil.rmtree(_SHARED.get("root", ""), ignore_errors=True)


class SyncCase(unittest.TestCase):
    """A fresh iOS tree per test with one commit and no parity/SPEC_PIN, plus a modified tracked file and an
    untracked file, so a sync that stages more than parity/SPEC_PIN shows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ios-sync-")
        self.env = clean_env(self.tmp)
        self.spec = _SHARED["spec"]
        self.pins = _SHARED["pins"]
        self.repo = Path(self.tmp) / "ios"
        self.repo.mkdir()
        self.g("init", "-q", "-b", "dev")
        self.write("docs/notes.md", "notes\n")
        self.g("add", "-A")
        self.g("commit", "-q", "-m", "chore: start")
        self.dirty()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def g(self, *args):
        return git(self.repo, *args, env=self.env)

    def write(self, rel, text):
        f = self.repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def dirty(self):
        self.write("docs/notes.md", "notes, edited\n")
        self.write("docs/untracked.md", "untracked\n")

    def commit_pin(self, sha):
        """Set up a tree that already pins `sha` (plain git, as history), then dirty it again."""
        self.g("stash", "-q", "-u")
        self.write(PIN, sha + "\n")
        self.g("add", PIN)
        self.g("commit", "-q", "-m", "chore(parity): pin")
        self.g("stash", "pop", "-q")

    def staged(self):
        return self.g("diff", "--cached", "--name-only").splitlines()

    def head_count(self):
        return int(self.g("rev-list", "--count", "HEAD"))

    def sync(self, pin, expect=0, spec=None, repo=None):
        return run(self.env, spec or self.spec, "sync", "--repo", str(repo or self.repo), "--pin", pin,
                   expect=expect)

    def synced(self, pin, spec=None):
        before = self.head_count()
        r = self.sync(pin, 0, spec)
        self.assertIn("SYNC OK %s" % pin, r.stdout)
        self.assertNotIn("BLOCKED", r.stdout)
        self.assertEqual((self.repo / PIN).read_bytes(), (pin + "\n").encode())
        self.assertEqual(self.g("cat-file", "blob", ":" + PIN) + "\n", pin + "\n")
        self.assertEqual(self.staged(), [PIN], "sync staged more (or less) than parity/SPEC_PIN")
        self.assertEqual(self.head_count(), before, "sync made a commit")
        self.assertEqual(self.g("status", "--porcelain", "--", "docs/notes.md"), "M docs/notes.md",
                         "sync touched a tracked file it does not own")
        self.assertTrue((self.repo / "docs/untracked.md").exists())
        return r

    def refused(self, pin, *says, spec=None, repo=None):
        f = self.repo / PIN
        before = (f.read_bytes() if f.exists() else None, self.staged(), self.g("rev-parse", "HEAD"))
        r = self.sync(pin, 1, spec, repo)
        self.assertIn("BLOCKED: ", r.stdout)
        self.assertNotIn("SYNC OK", r.stdout)
        for s in says:
            self.assertIn(s, r.stdout)
        after = (f.read_bytes() if f.exists() else None, self.staged(), self.g("rev-parse", "HEAD"))
        self.assertEqual(after, before, "a refused sync changed the tree, the index or HEAD")
        return r

    def check(self, message, spec=None):
        mf = Path(self.tmp) / "COMMIT_EDITMSG"
        mf.write_text(message, encoding="utf-8")
        return run(self.env, spec or self.spec, "check-commit", "--platform", "ios", "--repo", str(self.repo),
                   "--msg-file", str(mf), expect=None)

    def copy_spec(self):
        dst = Path(self.tmp) / "spec-copy"
        shutil.copytree(self.spec, dst, symlinks=True)
        return dst


class Sync(SyncCase):
    # -- it writes and stages the first pin, and the commit of it passes the hook
    def test_first_pin_is_written_and_staged_not_committed(self):
        self.assertFalse((self.repo / PIN).exists())
        self.synced(self.pins["new"])

    def test_first_sync_commit_passes_check_commit(self):
        self.synced(self.pins["new"])
        self.g("stash", "-q", "--keep-index", "-u")
        self.assertEqual(self.staged(), [PIN])
        r = self.check(SYNC_MSG)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PARITY PASS", r.stdout)

    def test_sync_into_a_tree_with_no_commit_yet(self):
        repo = Path(self.tmp) / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "dev", env=self.env)
        r = self.sync(self.pins["new"], 0, repo=repo)
        self.assertIn("SYNC OK %s" % self.pins["new"], r.stdout)
        self.assertEqual((repo / PIN).read_bytes(), (self.pins["new"] + "\n").encode())
        self.assertEqual(git(repo, "ls-files", "--cached", env=self.env).splitlines(), [PIN])
        self.assertEqual(git(repo, "rev-list", "--all", env=self.env), "", "sync made a commit")

    def test_the_pin_moves_forward(self):
        self.commit_pin(self.pins["old"])
        self.synced(self.pins["new"])

    def test_the_clone_working_tree_does_not_count(self):
        spec = self.copy_spec()
        (spec / "parity" / "PARITY.log").unlink()
        (spec / "parity" / "PARITY.json").write_text("{not json")
        self.synced(self.pins["new"], spec=spec)

    # -- what it refuses
    def test_not_a_full_commit_id_refused(self):
        for pin in (self.pins["new"][:7], self.pins["new"][:39], "trunk", "HEAD", ""):
            with self.subTest(pin=pin):
                self.refused(pin)

    def test_an_id_the_clone_does_not_hold_refused(self):
        self.refused("0123456789abcdef0123456789abcdef01234567")

    def test_a_tree_id_refused(self):
        self.refused(self.pins["tree"])

    def test_an_off_trunk_commit_refused(self):
        self.refused(self.pins["side"], "trunk")

    def test_reachability_is_from_trunk_not_the_clone_head(self):
        spec = self.copy_spec()
        git(spec, "switch", "-q", "side", env=self.env)
        self.refused(self.pins["side"], "trunk", spec=spec)
        self.synced(self.pins["new"], spec=spec)

    def test_a_pin_whose_registry_does_not_replay_refused(self):
        self.refused(self.pins["bad"])

    def test_a_pin_with_no_registry_refused(self):
        self.refused(self.pins["start"])

    def test_a_pin_that_moves_back_refused(self):
        self.commit_pin(self.pins["new"])
        self.refused(self.pins["old"])

    def test_a_missing_spec_clone_refused(self):
        self.refused(self.pins["new"], spec=Path(self.tmp) / "no-spec")

    def test_a_repo_that_is_not_a_work_tree_refused(self):
        nowhere = Path(self.tmp) / "not-a-repo"
        nowhere.mkdir()
        r = self.sync(self.pins["new"], 1, repo=nowhere)
        self.assertIn("BLOCKED: ", r.stdout)
        self.assertFalse((nowhere / PIN).exists())


class CheckCommitOnAPinOnlyCommit(SyncCase):
    """Green at HEAD: pins that check-commit already lets a commit staging only parity/SPEC_PIN through, with the
    trailer `Parity: sync`, because `parity/**` is exempt at the pin (not through the vendored carve-out)."""

    def test_a_staged_pin_only_commit_passes_check_commit(self):
        self.g("stash", "-q", "-u")
        self.write(PIN, self.pins["new"] + "\n")
        self.g("add", PIN)
        r = self.check(SYNC_MSG)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PARITY PASS", r.stdout)
        self.assertIn("no area hit", r.stdout)


if __name__ == "__main__":
    unittest.main()
