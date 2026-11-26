#!/usr/bin/env python3
"""Count production CI points and verify their runs."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast


from scripts.buildlog.repository import remote_identity
from scripts.copies import copies
from scripts.lib.process_failure import failure_detail
from scripts.production import promotion
from scripts.production.checkpoint_gate import CiRecord as CiRecord, InvalidCiRecord, NoCiRecord, Stop, read_ci_record
from scripts.production.production_doc import Production, Refusal, read_production
from scripts.production.production_names import CARGO_LINER_PRODUCTION
from scripts.production.merge_checkpoint import git, live_file_refusal, merged_checkpoints, merge_lock


# These job names come from cargo-liner's CI workflow. The change filters
# control only the jobs in CARGO_LINER_CONDITIONAL_JOBS.
CARGO_LINER_REQUIRED_JOBS = frozenset({"Detect Changes", "Format Check", "Taplo Check", "Clippy", "Mend Check"})
CARGO_LINER_CONDITIONAL_JOBS = frozenset({"Build Check", "Test Suite", "cargo-mend Build Check", "cargo-mend Test Suite",
                                         "Stable Without Bootstrap", "macOS cargo-tile", "Windows Build Check"})

REQUIRED_CI_JOB = "Test Suite"


class CiDue(NamedTuple):
    pass


class CiNotDue(NamedTuple):
    merges_since: int


class CiNotUsed(NamedTuple):
    pass


class CiRun(NamedTuple):
    repo: str
    run_id: str
    tip: str


class CiPushRemote(NamedTuple):
    url: str
    repo: str


class PushStartsCi(NamedTuple):
    workflow: str


class DispatchStartsCi(NamedTuple):
    workflow: str


class NoCiTrigger(NamedTuple):
    workflow: str


class PassedValidation(NamedTuple):
    input_tip: str
    validated_commit: str


class NoReusableValidation(NamedTuple):
    pass


class PointFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.detail: str = detail


def report(step: str, state: str, detail: str) -> None:
    print(f"{step}: {state} — {detail.replace(chr(10), '; ')}", flush=True)


def command(step: str, *argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise PointFailure(step, str(error)) from error


def required(result: subprocess.CompletedProcess[str], step: str) -> str:
    if result.returncode:
        raise PointFailure(step, failure_detail(result))
    return result.stdout.strip()


def branch_tip(production: Production) -> str:
    return required(git(production.checkout, "rev-parse", f"refs/heads/{production.merge_branch}"), "ci collect")


def ci_push_remote(checkout: Path) -> CiPushRemote:
    url = required(git(checkout, "remote", "get-url", "--push", "--all", "origin"), "ci start")
    if not url or "\n" in url:
        raise PointFailure("ci start", "origin must have one push URL")
    identity = remote_identity(url, str(checkout))
    match = re.fullmatch(r"https://([a-zA-Z0-9.-]+)/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", identity)
    if match is None:
        raise PointFailure("ci start", "origin push URL must name a GitHub repository")
    host, repo = match.groups()
    if host == "ssh.github.com":
        host = "github.com"
    return CiPushRemote(url, f"{host}/{repo}")


def ci_record(state_dir: Path) -> CiRecord | NoCiRecord:
    try:
        return read_ci_record(state_dir)
    except InvalidCiRecord as error:
        raise PointFailure("due", str(error)) from error


def due(production: Production, state_dir: Path, no_ci: bool) -> CiDue | CiNotDue | CiNotUsed:
    if no_ci:
        return CiNotUsed()
    count = len(merged_checkpoints(production))
    record = ci_record(state_dir)
    since = count - (record.merges_collected if isinstance(record, CiRecord) else 0)
    return CiDue() if since >= 5 or since < 0 else CiNotDue(since)


def write_json(path: Path, fields: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(fields) + "\n", encoding="utf-8")
    _ = temporary.replace(path)


def read_run(state_dir: Path) -> CiRun:
    path = state_dir / "ci_run.json"
    if not path.exists():
        raise PointFailure("ci collect", "no started CI point; run ci start")
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PointFailure("ci collect", f"invalid {path}")
    fields = cast(dict[str, object], raw)
    repo, run_id, tip = fields.get("repo"), fields.get("run_id"), fields.get("tip")
    if not all(isinstance(value, str) and value for value in (repo, run_id, tip)):
        raise PointFailure("ci collect", f"invalid {path}")
    return CiRun(cast(str, repo), cast(str, run_id), cast(str, tip))


def handoff(output: str) -> CiRun:
    block = output.rpartition("=== CI HANDOFF TO AGENT ===")[2]
    fields = dict(re.findall(r"(?m)^(repo|sha|run_id):\s*(\S+)", block))
    if any(key not in fields for key in ("repo", "sha", "run_id")) or fields["run_id"] == "none":
        raise PointFailure("ci start", "validation did not provide a CI run; rerun ci start")
    return CiRun(fields["repo"], fields["run_id"], fields["sha"])


def branch_matches(pattern: str, branch: str) -> bool:
    """Match the complete branch name with GitHub's filter syntax."""
    tokens = [match.group() for match in re.finditer(r"\\.|\*\*/|\*\*|\*|\[[a-zA-Z0-9-]+\]|.", pattern)]
    parts: list[str] = []
    for index, token in enumerate(tokens):
        if token == "**/":
            parts.append("(?:.*/)?")
        elif token == "**":
            parts.append(".*")
        elif token == "*":
            parts.append("[^/]*")
        elif token in ("?", "+") and index and tokens[index - 1] not in ("*", "**", "**/", "?", "+"):
            parts[-1] = f"(?:{parts[-1]}){token}"
        elif token.startswith("[") and len(token) > 1:
            parts.append(token)
        else:
            parts.append(re.escape(token.removeprefix("\\") if len(token) > 1 else token))
    return re.fullmatch("".join(parts), branch) is not None


def ci_workflow_events(checkout: Path, tip: str, workflow: str) -> dict[str, object]:
    source = git(checkout, "show", f"{tip}:{workflow}")
    _ = required(source, "ci start")
    # BaseLoader keeps the `on` key as text.
    parse = ("import json, sys, yaml; "
             "print(json.dumps(yaml.load(sys.stdin, Loader=yaml.BaseLoader)))")
    result = subprocess.run(["uv", "--no-config", "run", "--no-project", "--with", "PyYAML",
                             "--python", sys.executable, "--", "python", "-c", parse],
                            input=source.stdout, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PointFailure("ci start", failure_detail(result, lines=1))
    document = cast(object, json.loads(result.stdout))
    if not isinstance(document, dict):
        return {}
    fields = cast(dict[str, object], document)
    jobs = fields.get("jobs")
    if not isinstance(jobs, dict) or not any(
        isinstance(job, dict) and cast(dict[str, object], job).get("name", job_id) == REQUIRED_CI_JOB
        for job_id, job in cast(dict[str, object], jobs).items()
    ):
        return {}
    events = fields.get("on")
    if isinstance(events, dict):
        return cast(dict[str, object], events)
    if isinstance(events, str):
        return {events: ""}
    if isinstance(events, list):
        return {event: "" for event in cast(list[object], events) if isinstance(event, str)}
    return {}


def push_covers_branch(filters: object, branch: str) -> bool:
    if not isinstance(filters, dict):
        return filters in ("", "null", "Null", "NULL", "~")
    fields = cast(dict[str, object], filters)
    if "paths" in fields or "paths-ignore" in fields:
        return False
    for key in ("branches", "branches-ignore"):
        if key not in fields:
            continue
        patterns = fields[key]
        if not isinstance(patterns, list):
            raise ValueError(f"{key} must be a list of patterns")
        included = key == "branches-ignore"
        for pattern in cast(list[object], patterns):
            if not isinstance(pattern, str):
                raise ValueError(f"{key} must contain only text patterns")
            negative = key == "branches" and pattern.startswith("!")
            if branch_matches(pattern[1:] if negative else pattern, branch):
                included = key == "branches" and not negative
        return included
    return "tags" not in fields and "tags-ignore" not in fields


def ci_trigger(production: Production, tip: str, remote: CiPushRemote) -> PushStartsCi | DispatchStartsCi | NoCiTrigger:
    try:
        workflows = promotion.ci_workflow_paths(production.checkout, tip)
    except Stop as error:
        raise PointFailure("ci start", error.detail) from error
    candidates: dict[str, bool] = {}
    for workflow in workflows:
        try:
            events = ci_workflow_events(production.checkout, tip, workflow)
            covers_push = "push" in events and push_covers_branch(events["push"], production.merge_branch)
        except (PointFailure, ValueError, re.error) as error:
            print(f"ci start: cannot use {workflow} — {' '.join(str(error).splitlines())}", file=sys.stderr)
            continue
        if covers_push or "workflow_dispatch" in events:
            candidates[workflow] = covers_push
    if not candidates:
        return NoCiTrigger(", ".join(workflows) or f"{promotion.CI_WORKFLOW_DIRECTORY}/ci.yml")
    host, _, repo = remote.repo.partition("/")
    rows = required(command("ci start", "gh", "api", "--hostname", host, "--paginate",
                            f"repos/{repo}/actions/workflows?per_page=100", "--jq",
                            ".workflows[] | [.path, .state] | @tsv",
                            cwd=production.checkout), "ci start").splitlines()
    registrations: dict[str, str] = {}
    for row in rows:
        path, state = row.split("\t", 1)
        registrations[path] = state
    push_workflows = [path for path, covers_push in candidates.items()
                      if covers_push and registrations.get(path, "active") == "active"]
    eligible = push_workflows or [path for path, covers_push in candidates.items()
                                  if not covers_push and registrations.get(path) == "active"]
    if not eligible:
        inactive_push = [path for path, covers_push in candidates.items() if covers_push and path in registrations]
        if inactive_push:
            raise PointFailure("ci start", f"inactive push CI workflow registration for {', '.join(inactive_push)}")
        raise PointFailure("ci start", f"no active CI workflow registration for {', '.join(candidates)}")
    if len(eligible) != 1:
        event = "push" if push_workflows else "dispatch"
        names = ", ".join(candidates)
        raise PointFailure(
            "ci start",
            f"multiple {event} workflows can start CI for {production.merge_branch}. Candidates are {names}",
        )
    workflow, = eligible
    name = Path(workflow).name
    return PushStartsCi(name) if push_workflows else DispatchStartsCi(name)


def require_ci_trigger(production: Production, tip: str, remote: CiPushRemote) -> PushStartsCi | DispatchStartsCi:
    trigger = ci_trigger(production, tip, remote)
    if isinstance(trigger, NoCiTrigger):
        raise PointFailure("ci start", f"{trigger.workflow}: no push trigger for {production.merge_branch} and no workflow_dispatch")
    return trigger


def reusable_validation(production: Production, record: Path, tip: str) -> PassedValidation | NoReusableValidation:
    try:
        raw = cast(object, json.loads(record.read_text(encoding="utf-8")))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return NoReusableValidation()
    if not isinstance(raw, dict):
        return NoReusableValidation()
    fields = cast(dict[str, object], raw)
    input_tip = fields.get("input_tip")
    validated = fields.get("validated_commit")
    if not isinstance(input_tip, str) or not isinstance(validated, str) or tip not in (input_tip, validated):
        return NoReusableValidation()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", validated):
        return NoReusableValidation()
    commit = git(production.checkout, "cat-file", "-t", validated)
    if commit.returncode or commit.stdout.strip() != "commit":
        return NoReusableValidation()
    return PassedValidation(input_tip, validated)


def validate_commit(production: Production, tip: str, script: Path, validation_log: Path, push_origin: str) -> str:
    checkpoints = merged_checkpoints(production)
    checkpoint = (f"{checkpoints[-1].unit} phase {checkpoints[-1].phase}" if checkpoints else "production wrap")
    # The validation wrapper is OtherCommand and performs several validation operations.
    with copies.standing(str(production.owner.id), production.checkout, copies.CommittedSource(tip), step="ci") as checkout:
        source_url = required(git(checkout, "config", "--local", "--get", "remote.origin.url"), "ci start")
        settings = [(f"url.{push_origin}.pushInsteadOf", source_url)]
        for key in ("user.name", "user.email"):
            identity = git(production.checkout, "config", "--get", key)
            if identity.returncode == 0:
                settings.append((key, identity.stdout.strip()))
        # Git's command environment reaches validation's child commands without
        # changing the standing checkout's persistent configuration.
        inherited = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        configuration = [f"GIT_CONFIG_COUNT={inherited + len(settings)}"]
        for index, (key, value) in enumerate(settings, inherited):
            configuration.extend((f"GIT_CONFIG_KEY_{index}={key}", f"GIT_CONFIG_VALUE_{index}={value}"))
        argv = ("env", *configuration, f"CARGO_TARGET_DIR={checkout / 'target'}", "bash", str(script.resolve()),
                "--validate-only", "--fix-commit",
                f"ci({production.name}): validation fixes after {checkpoint}")
        try:
            with validation_log.open("w", encoding="utf-8") as output:
                result = subprocess.run(argv, cwd=checkout, stdout=output, stderr=subprocess.STDOUT,
                                        text=True, check=False, pass_fds=copies.COMMAND_LOCKS.get())
            if result.returncode:
                detail = f"exit {result.returncode}, no step named"
                with validation_log.open(encoding="utf-8", errors="replace") as output:
                    for line in output:
                        marker = re.fullmatch(r"!!! VALIDATION FAILED at step: (.+) !!!", line.rstrip("\r\n"))
                        if marker is not None:
                            detail = f"step {marker.group(1)} failed"
                raise PointFailure("validation", f"{detail}; log {validation_log}")
        except OSError as error:
            raise PointFailure("validation", str(error)) from error
        validated = required(git(checkout, "rev-parse", "HEAD"), "ci start")
        _ = required(git(production.checkout, "fetch", "--no-tags", "--no-write-fetch-head", str(checkout), validated), "validation")
    return validated


def ci_start(production: Production, state_dir: Path, no_ci: bool, cancel_prior: bool) -> None:
    if no_ci:
        report("ci start", "ok", "skipped: no CI")
        return
    script = Path(os.environ.get("CI_POINTS_VALIDATE_SCRIPT", str(Path.home() / ".claude/scripts/validate_and_push/validate_and_push.sh"))).resolve()
    tip = branch_tip(production)
    remote = ci_push_remote(production.checkout)
    trigger = require_ci_trigger(production, tip, remote)
    state_dir.mkdir(parents=True, exist_ok=True)
    validation_record = state_dir / "ci_validation.json"
    saved = reusable_validation(production, validation_record, tip)
    if isinstance(saved, NoReusableValidation):
        saved = PassedValidation(tip, validate_commit(production, tip, script,
                                                      state_dir.resolve() / "ci_validation.log", remote.url))
        write_json(validation_record, {"input_tip": saved.input_tip, "validated_commit": saved.validated_commit})
    validated = saved.validated_commit
    push = ("env", f"PUSH_TARGET_BRANCH={production.merge_branch}", f"CI_VALIDATED_BASE={tip}",
            f"CI_PUSH_ORIGIN={remote.url}", f"CI_PUSH_REPO={remote.repo}",
            f"PRODUCTION_INTEGRATION_PUSH={promotion.integration_push_script(production)}",
            f"CANCEL_PRIOR={str(cancel_prior).lower()}", "bash", str(script.with_name("push_direct.sh")))
    with merge_lock(production.checkout, production.name):
        if ci_push_remote(production.checkout) != remote:
            raise PointFailure("ci start", "origin push URL changed during validation")
        if refusal := live_file_refusal(production.checkout, validated):
            raise PointFailure("validation", refusal)
        if validated != tip:
            current_branch = required(git(production.checkout, "branch", "--show-current"), "validation")
            if branch_tip(production) != tip or current_branch != production.merge_branch:
                raise PointFailure("validation", "merge branch moved during validation")
            _ = required(git(production.checkout, "merge", "--no-overwrite-ignore", "--ff-only", validated), "validation")
        published = required(command("validation", *push, "--publish-only", validated,
                                     cwd=production.checkout), "validation")
        publication = published.splitlines()[-1].split() if published else []
        if len(publication) != 2 or publication[1] not in ("pushed", "contained"):
            raise PointFailure("ci start", "push did not provide a published commit; rerun ci start")
        published_commit, publication_state = publication
        if published_commit != tip and required(git(production.checkout, "diff", "--name-only", tip, published_commit,
                                                   "--", ".github/workflows/"), "ci start"):
            trigger = require_ci_trigger(production, published_commit, remote)
        follow_args: tuple[str, ...] = (trigger.workflow, "push")
        if isinstance(trigger, DispatchStartsCi):
            dispatch_result = required(command("ci start", *push, "--dispatch-published", published_commit,
                                               trigger.workflow, cwd=production.checkout), "ci start")
            follow_args = (trigger.workflow, "workflow_dispatch", dispatch_result)
    followed = required(command("ci start", *push, "--follow-published", *publication, *follow_args,
                                cwd=production.checkout), "ci start")
    run = handoff(followed)
    if run.repo != remote.repo:
        raise PointFailure("ci start", f"CI run {run.run_id} is in {run.repo}, expected {remote.repo}")
    if run.tip != published_commit:
        raise PointFailure("ci start", f"CI run {run.run_id} is for {run.tip}, expected {published_commit}")
    if publication_state == "contained":
        report("validation", "ok", f"validated {tip}; no push; following {run.tip}")
    else:
        report("validation", "ok", f"validated {validated}; fast-forward push to origin/{production.merge_branch}")
    write_json(state_dir / "ci_run.json", {"repo": run.repo, "run_id": run.run_id, "tip": run.tip})
    report("ci start", "ok", f"run {run.run_id} on {run.tip} ({run.repo})")


def run_view(run: CiRun, checkout: Path) -> dict[str, object]:
    output = required(command("ci collect", "gh", "run", "view", run.run_id, "--repo", run.repo,
                              "--json", "headSha,status,conclusion,jobs", cwd=checkout), "ci collect")
    raw = cast(object, json.loads(output))
    if not isinstance(raw, dict):
        raise PointFailure("ci collect", "invalid GitHub run result")
    return cast(dict[str, object], raw)


def ci_collect(production: Production, state_dir: Path, no_ci: bool) -> None:
    from scripts.production.production_doc import EnrolledPaths, ProductionPathsError, resolve_production_paths

    try:
        paths = resolve_production_paths(production.doc)
    except ProductionPathsError as error:
        raise PointFailure("ci collect", str(error)) from error
    cargo_liner = isinstance(paths, EnrolledPaths) and production.name == CARGO_LINER_PRODUCTION
    if no_ci:
        if cargo_liner:
            raise PointFailure("ci collect", "cargo-liner requires CI on its integration tip")
        report("ci collect", "ok", "skipped: no CI")
        return
    run = read_run(state_dir)
    tip = branch_tip(production)
    count = len(merged_checkpoints(production))
    _ = command("ci collect", "gh", "run", "watch", run.run_id, "--repo", run.repo,
                "--exit-status", cwd=production.checkout)
    view = run_view(run, production.checkout)
    head = view.get("headSha")
    if not isinstance(head, str) or not head:
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI result names no head commit")
    ran_on = head
    if ran_on != run.tip or ran_on != tip:
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI ran on {ran_on}")
    if view.get("status") != "completed":
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI on {ran_on} is unfinished")
    raw_jobs = view.get("jobs")
    if not isinstance(raw_jobs, list):
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI on {ran_on} has no job results")
    jobs = cast(list[object], raw_jobs)
    seen: set[str] = set()
    required_linux = False
    for item in jobs:
        if not isinstance(item, dict):
            raise PointFailure("ci collect", f"rerun CI on {tip}: invalid job result on {ran_on}")
        job = cast(dict[str, object], item)
        name = job.get("name")
        conclusion = job.get("conclusion")
        if cargo_liner:
            if (not isinstance(name, str) or name in seen or job.get("status") != "completed"
                    or (conclusion != "success" and (conclusion != "skipped" or name not in CARGO_LINER_CONDITIONAL_JOBS))):
                raise PointFailure("ci collect", f"rerun {name or 'job'} on {tip}: CI ran on {ran_on} ({conclusion or 'unfinished'})")
            seen.add(name)
            continue
        if name == REQUIRED_CI_JOB:
            required_linux = conclusion == "success"
        if conclusion not in ("success", "skipped") or (name == REQUIRED_CI_JOB and not required_linux):
            raise PointFailure("ci collect", f"rerun {name if isinstance(name, str) else 'job'} on {tip}: CI ran on {ran_on} ({conclusion or 'unfinished'})")
    if cargo_liner:
        missing = (CARGO_LINER_REQUIRED_JOBS | CARGO_LINER_CONDITIONAL_JOBS) - seen
        if missing:
            raise PointFailure("ci collect", f"rerun CI on {tip}: missing jobs {', '.join(sorted(missing))}")
        if view.get("conclusion") != "success":
            raise PointFailure("ci collect", f"rerun CI on {tip}: CI on {ran_on} did not succeed")
        with merge_lock(production.checkout, production.name):
            current = branch_tip(production)
            if current != tip:
                raise PointFailure("ci collect", f"rerun CI on {current}: integration moved during collection")
            write_json(state_dir / "ci_points.json", {"count": count, "tip": tip, "policy": "cargo-liner"})
    elif not required_linux or view.get("conclusion") != "success":
        raise PointFailure("ci collect", f"rerun {REQUIRED_CI_JOB if not required_linux else 'CI'} on {tip}: CI ran on {ran_on} ({view.get('conclusion') or 'unfinished'})")
    else:
        write_json(state_dir / "ci_points.json", {"count": count, "tip": tip})
    report("ci collect", "ok", f"run {run.run_id} green on {tip}; {count} code merges recorded")
    print(f"--ci-green {tip}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    due_parser = commands.add_parser("due")
    ci_parser = commands.add_parser("ci")
    ci_commands = ci_parser.add_subparsers(dest="ci_action", required=True)
    start_parser = ci_commands.add_parser("start")
    _ = start_parser.add_argument("--cancel-prior", action="store_true")
    collect_parser = ci_commands.add_parser("collect")
    for subparser in (due_parser, start_parser, collect_parser):
        _ = subparser.add_argument("--production", required=True)
        _ = subparser.add_argument("--state-dir", required=True, type=Path)
        _ = subparser.add_argument("--no-ci", action="store_true")
    args = parser.parse_args()
    action = cast(str, args.action)
    step = action + (f" {cast(str, args.ci_action)}" if action == "ci" else "")
    try:
        production = read_production(cast(str, args.production))
        if action == "due":
            state = due(production, cast(Path, args.state_dir), cast(bool, args.no_ci))
            if isinstance(state, CiNotUsed):
                report("due", "ok", "skipped: no CI")
            elif isinstance(state, CiDue):
                report("due", "ok", "CI point due")
            else:
                report("due", "ok", f"{state.merges_since} of 5 code merges since last CI point")
        else:
            if cast(str, args.ci_action) == "start":
                ci_start(production, cast(Path, args.state_dir), cast(bool, args.no_ci),
                         cast(bool, args.cancel_prior))
            else:
                ci_collect(production, cast(Path, args.state_dir), cast(bool, args.no_ci))
        return 0
    except (copies.CopyError, PointFailure, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        report(error.step if isinstance(error, PointFailure) else step, "failed", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
