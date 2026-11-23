"""Release steps finish in a disposable repository after a failed Git step."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import cast, override

from scripts.lib.rerun_test_support import SCRIPTS, ScriptRepositoryTestCase


class ReleaseRerunTests(ScriptRepositoryTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.environment["TEST_RELEASE_STATE"] = str(self.root)
        _ = self.stub("cargo", '''import json, os, sys
from pathlib import Path
state = Path(os.environ["TEST_RELEASE_STATE"])
with (state / "cargo.calls").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "metadata":
    print(json.dumps({"packages": [{"name": "other", "version": "9.0.0"},
                                   {"name": "fixture", "version": "1.2.3"}]}))
''')
        _ = self.stub("curl", '''import json, os, sys
from pathlib import Path
state = Path(os.environ["TEST_RELEASE_STATE"])
with (state / "curl.calls").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
response = state / ("crates-" + sys.argv[-1].rsplit("/", 1)[-1] + "-response")
print((response if response.exists() else state / "crates-response").read_text())
''')
        _ = self.write(self.root / "crates-response", '{"errors": [{"detail": "not found"}]}')
        _ = self.stub("gh", '''import json, os, sys
from pathlib import Path
state = Path(os.environ["TEST_RELEASE_STATE"])
args = sys.argv[1:]
with (state / "gh.calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["release", "view"]:
    raise SystemExit(0 if (state / "released").exists() else 1)
if args[0] == "api":
    if (state / "lookup-fails").exists():
        print("HTTP 403: Resource not accessible", file=sys.stderr)
        raise SystemExit(1)
    if (state / "target-tag").exists():
        raise SystemExit(0)
    print("HTTP 404: Not Found", file=sys.stderr)
    raise SystemExit(1)
if args[:2] == ["release", "create"]:
    if "--verify-tag" not in args or not (state / "target-tag").exists():
        raise SystemExit(1)
    if (state / "create-fails").exists():
        print("HTTP 403: Resource not accessible", file=sys.stderr)
        raise SystemExit(1)
    (state / "released").touch()
else:
    raise SystemExit("unexpected gh command")
''')

    def calls(self, command: str) -> list[list[str]]:
        path = self.root / f"{command}.calls"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_an_absent_post_publish_hook_succeeds_without_running_a_hook(self) -> None:
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        for options in ((), ("--dry-run",)):
            with self.subTest(options=options):
                result = self.script("release/post_publish_hook.sh", "fixture", "1.2.3", sha, *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "post-publish hook: absent\n")
                self.assertEqual(result.stderr, "")

    def test_the_post_publish_hook_receives_the_values_and_runs_from_the_repository_root(self) -> None:
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(self.repo / ".claude/config/post_publish.sh",
                       'printf "%s\\n" "$CRATE" "$VERSION" "$SHA" "$PWD" > .git/hook-values\n'
                       + 'echo "hook output"\necho "hook detail" >&2\n')
        subfolder = self.repo / "nested folder"
        subfolder.mkdir()
        result = self.run_in_repo("bash", "-c", 'cd "$1" && exec bash "$2" "$3" "$4" "$5"',
                                  "hook-fixture", str(subfolder), str(SCRIPTS / "release/post_publish_hook.sh"),
                                  "fixture", "1.2.3", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / ".git/hook-values").read_text().splitlines(),
                         ["fixture", "1.2.3", sha, str(self.repo)])
        self.assertEqual(result.stdout, "hook output\npost-publish hook: done\n")
        self.assertEqual(result.stderr, "hook detail\n")

    def test_a_failed_post_publish_hook_reports_its_exit_and_a_quoted_rerun_command(self) -> None:
        self.repo: Path = self.repo.rename(self.root / "repo with spaces")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        crate = "fixture odd;$value"
        _ = self.write(self.repo / ".claude/config/post_publish.sh",
                       'printf "%s\\n" "$CRATE" "$VERSION" "$SHA" "$PWD" >> .git/hook-values\nexit 23\n')
        result = self.script("release/post_publish_hook.sh", crate, "1.2.3", sha)
        self.assertEqual(result.returncode, 1)
        lines = result.stderr.splitlines()
        self.assertEqual(lines[0], "post-publish hook: failed (exit 23)")
        self.assertTrue(lines[1].startswith("rerun: cd "))
        rerun = self.run_in_repo("bash", "-c", lines[1].removeprefix("rerun: "))
        self.assertEqual(rerun.returncode, 23, rerun.stderr)
        self.assertEqual((self.repo / ".git/hook-values").read_text().splitlines(),
                         [crate, "1.2.3", sha, str(self.repo)] * 2)

    def test_post_publish_dry_run_reports_the_values_without_running_the_hook(self) -> None:
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(self.repo / ".claude/config/post_publish.sh", "touch hook-ran\n")
        result = self.script("release/post_publish_hook.sh", "fixture", "1.2.3", sha, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         f"[DRY-RUN] Would run post-publish hook: CRATE=fixture VERSION=1.2.3 SHA={sha}\n")
        self.assertFalse((self.repo / "hook-ran").exists())

    def test_post_publish_refuses_invalid_arguments_before_running_the_hook(self) -> None:
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        blob = self.git("hash-object", "seed.txt").stdout.strip()
        _ = self.write(self.repo / ".claude/config/post_publish.sh", "touch hook-ran\n")
        for arguments, message in (
            (("fixture", "1.2.3"), "Usage:"),
            (("fixture", "1.2.3", sha, "--unknown"), "Usage:"),
            (("fixture", "1.2.3", sha, "--dry-run", "extra"), "Usage:"),
            (("fixture", "1.2.3", ""), "Usage:"),
            (("fixture", "v1.2.3", sha), "ERROR: version must have no leading v"),
            (("fixture", "1.2.3", sha[:12]), "ERROR: sha must name a full 40-character commit id"),
            (("fixture", "1.2.3", "0" * 40), "ERROR: sha must name a full 40-character commit id"),
            (("fixture", "1.2.3", blob), "ERROR: sha must name a full 40-character commit id"),
        ):
            with self.subTest(arguments=arguments):
                result = self.script("release/post_publish_hook.sh", *arguments)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.repo / "hook-ran").exists())

    def test_a_new_release_branch_starts_at_head(self) -> None:
        head = self.git("rev-parse", "HEAD").stdout
        for options, branch in (((), "release-1.2.3"), (("--package", "fixture"), "release-fixture-1.2.3")):
            with self.subTest(branch=branch):
                result = self.script("release/create_release_branch.sh", "1.2.3", *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.git("branch", "--show-current").stdout.strip(), branch)
                self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)

    def test_an_existing_release_branch_at_head_is_reused(self) -> None:
        for options, branch in (((), "release-1.2.3"), (("--package", "fixture"), "release-fixture-1.2.3")):
            with self.subTest(branch=branch):
                _ = self.git("switch", "main")
                _ = self.git("branch", branch)
                for attempt in range(2):
                    with self.subTest(attempt=attempt):
                        result = self.script("release/create_release_branch.sh", "1.2.3", *options)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn(f"reusing release branch {branch}", result.stdout)
                        self.assertEqual(self.git("branch", "--show-current").stdout.strip(), branch)

    def test_a_release_branch_at_another_commit_is_refused(self) -> None:
        branch = "release-1.2.3"
        _ = self.git("branch", branch)
        original = self.git("rev-parse", branch).stdout.strip()
        _ = self.write(self.repo / "seed.txt", "next\n")
        _ = self.git("commit", "-am", "next")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        for options in ((), ("--dry-run",)):
            with self.subTest(options=options):
                result = self.script("release/create_release_branch.sh", "1.2.3", *options)
                self.assertEqual(result.returncode, 1)
                self.assertIn(original, result.stderr)
                self.assertIn(head, result.stderr)
                self.assertEqual(self.git("rev-parse", branch).stdout.strip(), original)
                self.assertEqual(self.git("branch", "--show-current").stdout.strip(), "main")
                if options:
                    self.assertIn("Would refuse release branch", result.stdout)

    def test_branch_dry_run_reports_creation_or_reuse_without_checkout(self) -> None:
        branch = "release-1.2.3"
        for exists in (False, True):
            with self.subTest(exists=exists):
                if exists:
                    _ = self.git("branch", branch)
                before = self.git("show-ref").stdout
                result = self.script("release/create_release_branch.sh", "1.2.3", "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Would reuse release branch" if exists else "Would create release branch", result.stdout)
                self.assertEqual(self.git("show-ref").stdout, before)
                self.assertEqual(self.git("branch", "--show-current").stdout.strip(), "main")

    def test_an_already_published_version_skips_both_publish_commands(self) -> None:
        _ = self.write(self.root / "crates-response", '{"version": {"num": "1.2.3", "yanked": false}}')
        for options in ((), ("--dry-run", "--allow-dirty")):
            with self.subTest(options=options):
                result = self.script("release/publish_crate.sh", "fixture", *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("already published: fixture 1.2.3", result.stdout)
        self.assertEqual(self.calls("cargo"), [["metadata", "--no-deps", "--format-version", "1"]] * 2)
        self.assertTrue(all(call[-1] == "https://crates.io/api/v1/crates/fixture/1.2.3" for call in self.calls("curl")))
        self.assertEqual(len(self.calls("curl")), 2)

    def test_resume_skips_published_crates_and_checks_each_remaining_crate(self) -> None:
        _ = self.write(self.root / "crates-fixture-response",
                       '{"versions": [{"num": "1.2.3"}], "crate": {"max_version": "9.9.9"}}')
        other = self.write(self.root / "crates-other-response",
                           '{"versions": [{"num": "1.2.2"}], "crate": {"max_version": "1.2.2"}}')
        result = self.script("release/validate_version.sh", "1.2.3", "--resume", "fixture", "other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  fixture: 1.2.3 already published (resume)", result.stdout)
        self.assertIn("OK — 1.2.3 is valid for other", result.stdout)
        self.assertEqual([call[-1] for call in self.calls("curl")],
                         ["https://crates.io/api/v1/crates/fixture", "https://crates.io/api/v1/crates/other"])
        _ = other.write_text('{"versions": [{"num": "1.2.0"}], "crate": {"max_version": "1.2.0"}}')
        gap = self.script("release/validate_version.sh", "1.2.3", "--resume", "fixture", "other")
        self.assertEqual(gap.returncode, 1)
        self.assertIn("fixture: 1.2.3 already published (resume)", gap.stdout)
        self.assertIn("other version gap", gap.stderr)

    def test_a_published_version_is_refused_without_resume(self) -> None:
        _ = self.write(self.root / "crates-response",
                       '{"versions": [{"num": "1.2.3"}], "crate": {"max_version": "1.2.3"}}')
        result = self.script("release/validate_version.sh", "1.2.3", "fixture")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: fixture version 1.2.3 is already published on crates.io", result.stderr)

    def test_validation_refuses_unknown_flags_and_invalid_versions_before_lookup(self) -> None:
        for version, flag, message in (("1.2.3", "--unknown", "ERROR: unknown flag: --unknown"),
                                       ("invalid", "--resume", "ERROR: Invalid version format: invalid")):
            with self.subTest(version=version, flag=flag):
                result = self.script("release/validate_version.sh", version, flag, "fixture")
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.calls("curl"), [])

    def test_an_unpublished_version_runs_the_dry_run_then_publish(self) -> None:
        result = self.script("release/publish_crate.sh", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("cargo"), [["metadata", "--no-deps", "--format-version", "1"],
                                               ["publish", "--package", "fixture", "--dry-run"],
                                               ["publish", "--package", "fixture"]])

    def test_publish_dry_run_does_not_publish_and_allow_dirty_requires_dry_run(self) -> None:
        refused = self.script("release/publish_crate.sh", "fixture", "--allow-dirty")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--allow-dirty is only valid with --dry-run", refused.stderr)
        self.assertEqual(self.calls("cargo"), [])
        result = self.script("release/publish_crate.sh", "fixture", "--dry-run", "--allow-dirty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("cargo"), [["metadata", "--no-deps", "--format-version", "1"],
                                               ["publish", "--package", "fixture", "--dry-run", "--allow-dirty"]])

    def test_an_existing_github_release_skips_creation_on_the_target_repository(self) -> None:
        _ = self.write(self.root / "released", "exists\n")
        result = self.script("release/create_github_release.sh", "1.2.3", "public/project", "Project", "missing.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already released: v1.2.3 on public/project", result.stdout)
        self.assertEqual(self.calls("gh"), [["release", "view", "v1.2.3", "--repo", "public/project"]])

    def test_a_new_github_release_checks_the_tag_on_the_target_repository(self) -> None:
        notes = self.write(self.repo / "notes.md", "release notes\n")
        _ = self.write(self.root / "target-tag", "exists\n")
        for version, options, tag, title in (("1.2.3", (), "v1.2.3", "Project v1.2.3"),
                                             ("2.0.0-rc.1", ("--package", "fixture"),
                                              "fixture-v2.0.0-rc.1", "fixture v2.0.0-rc.1")):
            with self.subTest(version=version):
                (self.root / "released").unlink(missing_ok=True)
                result = self.script("release/create_github_release.sh", version, "public/project", "Project", str(notes), *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls("gh")[-3], ["release", "view", tag, "--repo", "public/project"])
                self.assertEqual(self.calls("gh")[-2], ["api", f"repos/public/project/git/ref/tags/{tag}"])
                expected = ["release", "create", tag, "--repo", "public/project", "--title", title,
                            "--notes-file", str(notes), "--verify-tag"]
                self.assertEqual(self.calls("gh")[-1], expected + (["--prerelease"] if options else []))

    def test_a_missing_target_tag_refuses_the_github_release(self) -> None:
        notes = self.write(self.repo / "notes.md", "release notes\n")
        _ = self.git("tag", "v1.2.3")
        result = self.script("release/create_github_release.sh", "1.2.3", "public/project", "Project", str(notes))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: tag v1.2.3 is not on public/project; push it there first", result.stderr)
        self.assertFalse((self.root / "released").exists())
        self.assertEqual(self.calls("gh"), [["release", "view", "v1.2.3", "--repo", "public/project"],
                                           ["api", "repos/public/project/git/ref/tags/v1.2.3"]])

    def test_a_failed_tag_lookup_reports_the_lookup_error_without_creating_a_release(self) -> None:
        notes = self.write(self.repo / "notes.md", "release notes\n")
        _ = self.write(self.root / "lookup-fails", "fail\n")
        result = self.script("release/create_github_release.sh", "1.2.3", "public/project", "Project", str(notes))
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 403: Resource not accessible", result.stderr)
        self.assertIn("ERROR: could not check tag v1.2.3 on public/project", result.stderr)
        self.assertNotIn("tag v1.2.3 is not on", result.stderr)
        self.assertFalse((self.root / "released").exists())
        self.assertEqual(self.calls("gh"), [["release", "view", "v1.2.3", "--repo", "public/project"],
                                           ["api", "repos/public/project/git/ref/tags/v1.2.3"]])

    def test_a_create_failure_with_a_present_tag_reports_the_create_error(self) -> None:
        notes = self.write(self.repo / "notes.md", "release notes\n")
        _ = self.write(self.root / "target-tag", "exists\n")
        _ = self.write(self.root / "create-fails", "fail\n")
        result = self.script("release/create_github_release.sh", "1.2.3", "public/project", "Project", str(notes))
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 403: Resource not accessible", result.stderr)
        self.assertIn("ERROR: gh release create failed for v1.2.3 on public/project", result.stderr)
        self.assertNotIn("tag v1.2.3 is not on", result.stderr)
        self.assertFalse((self.root / "released").exists())
        self.assertEqual(self.calls("gh"), [["release", "view", "v1.2.3", "--repo", "public/project"],
                                           ["api", "repos/public/project/git/ref/tags/v1.2.3"],
                                           ["release", "create", "v1.2.3", "--repo", "public/project",
                                            "--title", "Project v1.2.3", "--notes-file", str(notes), "--verify-tag"]])

    def test_github_dry_run_reads_the_target_without_creating_a_release(self) -> None:
        notes = self.write(self.repo / "notes.md", "release notes\n")
        result = self.script("release/create_github_release.sh", "1.2.3", "public/project", "Project", str(notes), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Repo: public/project", result.stdout)
        self.assertEqual(self.calls("gh"), [["release", "view", "v1.2.3", "--repo", "public/project"]])

    def test_a_created_release_tag_finishes_both_remote_pushes_on_rerun(self) -> None:
        expected_tags: list[str] = []
        for version, failed_push in (("1.0", ("push", "-u")), ("1.1", ("push", "origin", "v1.1"))):
            with self.subTest(failed_push=failed_push):
                _ = self.git("switch", "-C", f"release-{version}")
                self.fail_git(*failed_push)
                stopped = self.script("release/push_release.sh", version)
                self.assertNotEqual(stopped.returncode, 0)
                self.assertEqual(self.git("rev-parse", f"v{version}").stdout, self.git("rev-parse", "HEAD").stdout)
                self.assertEqual(self.git("ls-remote", "origin", f"refs/tags/v{version}").stdout, "")
                finished = self.script("release/push_release.sh", version)
                self.assertEqual(finished.returncode, 0, finished.stderr)
                refs = self.git("ls-remote", "origin", f"refs/heads/release-{version}", f"refs/tags/v{version}").stdout
                self.assertEqual(len(refs.splitlines()), 2)
                expected_tags.append(f"v{version}")
                self.assertEqual(self.git("tag").stdout.splitlines(), expected_tags)

    def test_a_release_tag_on_another_commit_is_refused(self) -> None:
        _ = self.git("tag", "v1.0")
        _ = self.git("switch", "-c", "release-1.0")
        _ = self.write(self.repo / "seed.txt", "changed\n")
        _ = self.git("commit", "-am", "next")
        result = self.script("release/push_release.sh", "1.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another commit", result.stderr)

    def test_restoring_unreleased_finishes_a_failed_commit_or_push_without_duplicate_sections(self) -> None:
        _ = self.write(self.repo / ".claude/config/post_push.sh",
                       'printf "%s\\n" "$LANDED_SHA" >> .git/hook-runs\n')
        changelog = self.write(self.repo / "CHANGELOG.md", f"## [1.0] - {date.today()}\n\nrelease\n")
        _ = self.git("add", "CHANGELOG.md")
        _ = self.git("commit", "-m", "release")
        self.fail_git("commit")
        stopped = self.script("release/restore_unreleased.sh", "1.0", "CHANGELOG.md")
        self.assertNotEqual(stopped.returncode, 0)
        self.fail_git("-C", str(self.repo), "push")
        stopped = self.script("release/restore_unreleased.sh", "1.0", "CHANGELOG.md")
        self.assertNotEqual(stopped.returncode, 0)
        committed = self.git("rev-parse", "HEAD").stdout
        finished = self.script("release/restore_unreleased.sh", "1.0", "CHANGELOG.md")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(changelog.read_text().count("## [Unreleased]"), 1)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, committed)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout, committed)
        self.assertIn("post-push hook: done", finished.stdout)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [committed.strip()])

    def test_finalizing_multiple_changelogs_resumes_after_the_first_backup_is_removed(self) -> None:
        first = self.write(self.repo / "first.md", "## [Unreleased]\n\nfirst\n")
        second = self.write(self.repo / "second.md", "## [Unreleased]\n\nsecond\n")
        self.stop_command("rm", target="first.md.portbak", after=True)
        stopped = self.script("release/finalize_changelogs.sh", "1.0", "first.md", "second.md")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertNotIn("Unreleased", first.read_text())
        self.assertIn("Unreleased", second.read_text())
        finished = self.script("release/finalize_changelogs.sh", "1.0", "first.md", "second.md")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        for path in (first, second):
            self.assertEqual(path.read_text().count("## [1.0]"), 1)
            self.assertNotIn("Unreleased", path.read_text())
