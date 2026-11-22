"""Test helpers: an isolated spec repo in a temp dir, and a runner for tools/parity.py.

Every git call here and in the tool runs with no system or global config, a temp HOME and no replace objects,
so a test never reads or writes the owner's real settings, lock or spec clone.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
TOOL = TOOLS / "parity.py"


def clean_env(tmp):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "ZCHAT_"))}
    home = Path(tmp) / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text("")
    env.update({
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_AUTHOR_NAME": "Spec Test", "GIT_AUTHOR_EMAIL": "spec@test.invalid",
        "GIT_COMMITTER_NAME": "Spec Test", "GIT_COMMITTER_EMAIL": "spec@test.invalid",
        "ZCHAT_PARITY_LOCK": str(Path(tmp) / "parity.lock"),
        "ZCHAT_PARITY_CACHE": str(Path(tmp) / "cache"),
        "ZCHAT_WAIVERS": str(Path(tmp) / "WAIVERS_IOS.md"),
        "ZCHAT_PARITY_BY": "test",
    })
    return env


def git(repo, *args, env, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError("git %s failed: %s" % (" ".join(args), r.stderr))
    return r.stdout.strip()


class SpecCase(unittest.TestCase):
    """A fresh spec repo on `trunk` with one plain commit, per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spec-test-")
        self.env = clean_env(self.tmp)
        self.spec = Path(self.tmp) / "spec"
        self.spec.mkdir()
        git(self.spec, "init", "-q", "-b", "trunk", env=self.env)
        (self.spec / "README.md").write_text("spec\n")
        git(self.spec, "add", "README.md", env=self.env)
        git(self.spec, "commit", "-q", "-m", "chore: start", env=self.env)
        self.waivers = Path(self.env["ZCHAT_WAIVERS"])
        self.waivers.write_text("# waivers\n\n## WI-9 — test approval\nOwner, verbatim: \"Yes\"\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *args, expect=0, spec=None):
        r = subprocess.run([sys.executable, "-I", str(TOOL), "--spec", str(spec or self.spec), *args],
                           env=self.env, capture_output=True, text=True, timeout=60)
        if expect is not None and r.returncode != expect:
            self.fail("parity.py %s: exit %d, expected %d\nstdout: %s\nstderr: %s"
                      % (" ".join(args), r.returncode, expect, r.stdout, r.stderr))
        return r

    def refused(self, *args, says=None):
        r = self.run_tool(*args, expect=1)
        if says:
            self.assertIn(says, r.stdout + r.stderr)
        return r

    def commits(self):
        return int(git(self.spec, "rev-list", "--count", "HEAD", env=self.env))

    def p(self, rel):
        return self.spec / "parity" / rel

    def seed(self):
        """init, the planned releases, a small classification. Returns nothing; every op commits."""
        self.run_tool("init")
        for plat, ver, cls in [("android", "2.13.1", "outward"), ("android", "2.13.2", "outward"),
                               ("android", "2.13.3", "outward"), ("android", "2.14.0", "outward"),
                               ("ios", "0.1.0", "internal"), ("ios", "0.2.0", "internal"),
                               ("ios", "1.0.0", "outward"), ("ios", "1.1.0", "outward"),
                               ("backend", "2026.12", "outward")]:
            self.run_tool("release", "add", "--platform", plat, "--version", ver, "--class", cls)
        for plat, pat, area, tier in [("ios", "Packages/ZChatCore/Sources/Money/**", "money.consent", "hard"),
                                      ("ios", "Packages/ZChatCore/Sources/Wire/**", "zmsg", "hard"),
                                      ("ios", "App/Views/**", "ui.chat", "local"),
                                      ("android", "ui-lib/src/main/**/money/**", "money.consent", "hard")]:
            self.run_tool("area", "set", "--platform", plat, "--pattern", pat, "--area", area, "--tier", tier)

    def add_item(self, *extra, kind="behaviour", area="money.consent", expect=0):
        args = ["add", "--kind", kind, "--area", area, "--title", "A test behaviour", "--source", "W-236"]
        args += list(extra)
        return self.run_tool(*args, expect=expect)
