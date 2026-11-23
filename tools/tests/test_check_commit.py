"""Step P0, slice B: `check-commit`, the default-deny classification and `coverage` (plan D.4, D.5; annex A7, A8;
appendix X4 tests 1, 2, 3, 4, 5, 6, 15, 16, 20, 37, 57).

The contract these tests pin (the implementer builds to it):

  parity.py --spec <clone> check-commit --platform <android|ios|backend> --repo <tree> --msg-file <file>
    Judges the commit about to be made in <tree>: its STAGED index (git diff --cached against HEAD, renames give both
    names, deletions count) and the message in <file> (lines starting with '#' are dropped, as git does).
    exit 0, stdout holds "PARITY PASS"            the commit may be made
    exit 1, stdout holds "BLOCKED: <why>"         the commit is refused; <why> names the reason and the way forward
    Every failure to judge (spec clone missing, pin unknown, log replay mismatch) is exit 1 BLOCKED (fails closed).

  parity.py --spec <clone> coverage --platform <p> --repo <tree>
    Every file of <tree>'s HEAD commit must be classified (an area or an exempt pattern), read at HEAD's SPEC_PIN.
    exit 0 "COVERAGE OK"; exit 1 "COVERAGE FAIL" and every unclassified path.

  parity.classify(state, platform, path) -> (cls, name)
    cls "hard" | "mixed" | "local" with name = the area; "test" | "exempt" with name = the exempt pattern that matched;
    (None, None) when nothing matches (default deny). "test" is an exempt match on a test path by the tool's own fixed
    rule (annex A7's test patterns plus the iOS test roots `Packages/*/Tests/`, `App/Tests/`, `Tests/`,
    `tools/qa/tests/`); a test path that is on no exempt pattern is unclassified. Only `platform`'s lists count.

  The order check-commit decides in (each step's refusal is pinned by a test below):
   1. The staged tree has `parity/SPEC_PIN`, a full spec commit id that the clone holds; else BLOCKED naming SPEC_PIN.
      Until parity/ACTIVATION exists (slice C) every staged commit is checked, so this holds for doc-only commits too.
      Registry, areas and exceptions are read from the spec AT THE PIN (git objects), replayed and compared like
      `validate`; the clone's working tree and later trunk commits do not count.
   2. A staged change under the vendored paths `parity/testdata/` or `parity/runner/` -> BLOCKED ("vendored"),
      unless the trailer is `Parity: sync <old>..<new>` (the commit `parity.py sync` makes). What a sync commit must
      satisfy (only `parity/`, vendored files equal the spec at the new pin, the pin descends) is slice C's
      (X4 19, 41) and is not pinned here. This runs before "nothing hit", because `parity/**` is exempt.
   3. An added file, or the new name of a rename, that is unclassified -> BLOCKED naming the path and "no area".
      (Modified unclassified files are left to `coverage` and the range check; not pinned here.)
   4. Only test and exempt paths: pass, unless the commit deletes a test file that holds a test an item names for
      this platform, removes the line declaring it (`func <method>`), or adds a skip marker (XCTSkip, .disabled(,
      withKnownIssue) to a file that holds it -> BLOCKED naming the item and the test. Test names are SwiftPM's
      `<Target>.<Type>/<method>`.
   5. Something hit and no `Parity:` trailer -> BLOCKED "no Parity trailer" and the areas hit.
   6. `Parity: n/a — <reason>` on any product path -> BLOCKED (step P0 refuses n/a; test 57).
   7. `Parity: PAR-n[, PAR-m]`: every id exists at the pin ("unknown item PAR-n"); every area hit is the area of some
      named item ("<id> does not cover <area>"); the item is not null ("null") or excepted ("excepted") for this
      platform; every other platform is shipped, excepted, null, or open with a due among its next releases (else
      BLOCKED naming that platform and "due").

Ambiguities settled here (reported to the orchestrator): coverage is per commit, the union of the named items'
areas, not X5's per-item `covers(it.area, hits)`; X4 test 6 ("open without due") is an open side whose due release
has already shipped, the only such state P0's ops can make.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import TOOL, clean_env, git  # noqa: E402

import parity  # noqa: E402

NAMED_TEST = "ZChatCoreTests.MoneyConsentTests/testRefusesWithoutConsent"
NAMED_FILE = "Packages/ZChatCore/Tests/ZChatCoreTests/MoneyConsentTests.swift"
OTHER_TEST_FILE = "Packages/ZChatCore/Tests/ZChatCoreTests/OtherTests.swift"
MONEY = "Packages/ZChatCore/Sources/Money/Consent.swift"
WIRE = "Packages/ZChatCore/Sources/Wire/Prefix.swift"
VIEW = "App/Views/ChatView.swift"
VM = "Packages/ZChatUI/Sources/ZChatUI/ChatViewModel.swift"
NA = "Parity: n/a — pure rename, no behaviour change"
WAIVER = "# waivers\n\n## WI-9 — test approval\nOwner, verbatim: \"Yes\"\n"

NAMED_SRC = """import XCTest
@testable import ZChatCore

final class MoneyConsentTests: XCTestCase {
    func testRefusesWithoutConsent() throws {
        XCTAssertTrue(true)
    }
}
"""
OTHER_SRC = """import XCTest

final class OtherTests: XCTestCase {
    func testSomethingElse() {
        XCTAssertEqual(1, 1)
    }
}
"""

IOS_EXEMPT = ["Tests/**", "Packages/*/Tests/**", "App/Tests/**", "tools/qa/**", "docs/**", "parity/**"]

_SHARED = {}


def run(env, spec, *args, expect=0):
    r = subprocess.run([sys.executable, "-I", str(TOOL), "--spec", str(spec), *args], env=env, capture_output=True,
                       text=True, timeout=60)
    if expect is not None and r.returncode != expect:
        raise AssertionError("parity.py %s: exit %d, expected %d\nstdout: %s\nstderr: %s"
                             % (" ".join(args), r.returncode, expect, r.stdout, r.stderr))
    return r


def ios_open(reg):
    return ["--due", "ios=1.0.0", "--register", "ios=" + reg]


def add(env, spec, want, kind, area, *extra):
    r = run(env, spec, "add", "--kind", kind, "--area", area, "--title", "A test behaviour", "--source", "W-236",
            *extra)
    if "ADDED %s" % want not in r.stdout:
        raise AssertionError("fixture: expected ADDED %s, got %s" % (want, r.stdout))


def build_spec(root):
    """The shared spec, built once with slice A's ops. Items:
    PAR-0001 money.consent  ios open, names NAMED_TEST; android open due 2.13.2; backend null
    PAR-0002 zmsg (wire)    ios open; android, backend null
    PAR-0003 ui.chat        ios null; android open due 2.13.2
    PAR-0004 ui.chat        ios open; android, backend null
    PAR-0005 money.consent  ios open; android open due 2.13.1, which then ships as the baseline (due passed)
    PAR-0006 money.consent  ios open; android shipped at the 2.13.1 baseline
    PAR-0007 money.consent  ios open but excepted (WI-9 to 1.0.0); android open due 2.13.2
    PAR-0008 money.consent  ios open; android excepted (WI-9 to 2.13.2)
    """
    env = clean_env(root)
    spec = Path(root) / "spec"
    spec.mkdir()
    git(spec, "init", "-q", "-b", "trunk", env=env)
    (spec / "README.md").write_text("spec\n")
    git(spec, "add", "README.md", env=env)
    git(spec, "commit", "-q", "-m", "chore: start", env=env)
    Path(env["ZCHAT_WAIVERS"]).write_text(WAIVER)
    run(env, spec, "init")
    for plat, ver, cls in [("android", "2.13.1", "outward"), ("android", "2.13.2", "outward"),
                           ("android", "2.13.3", "outward"), ("android", "2.14.0", "outward"),
                           ("ios", "0.1.0", "internal"), ("ios", "0.2.0", "internal"),
                           ("ios", "1.0.0", "outward"), ("ios", "1.1.0", "outward"),
                           ("backend", "2026.12", "outward")]:
        run(env, spec, "release", "add", "--platform", plat, "--version", ver, "--class", cls)
    for plat, pat, area, tier in [("ios", "Packages/ZChatCore/Sources/Money/**", "money.consent", "hard"),
                                  ("ios", "Packages/ZChatCore/Sources/Wire/**", "zmsg", "hard"),
                                  ("ios", "Packages/ZChatUI/Sources/**", "ui.vm", "mixed"),
                                  ("ios", "App/Views/**", "ui.chat", "local"),
                                  ("android", "ui-lib/src/main/**/money/**", "money.consent", "hard")]:
        run(env, spec, "area", "set", "--platform", plat, "--pattern", pat, "--area", area, "--tier", tier)
    for pat in IOS_EXEMPT:
        run(env, spec, "area", "exempt", "--platform", "ios", "--pattern", pat, "--reason",
            "tests, harness, docs or own checks")
    nb = ["--null", "backend"]
    add(env, spec, "PAR-0001", "behaviour", "money.consent", *ios_open("IOSP-3"), "--test", "ios=" + NAMED_TEST,
        "--due", "android=2.13.2", "--register", "android=ZC-2702", *nb)
    add(env, spec, "PAR-0002", "wire", "zmsg", *ios_open("IOSP-4"), "--case", "ios=zmsg/prefix.json#*",
        "--null", "android", *nb)
    add(env, spec, "PAR-0003", "behaviour", "ui.chat", "--null", "ios", "--due", "android=2.13.2", "--register",
        "android=ZC-2703", *nb)
    add(env, spec, "PAR-0004", "behaviour", "ui.chat", *ios_open("IOSP-5"), "--null", "android", *nb)
    add(env, spec, "PAR-0005", "behaviour", "money.consent", *ios_open("IOSP-6"), "--due", "android=2.13.1",
        "--register", "android=ZC-2704", *nb)
    run(env, spec, "baseline", "--platform", "android", "--version", "2.13.1")
    add(env, spec, "PAR-0006", "behaviour", "money.consent", *ios_open("IOSP-7"), "--due", "android=2.13.1",
        "--baseline-shipped", "android", "--test", "android=SomeTest", *nb)
    add(env, spec, "PAR-0007", "behaviour", "money.consent", *ios_open("IOSP-8"), "--due", "android=2.13.2",
        "--register", "android=ZC-2705", *nb)
    run(env, spec, "except", "--item", "PAR-0007", "--platform", "ios", "--waiver", "WI-9", "--last-release", "1.0.0")
    add(env, spec, "PAR-0008", "behaviour", "money.consent", *ios_open("IOSP-9"), "--due", "android=2.13.2",
        "--register", "android=ZC-2706", *nb)
    run(env, spec, "except", "--item", "PAR-0008", "--platform", "android", "--waiver", "WI-9",
        "--last-release", "2.13.2")
    run(env, spec, "validate")
    return spec, git(spec, "rev-parse", "HEAD", env=env)


def setUpModule():
    root = tempfile.mkdtemp(prefix="spec-shared-")
    _SHARED["root"] = root
    _SHARED["spec"], _SHARED["pin"] = build_spec(root)


def tearDownModule():
    shutil.rmtree(_SHARED.get("root", ""), ignore_errors=True)


class CommitCase(unittest.TestCase):
    """A fresh iOS tree per test, pinned at the shared spec's trunk head, every file classified."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ios-test-")
        self.env = clean_env(self.tmp)
        self.spec = _SHARED["spec"]
        self.pin = _SHARED["pin"]
        self.repo = Path(self.tmp) / "ios"
        self.repo.mkdir()
        self.g("init", "-q", "-b", "dev")
        for rel, text in {
            "parity/SPEC_PIN": self.pin + "\n",
            MONEY: "public let consentNeeded = true\n",
            WIRE: "public let prefix = \"ZMSG|\"\n",
            VIEW: "struct ChatView {}\n",
            VM: "final class ChatViewModel {}\n",
            NAMED_FILE: NAMED_SRC,
            OTHER_TEST_FILE: OTHER_SRC,
            "docs/notes.md": "notes\n",
            "parity/testdata/money/q.json": "{}\n",
            "parity/runner/swift/Runner.swift": "// runner\n",
            "tools/qa/check.py": "# harness\n",
        }.items():
            self.write(rel, text)
        self.g("add", "-A")
        self.g("commit", "-q", "-m", "chore: start")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the tree
    def g(self, *args):
        return git(self.repo, *args, env=self.env)

    def write(self, rel, text):
        f = self.repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def append(self, rel, text):
        self.write(rel, (self.repo / rel).read_text(encoding="utf-8") + text)

    def stage(self):
        self.g("add", "-A")

    def reset(self):
        self.g("reset", "-q", "--hard", "HEAD")
        self.g("clean", "-q", "-fd")

    def commit_raw(self, subject):
        """A commit made with plain git (no hooks in this tree), to set up history."""
        self.g("add", "-A")
        self.g("commit", "-q", "-m", subject)

    # -- the judge
    def check(self, message, expect, spec=None):
        mf = Path(self.tmp) / "COMMIT_EDITMSG"
        mf.write_text(message, encoding="utf-8")
        return run(self.env, spec or self.spec, "check-commit", "--platform", "ios", "--repo", str(self.repo),
                   "--msg-file", str(mf), expect=expect)

    def passes(self, message, spec=None):
        r = self.check(message, 0, spec)
        self.assertIn("PARITY PASS", r.stdout)
        self.assertNotIn("BLOCKED", r.stdout)
        return r

    def blocked(self, message, *says, not_says=(), spec=None):
        r = self.check(message, 1, spec)
        self.assertIn("BLOCKED: ", r.stdout)
        self.assertNotIn("PARITY PASS", r.stdout)
        for s in says:
            self.assertIn(s, r.stdout)
        for s in not_says:
            self.assertNotIn(s, r.stdout)
        return r


def msg(subject, trailer=None):
    out = subject + "\n\nWhy this change is made.\n"
    out += "\n" + (trailer + "\n" if trailer else "") + "Co-Authored-By: Test Author <author@test.invalid>\n"
    return out


NO_TRAILER = "no Parity trailer"


class Trailers(CommitCase):
    """X4 tests 1, 2, 3 and 5."""

    def test_1_hard_path_without_trailer_blocked(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.blocked(msg("feat(core): consent limit"), NO_TRAILER, "money.consent")

    def test_1_same_edit_with_an_open_item_passes(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.passes(msg("feat(core): consent limit", "Parity: PAR-0001"))

    def test_local_path_without_trailer_blocked_in_p0(self):
        self.append(VIEW, "struct Bubble {}\n")
        self.stage()
        self.blocked(msg("feat(ui): bubble"), NO_TRAILER, "ui.chat")
        self.passes(msg("feat(ui): bubble", "Parity: PAR-0004"))

    def test_commented_trailer_does_not_count(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        text = "feat(core): consent limit\n\nWhy.\n# Parity: PAR-0001\n"
        self.blocked(text, NO_TRAILER, "money.consent")

    def test_deleted_product_file_is_a_hit(self):
        self.g("rm", "-q", MONEY)
        self.blocked(msg("refactor(core): drop consent"), NO_TRAILER, "money.consent")
        self.passes(msg("refactor(core): drop consent", "Parity: PAR-0001"))

    def test_docs_and_harness_only_need_no_trailer(self):
        self.append("docs/notes.md", "more\n")
        self.append("tools/qa/check.py", "# more\n")
        self.stage()
        self.passes(msg("docs: notes"))

    def test_2_unknown_item_blocked(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.blocked(msg("feat(core): limit", "Parity: PAR-0099"), "unknown item PAR-0099", not_says=(NO_TRAILER,))

    def test_2_one_unknown_among_several_blocked(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.blocked(msg("feat(core): limit", "Parity: PAR-0001, PAR-0099"), "unknown item PAR-0099")

    def test_3_item_area_does_not_cover_the_path(self):
        self.append(VIEW, "struct Bubble {}\n")
        self.stage()
        self.blocked(msg("feat(ui): bubble", "Parity: PAR-0001"), "does not cover", "ui.chat",
                     not_says=(NO_TRAILER, "unknown item"))

    def test_3_a_rename_hits_both_names(self):
        self.g("mv", MONEY, "App/Views/Consent.swift")
        self.blocked(msg("refactor: move", "Parity: PAR-0004"), "does not cover", "money.consent")
        self.blocked(msg("refactor: move", "Parity: PAR-0001"), "does not cover", "ui.chat")
        self.passes(msg("refactor: move", "Parity: PAR-0001, PAR-0004"))

    def test_two_areas_need_both_covered(self):
        self.append(MONEY, "public let limit = 5\n")
        self.append(WIRE, "public let other = \"ZPAY|\"\n")
        self.stage()
        self.blocked(msg("feat(core): limit and prefix", "Parity: PAR-0001"), "does not cover", "zmsg")
        self.passes(msg("feat(core): limit and prefix", "Parity: PAR-0001, PAR-0002"))

    def test_5_other_platform_open_with_due_and_register_passes(self):
        # PAR-0001: android open, due 2.13.2 (its next release), ZC-2702.
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.passes(msg("feat(core): limit", "Parity: PAR-0001"))


class OtherPlatforms(CommitCase):
    """X4 tests 4 and 6, and the per-platform states of step 5 of annex A8 (exceptions: test 10's commit half)."""

    def setUp(self):
        super().setUp()
        self.append(MONEY, "public let limit = 5\n")
        self.stage()

    def test_4_other_platform_shipped_passes(self):
        self.passes(msg("feat(core): limit", "Parity: PAR-0006"))

    def test_other_platform_excepted_passes(self):
        self.passes(msg("feat(core): limit", "Parity: PAR-0008"))

    def test_6_other_platform_without_a_due_blocked(self):
        # PAR-0005's android side is open with due 2.13.1, which has shipped without it: no due release is left.
        self.blocked(msg("feat(core): limit", "Parity: PAR-0005"), "android", "due",
                     not_says=(NO_TRAILER, "does not cover", "unknown item"))

    def test_this_platform_excepted_blocked(self):
        self.blocked(msg("feat(core): limit", "Parity: PAR-0007"), "excepted", not_says=(NO_TRAILER,))

    def test_this_platform_null_blocked(self):
        self.reset()
        self.append(VIEW, "struct Bubble {}\n")
        self.stage()
        self.blocked(msg("feat(ui): bubble", "Parity: PAR-0003"), "null", not_says=(NO_TRAILER, "does not cover"))
        self.passes(msg("feat(ui): bubble", "Parity: PAR-0004"))


class NotApplicableInP0(CommitCase):
    """X4 test 57: in step P0 `Parity: n/a` is refused on every product path, whatever the tier and type."""

    def assert_na_refused(self, path, subject):
        self.append(path, "// reworded comment\n")
        self.stage()
        self.blocked(msg(subject, NA), "n/a", not_says=(NO_TRAILER, "unknown item", "does not cover"))

    def test_local(self):
        self.assert_na_refused(VIEW, "refactor(ui): tidy")

    def test_mixed(self):
        self.assert_na_refused(VM, "refactor(ui): tidy")

    def test_hard(self):
        self.assert_na_refused(MONEY, "style(core): tidy")

    def test_test_and_docs_only_commits_still_pass_with_na(self):
        self.append(OTHER_TEST_FILE, "// note\n")
        self.append("docs/notes.md", "more\n")
        self.stage()
        self.passes(msg("test(core): note", NA))


class TestOnly(CommitCase):
    """X4 test 15: a test-only commit passes, unless it deletes, renames or disables a named test, or edits a
    vendored file."""

    def test_editing_tests_passes(self):
        self.append(OTHER_TEST_FILE, "\nfinal class MoreTests: XCTestCase {\n    func testMore() {}\n}\n")
        self.stage()
        self.passes(msg("test(core): more"))

    def test_adding_a_test_next_to_a_named_one_passes(self):
        self.write(NAMED_FILE, NAMED_SRC.replace("    }\n}\n", "    }\n\n    func testAlsoThis() {}\n}\n"))
        self.stage()
        self.passes(msg("test(core): also this"))

    def test_deleting_an_unnamed_test_passes(self):
        self.g("rm", "-q", OTHER_TEST_FILE)
        self.passes(msg("test(core): drop other"))

    def test_deleting_a_named_test_blocked(self):
        self.g("rm", "-q", NAMED_FILE)
        self.blocked(msg("test(core): drop consent"), "PAR-0001", "testRefusesWithoutConsent")

    def test_renaming_a_named_test_blocked(self):
        self.write(NAMED_FILE, NAMED_SRC.replace("testRefusesWithoutConsent", "testRefusesWithoutConsentLater"))
        self.stage()
        self.blocked(msg("test(core): rename"), "PAR-0001", "testRefusesWithoutConsent")

    def test_disabling_a_named_test_blocked(self):
        self.write(NAMED_FILE, NAMED_SRC.replace("        XCTAssertTrue(true)\n",
                                                 "        throw XCTSkip(\"later\")\n        XCTAssertTrue(true)\n"))
        self.stage()
        self.blocked(msg("test(core): later"), "PAR-0001", "testRefusesWithoutConsent")

    def test_editing_a_vendored_vector_blocked(self):
        self.append(OTHER_TEST_FILE, "// note\n")
        self.write("parity/testdata/money/q.json", "{\"limit\": 6}\n")
        self.stage()
        self.blocked(msg("test(core): vectors"), "vendored", "parity/testdata/money/q.json")

    def test_editing_the_vendored_runner_blocked(self):
        self.append("parity/runner/swift/Runner.swift", "// weakened\n")
        self.stage()
        self.blocked(msg("test(core): runner"), "vendored", "parity/runner/swift/Runner.swift")


class Unclassified(CommitCase):
    """X4 tests 16 and 37: default deny. A file that matches no area and no exempt pattern blocks at commit; the
    `coverage` command finds it over the whole tree."""

    CASES = ["Packages/NewModule/Sources/NewModule/A.swift", "Packages/ZChatCore/Sources/Other/A.swift",
             "Extensions/Notify/NotificationService.swift", "App/Info.plist", "App/App.entitlements",
             "project.yml", "Package.resolved", "Config/Base.xcconfig", ".xcode-version",
             ".github/workflows/release.yml", "README.md", "Packages/ZChatCore/Package.swift"]

    def test_added_unclassified_file_blocked_even_with_a_good_trailer(self):
        for rel in self.CASES:
            with self.subTest(path=rel):
                self.reset()
                self.append(MONEY, "public let limit = 5\n")
                self.write(rel, "x\n")
                self.stage()
                self.blocked(msg("feat(core): limit", "Parity: PAR-0001"), rel, "no area")

    def test_the_same_commit_without_the_new_file_passes(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.passes(msg("feat(core): limit", "Parity: PAR-0001"))

    def test_added_unclassified_file_alone_blocked(self):
        self.write("Extensions/Notify/NotificationService.swift", "x\n")
        self.stage()
        self.blocked(msg("feat(push): service"), "Extensions/Notify/NotificationService.swift", "no area")

    def test_rename_into_an_unclassified_place_blocked(self):
        (self.repo / "Extensions").mkdir()
        self.g("mv", VIEW, "Extensions/ChatView.swift")
        self.blocked(msg("refactor(ui): move", "Parity: PAR-0004"), "Extensions/ChatView.swift", "no area")

    def test_coverage_of_a_classified_tree_passes(self):
        r = run(self.env, self.spec, "coverage", "--platform", "ios", "--repo", str(self.repo), expect=0)
        self.assertIn("COVERAGE OK", r.stdout)

    def test_coverage_names_every_unclassified_file(self):
        self.write("Extensions/Notify/NotificationService.swift", "x\n")
        self.write("project.yml", "x\n")
        self.commit_raw("chore: unclassified files")
        r = run(self.env, self.spec, "coverage", "--platform", "ios", "--repo", str(self.repo), expect=1)
        self.assertIn("COVERAGE FAIL", r.stdout)
        self.assertIn("Extensions/Notify/NotificationService.swift", r.stdout)
        self.assertIn("project.yml", r.stdout)


class SpecPin(CommitCase):
    """X4 test 20, and the fail-closed half of test 18 at commit: the registry is read at the staged pin."""

    def copy_spec(self):
        dst = Path(self.tmp) / "spec"
        shutil.copytree(self.spec, dst)
        return dst

    def test_a_tree_without_spec_pin_blocked(self):
        self.g("rm", "-q", "parity/SPEC_PIN")
        self.commit_raw("chore: no pin")
        self.append("docs/notes.md", "more\n")
        self.stage()
        self.blocked(msg("docs: notes"), "SPEC_PIN")

    def test_deleting_spec_pin_blocked(self):
        self.g("rm", "-q", "parity/SPEC_PIN")
        self.append("docs/notes.md", "more\n")
        self.stage()
        self.blocked(msg("docs: notes"), "SPEC_PIN")

    def test_a_pin_the_clone_does_not_hold_blocked(self):
        for bad in ["0" * 40, "trunk", ""]:
            with self.subTest(pin=bad):
                self.reset()
                self.write("parity/SPEC_PIN", bad + "\n")
                self.append("docs/notes.md", "more\n")
                self.stage()
                r = self.blocked(msg("docs: notes"))
                self.assertIn("pin", r.stdout.lower())

    def test_spec_clone_missing_blocked(self):
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.blocked(msg("feat(core): limit", "Parity: PAR-0001"), spec=Path(self.tmp) / "no-spec")

    def test_the_registry_is_read_at_the_pin(self):
        spec = self.copy_spec()
        add(self.env, spec, "PAR-0009", "behaviour", "money.consent", *ios_open("IOSP-10"), "--null", "android",
            "--null", "backend")
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.blocked(msg("feat(core): limit", "Parity: PAR-0009"), "unknown item PAR-0009", spec=spec)
        self.g("stash", "-q")
        self.write("parity/SPEC_PIN", git(spec, "rev-parse", "trunk", env=self.env) + "\n")
        self.commit_raw("chore(parity): sync")
        self.g("stash", "pop", "-q")
        self.stage()
        self.passes(msg("feat(core): limit", "Parity: PAR-0009"), spec=spec)

    def test_the_clone_working_tree_does_not_count(self):
        spec = self.copy_spec()
        (spec / "parity" / "PARITY.log").unlink()
        (spec / "parity" / "PARITY.json").write_text("{not json")
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.passes(msg("feat(core): limit", "Parity: PAR-0001"), spec=spec)

    def test_a_pin_whose_registry_does_not_replay_blocked(self):
        spec = self.copy_spec()
        f = spec / "parity" / "PARITY.json"
        f.write_text(f.read_text().replace("A test behaviour", "A softer behaviour", 1))
        git(spec, "commit", "-q", "-am", "hand edit", env=self.env)
        self.write("parity/SPEC_PIN", git(spec, "rev-parse", "trunk", env=self.env) + "\n")
        self.commit_raw("chore(parity): pin the hand edit")
        self.append(MONEY, "public let limit = 5\n")
        self.stage()
        self.blocked(msg("feat(core): limit", "Parity: PAR-0001"), spec=spec)


class Classify(unittest.TestCase):
    """The default-deny classification (plan D.4, annex A7) as a pure function of the replayed state; X4 test 37's
    named cases for Android and the backend are here, since P0's hooks are iOS only."""

    @classmethod
    def setUpClass(cls):
        state = parity.empty_state()
        entries = [("area-set", "ios", "Packages/ZChatCore/Sources/Money/**", "money.consent", "hard"),
                   ("area-set", "ios", "Packages/ZChatUI/Sources/**", "ui.vm", "mixed"),
                   ("area-set", "ios", "App/Views/**", "ui.chat", "local"),
                   ("area-set", "android", "ui-lib/src/main/**/money/**", "money.consent", "hard"),
                   ("area-set", "android", "app/src/main/**", "app", "mixed")]
        for op, plat, pat, area, tier in entries:
            state = parity.apply(state, {"op": op, "args": {"platform": plat, "pattern": pat, "area": area,
                                                            "tier": tier}})
        for plat, pat in [("ios", p) for p in IOS_EXEMPT] + [("android", "**/src/test/**"),
                                                             ("android", "docs/**")]:
            state = parity.apply(state, {"op": "area-exempt", "args": {"platform": plat, "pattern": pat,
                                                                       "reason": "tests, harness or docs"}})
        cls.state = state

    def c(self, plat, path):
        return tuple(parity.classify(self.state, plat, path))

    def test_tiers(self):
        self.assertEqual(self.c("ios", "Packages/ZChatCore/Sources/Money/Rule.swift"), ("hard", "money.consent"))
        self.assertEqual(self.c("ios", "Packages/ZChatUI/Sources/Chat/ChatViewModel.swift"), ("mixed", "ui.vm"))
        self.assertEqual(self.c("ios", "App/Views/Chat/ChatView.swift"), ("local", "ui.chat"))
        self.assertEqual(self.c("android", "ui-lib/src/main/java/z/money/Fee.kt"), ("hard", "money.consent"))

    def test_test_paths(self):
        self.assertEqual(self.c("ios", "Packages/ZChatCore/Tests/ZChatCoreTests/A.swift"),
                         ("test", "Packages/*/Tests/**"))
        self.assertEqual(self.c("ios", "App/Tests/A.swift"), ("test", "App/Tests/**"))
        self.assertEqual(self.c("ios", "Tests/E2E/A.swift"), ("test", "Tests/**"))
        self.assertEqual(self.c("ios", "tools/qa/tests/test_hooks.py"), ("test", "tools/qa/**"))
        self.assertEqual(self.c("android", "app/src/test/java/A.kt"), ("test", "**/src/test/**"))

    def test_other_exempt_paths(self):
        self.assertEqual(self.c("ios", "tools/qa/githooks/commit_msg.py"), ("exempt", "tools/qa/**"))
        self.assertEqual(self.c("ios", "docs/plan.md"), ("exempt", "docs/**"))
        self.assertEqual(self.c("ios", "parity/SPEC_PIN"), ("exempt", "parity/**"))

    def test_default_deny(self):
        for plat, path in [("ios", "Packages/NewModule/Sources/NewModule/A.swift"),
                           ("ios", "Packages/ZChatCore/Sources/Other/A.swift"),
                           ("ios", "Extensions/Notify/NotificationService.swift"), ("ios", "App/Info.plist"),
                           ("ios", "Package.resolved"), ("ios", "project.yml"), ("ios", "Config/Base.xcconfig"),
                           ("ios", ".github/workflows/release.yml"), ("ios", "README.md"),
                           ("android", "app/src/release/java/A.kt"), ("android", "newmodule/src/main/A.kt"),
                           ("android", "buildSrc/src/main/kotlin/A.kt"), ("android", "app/proguard-rules.pro"),
                           ("android", "app/build.gradle.kts"), ("backend", "prisma/schema.prisma")]:
            with self.subTest(platform=plat, path=path):
                self.assertEqual(self.c(plat, path), (None, None))

    def test_only_the_platforms_own_lists_count(self):
        self.assertEqual(self.c("android", "App/Views/ChatView.swift"), (None, None))
        self.assertEqual(self.c("backend", "Tests/A.swift"), (None, None))

    def test_a_test_path_on_no_exempt_pattern_is_unclassified(self):
        self.assertEqual(self.c("android", "app/src/androidTest/java/A.kt"), (None, None))


if __name__ == "__main__":
    unittest.main()
