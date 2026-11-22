"""Step P0, slice A: registry operations, log replay, validate (plan D.2, D.3, D.12; appendix X4).

X4 tests covered here: 9 (the `add` half), 10 (exception binding and the approvals check), 18 (the validate half),
35, 38, 43, 58 (the `fix` and build-out halves). The `change` half of 58 is step P1.
"""
import json
import subprocess
import sys
import unittest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from helpers import SpecCase, TOOL, git  # noqa: E402

import parity  # noqa: E402

ALL_NULL_BUT_IOS = ["--null", "android", "--null", "backend"]


def ios_open(due="1.0.0", reg="IOSP-3"):
    return ["--due", "ios=" + due, "--register", "ios=" + reg]


class VersionCompare(unittest.TestCase):
    """X4 test 35."""

    def test_1_10_after_1_9(self):
        self.assertGreater(parity.vkey("1.10.0"), parity.vkey("1.9.0"))
        self.assertLess(parity.vkey("0.9.0"), parity.vkey("0.10.0"))
        self.assertEqual(parity.vkey("2.13.1"), parity.vkey("2.13.1"))

    def test_bad_version_refused(self):
        for bad in ["", "1.x", "v1.0", "1..0", "1.0-beta"]:
            with self.assertRaises(parity.ParityError):
                parity.vkey(bad)


class InitAndValidate(SpecCase):
    def test_init_makes_one_commit_and_validates(self):
        before = self.commits()
        self.run_tool("init")
        self.assertEqual(self.commits(), before + 1)
        for f in ["PARITY.json", "PARITY.log", "areas.json", "releases.json", "exceptions.json", "PARITY.md"]:
            self.assertTrue(self.p(f).exists(), f)
        self.assertIn("VALIDATE OK", self.run_tool("validate").stdout)
        self.refused("init", says="already")

    def test_replay_is_pure(self):
        self.seed()
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS)
        first = {f: self.p(f).read_bytes() for f in ["PARITY.json", "areas.json", "releases.json", "PARITY.md"]}
        self.run_tool("validate")
        self.run_tool("render")
        self.assertEqual(git(self.spec, "status", "--porcelain", env=self.env), "")
        for f, b in first.items():
            self.assertEqual(self.p(f).read_bytes(), b, f)

    def test_every_write_op_is_one_clean_commit(self):
        self.seed()
        n = self.commits()
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS)
        self.assertEqual(self.commits(), n + 1)
        self.assertEqual(git(self.spec, "status", "--porcelain", env=self.env), "")
        msg = git(self.spec, "log", "-1", "--format=%B", env=self.env)
        self.assertIn("PAR-0001", msg)


class HandEditsFail(SpecCase):
    """X4 test 18 (validate half): a hand edit of any normative file is found by the log replay."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS)

    def assert_validate_fails(self, says=None):
        r = self.run_tool("validate", expect=1)
        self.assertIn("VALIDATE FAIL", r.stdout)
        if says:
            self.assertIn(says, r.stdout)

    def test_title_edited(self):
        f = self.p("PARITY.json")
        f.write_text(f.read_text().replace("A test behaviour", "A softer behaviour"))
        self.assert_validate_fails("PARITY.json")

    def test_invalid_json(self):
        self.p("PARITY.json").write_text("{not json")
        self.assert_validate_fails()

    def test_areas_edited(self):
        f = self.p("areas.json")
        f.write_text(f.read_text().replace('"hard"', '"local"', 1))
        self.assert_validate_fails("areas.json")

    def test_releases_edited(self):
        f = self.p("releases.json")
        f.write_text(f.read_text().replace('"1.1.0"', '"9.9.0"'))
        self.assert_validate_fails("releases.json")

    def test_log_line_removed(self):
        f = self.p("PARITY.log")
        lines = f.read_text().splitlines(True)
        f.write_text("".join(lines[:-1]))
        self.assert_validate_fails()

    def test_forged_log_line_that_breaks_a_rule(self):
        f = self.p("PARITY.log")
        last = json.loads(f.read_text().splitlines()[-1])
        forged = dict(last, seq=last["seq"] + 1)
        forged["args"] = dict(last["args"], id="PAR-0002", sides=dict(last["args"]["sides"],
                                                                    ios={"due": "7.0.0", "register": "IOSP-3",
                                                                         "tests": [], "cases": []}))
        with open(f, "a") as fh:
            fh.write(json.dumps(forged, sort_keys=True) + "\n")
        self.assert_validate_fails()

    def test_missing_spec_dir(self):
        r = self.run_tool("validate", expect=1, spec=self.spec / "nope")
        self.assertIn("VALIDATE FAIL", r.stdout)

    def test_extra_snapshot_file(self):
        (self.p("releases")).mkdir(exist_ok=True)
        self.p("releases/ios-1.0.0.json").write_text("{}\n")
        self.assert_validate_fails("releases/ios-1.0.0.json")


class AddRules(SpecCase):
    """X4 tests 9 (add half) and 58."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_due_not_in_releases_refused(self):
        self.add_item(*ios_open(due="1.5.0"), *ALL_NULL_BUT_IOS, expect=1)

    def test_build_out_item_may_be_due_at_any_listed_release(self):
        # iOS has shipped nothing: 1.1.0 is the fourth listed iOS release.
        self.add_item(*ios_open(due="1.1.0"), *ALL_NULL_BUT_IOS)

    def test_fix_item_due_further_than_two_releases_refused(self):
        r = self.add_item(*ios_open(due="1.0.0"), *ALL_NULL_BUT_IOS, "--found-on", "ios",
                          "--fix-register", "ZC-2700", kind="fix", expect=1)
        self.assertIn("two releases", r.stdout + r.stderr)
        self.add_item(*ios_open(due="0.2.0"), *ALL_NULL_BUT_IOS, "--found-on", "ios",
                      "--fix-register", "ZC-2700", kind="fix")

    def test_fix_needs_register_and_found_on(self):
        self.add_item(*ios_open(due="0.1.0"), *ALL_NULL_BUT_IOS, kind="fix", expect=1)

    def test_after_first_outward_release_the_limit_binds_every_item(self):
        self.run_tool("baseline", "--platform", "android", "--version", "2.13.1")
        android_late = ["--due", "android=2.14.0", "--register", "android=ZC-2701"]
        self.add_item(*android_late, *ios_open(), "--null", "backend", expect=1)
        android_ok = ["--due", "android=2.13.3", "--register", "android=ZC-2701"]
        self.add_item(*android_ok, *ios_open(), "--null", "backend")

    def test_due_at_or_before_a_shipped_release_refused(self):
        self.run_tool("baseline", "--platform", "android", "--version", "2.13.1")
        self.add_item("--due", "android=2.13.1", "--register", "android=ZC-2701", *ios_open(), "--null", "backend",
                      expect=1)

    def test_every_platform_must_be_named(self):
        self.add_item(*ios_open(), "--null", "android", expect=1)

    def test_open_side_needs_register(self):
        self.add_item("--due", "ios=1.0.0", *ALL_NULL_BUT_IOS, expect=1)

    def test_unknown_area_or_kind_refused(self):
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, area="nowhere", expect=1)
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, kind="status", expect=1)

    def test_wire_kinds_need_cases(self):
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, kind="wire", area="zmsg", expect=1)
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, "--case", "ios=zmsg/prefix.json#*", kind="wire", area="zmsg")

    def test_money_rule_shape(self):
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, kind="money_rule", expect=1)
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, "--money-shape", "table", kind="money_rule", expect=1)
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, "--money-shape", "table", "--case", "ios=money/q.json#*",
                      kind="money_rule")

    def test_baseline_shipped_side(self):
        self.run_tool("baseline", "--platform", "android", "--version", "2.13.1")
        r = self.add_item("--due", "android=2.13.1", "--baseline-shipped", "android", "--test",
                          "android=SomeTest", *ios_open(), "--null", "backend")
        snap = json.loads(self.p("releases/android-2.13.1.json").read_text())
        self.assertEqual(snap["proven"], ["PAR-0001"])
        self.refused("baseline", "--platform", "android", "--version", "2.13.1", says="already")
        self.run_tool("validate")
        del r

    def test_ids_are_sequential(self):
        for _ in range(3):
            self.add_item(*ios_open(), *ALL_NULL_BUT_IOS)
        ids = [i["id"] for i in json.loads(self.p("PARITY.json").read_text())["items"]]
        self.assertEqual(ids, ["PAR-0001", "PAR-0002", "PAR-0003"])


class WhereWritesHappen(SpecCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_refused_off_trunk(self):
        git(self.spec, "checkout", "-q", "-b", "side", env=self.env)
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, expect=1)

    def test_refused_with_uncommitted_registry_state(self):
        f = self.p("PARITY.md")
        f.write_text(f.read_text() + "\nlocal note\n")
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, expect=1)

    def test_refused_when_files_do_not_replay(self):
        f = self.p("PARITY.json")
        f.write_text(f.read_text().replace("[]", "[ ]"))
        git(self.spec, "commit", "-q", "-am", "hand edit", env=self.env)
        self.add_item(*ios_open(), *ALL_NULL_BUT_IOS, expect=1)


class Concurrency(SpecCase):
    """X4 test 43: two `add` runs at once give two ids, two commits and a log that replays."""

    def test_two_adds_at_once(self):
        self.seed()
        n = self.commits()
        args = [sys.executable, "-I", str(TOOL), "--spec", str(self.spec), "add", "--kind", "behaviour",
                "--area", "money.consent", "--title", "Parallel", "--source", "W-236", *ios_open(),
                *ALL_NULL_BUT_IOS]
        procs = [subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0], outs)
        self.assertEqual(self.commits(), n + 2)
        ids = sorted(i["id"] for i in json.loads(self.p("PARITY.json").read_text())["items"])
        self.assertEqual(ids, ["PAR-0001", "PAR-0002"])
        self.run_tool("validate")


class Areas(SpecCase):
    """X4 test 38 and the P0 refusal of tier downgrades (test 39's verdict path is step P1)."""

    def setUp(self):
        super().setUp()
        self.run_tool("init")

    def test_never_exempt_patterns_refused(self):
        for plat, pat in [("android", "**/src/main/**"), ("android", "app/src/release/**"), ("android", "**"),
                          ("android", "**/*.gradle.kts"), ("android", "gradle.properties"),
                          ("android", "gradle/libs.versions.toml"), ("android", "**/proguard-rules.pro"),
                          ("android", "buildSrc/**"), ("ios", "Package.swift"), ("ios", "**/Package.resolved"),
                          ("ios", "project.yml"), ("ios", "Config/*.xcconfig"), ("ios", "App/*.entitlements"),
                          ("ios", "**/Info.plist"), ("ios", ".github/workflows/release.yml"),
                          ("ios", ".github/workflows/**"), ("backend", "prisma/**"),
                          ("backend", "apps/backend/prisma/**"), ("backend", "scripts/**"),
                          ("backend", "package.json"), ("backend", "**/package-lock.json"),
                          ("backend", "apps/backend/src/**"), ("ios", "Packages/**")]:
            with self.subTest(pattern=pat):
                self.refused("area", "exempt", "--platform", plat, "--pattern", pat, "--reason", "test exemption")

    def test_test_and_harness_patterns_accepted(self):
        for plat, pat in [("android", "**/src/test/**"), ("android", "**/src/androidTest/**"),
                          ("ios", "Tests/**"), ("ios", "Packages/ZChatCore/Tests/**"), ("ios", "tools/qa/**"),
                          ("ios", ".claude/**"), ("ios", "docs/**"), ("ios", "AGENTS.md")]:
            with self.subTest(pattern=pat):
                self.run_tool("area", "exempt", "--platform", plat, "--pattern", pat, "--reason", "tests or harness")
        self.run_tool("validate")

    def test_tier_downgrade_refused_in_p0(self):
        self.run_tool("area", "set", "--platform", "ios", "--pattern", "Packages/ZChatCore/Sources/**",
                      "--area", "core", "--tier", "hard")
        self.refused("area", "set", "--platform", "ios", "--pattern", "Packages/ZChatCore/Sources/**",
                     "--area", "core", "--tier", "mixed", says="area-review")
        self.refused("area", "exempt", "--platform", "ios", "--pattern", "Packages/ZChatCore/Sources/**",
                     "--reason", "moving it off")
        self.run_tool("area", "set", "--platform", "ios", "--pattern", "App/Glue/**", "--area", "glue",
                      "--tier", "local")
        self.run_tool("area", "set", "--platform", "ios", "--pattern", "App/Glue/**", "--area", "glue",
                      "--tier", "mixed")

    def test_short_reason_refused(self):
        self.refused("area", "exempt", "--platform", "ios", "--pattern", "Tests/**", "--reason", "tests")


class Exceptions(SpecCase):
    """X4 test 10: an exception is bound to one item and one platform, and to the verbatim owner text."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.add_item(*ios_open(), "--due", "android=2.13.2", "--register", "android=ZC-2702", "--null", "backend")

    def test_waiver_must_exist(self):
        self.refused("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-77",
                     "--last-release", "1.0.0", says="WI-77")

    def test_exception_recorded_with_hash(self):
        self.run_tool("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-9",
                      "--last-release", "1.0.0")
        ex = json.loads(self.p("exceptions.json").read_text())["exceptions"]
        self.assertEqual(len(ex), 1)
        self.assertEqual((ex[0]["item"], ex[0]["platform"], ex[0]["waiver"]), ("PAR-0001", "ios", "WI-9"))
        self.assertEqual(len(ex[0]["waiver_sha256"]), 64)
        self.assertIn("APPROVALS OK", self.run_tool("validate", "--approvals").stdout)

    def test_changed_waiver_text_fails_the_approvals_check(self):
        self.run_tool("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-9",
                      "--last-release", "1.0.0")
        self.waivers.write_text(self.waivers.read_text().replace('"Yes"', '"Yes, for now"'))
        r = self.run_tool("validate", "--approvals", expect=1)
        self.assertIn("WI-9", r.stdout)

    def test_removed_waiver_fails_the_approvals_check(self):
        self.run_tool("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-9",
                      "--last-release", "1.0.0")
        self.waivers.write_text("# waivers\n")
        self.run_tool("validate", "--approvals", expect=1)

    def test_exception_on_a_null_side_or_unknown_release_refused(self):
        self.refused("except", "--item", "PAR-0001", "--platform", "backend", "--waiver", "WI-9",
                     "--last-release", "2026.12")
        self.refused("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-9",
                     "--last-release", "4.0.0")
        self.refused("except", "--item", "PAR-0009", "--platform", "ios", "--waiver", "WI-9",
                     "--last-release", "1.0.0")

    def test_one_exception_per_item_and_platform(self):
        self.run_tool("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-9",
                      "--last-release", "1.0.0")
        self.refused("except", "--item", "PAR-0001", "--platform", "ios", "--waiver", "WI-9",
                     "--last-release", "1.1.0")


if __name__ == "__main__":
    unittest.main()
