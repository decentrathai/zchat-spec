"""Step P0, slice A follow-ups (advisor review of c8d2b3f).

- The default lock lives in another session's protected tree (~/.config/zchat-qa/locks/parity.lock). The tool never
  creates it there: without an injected ZCHAT_PARITY_LOCK the file must already exist.
- Annex A7's own exempt pattern `**/src/*Test/**` is accepted; a bare `*` source set stays refused.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import SpecCase  # noqa: E402


class DefaultLock(SpecCase):
    def setUp(self):
        super().setUp()
        del self.env["ZCHAT_PARITY_LOCK"]
        self.lockdir = Path(self.env["HOME"]) / ".config/zchat-qa/locks"
        self.lockdir.mkdir(parents=True)
        self.lock = self.lockdir / "parity.lock"

    def test_default_lock_is_never_created(self):
        r = self.refused("init", says="parity.lock")
        self.assertFalse(self.lock.exists(), "the tool created the default lock file")
        self.assertFalse((self.spec / "parity").exists())
        del r

    def test_default_lock_used_when_it_exists(self):
        self.lock.write_text("")
        self.run_tool("init")
        self.assertEqual(self.lock.read_text(), "")


class ExemptTestSourceSets(SpecCase):
    def setUp(self):
        super().setUp()
        self.run_tool("init")

    def test_a7_patterns_accepted(self):
        for plat, pat in [("android", "**/src/*Test/**"), ("ios", "tools/qa/tests/**"),
                          ("android", "**/src/testFixtures/**")]:
            with self.subTest(pattern=pat):
                self.run_tool("area", "exempt", "--platform", plat, "--pattern", pat, "--reason",
                              "test source sets, never shipped")

    def test_wildcard_source_sets_refused(self):
        for pat in ["**/src/*/**", "**/src/*Test*/**", "**/src/?est/**", "app/src/**/test/**"]:
            with self.subTest(pattern=pat):
                self.refused("area", "exempt", "--platform", "android", "--pattern", pat, "--reason",
                             "a long enough reason for rule 38", says="cannot be exempt")


if __name__ == "__main__":
    unittest.main()
