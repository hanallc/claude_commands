"""Script phone texts and named cases reviewed before delivery."""
import re
import shlex
from functools import partial

from scripts.notify.gpu_xid import XID_MEANINGS
from scripts.notify.push_texts import (ActionTemplate, NoActionNeeded, PushExample, PushText, SessionTemplate,
                                       TemplateValues, decision_values, display_values)
from scripts.production import nightly_schedule

# Codex usage destination: https://learn.chatgpt.com/docs/pricing (read 2026-10-14).
# Real quota case: notify/review.jsonl, 2026-10-13T22:30:22.569264-04:00.
# Other script senders have no live record in the two supplied logs. Their
# typical cases use existing test facts or the Nix thresholds, never invented log citations.

PAID_CREDIT_ACTION_PREFIX = "To stop it, run in a natedev Claude session: /tune accounts codex_credit_accounts "


def waiting_tests(count: int) -> str:
    """Name the tests that wait for the Mac."""
    return "1 test waits" if count == 1 else f"{count} tests wait"


def mac_idle_unreachable_values(given: dict[str, str]) -> TemplateValues:
    """Preserve the elapsed time in the values that reach send.py."""
    if "minutes" not in given:
        raise ValueError("mac_idle.unreachable: missing value minutes")
    displayed = dict(given)
    del displayed["minutes"]
    return TemplateValues(displayed, dict(given))


def nightly_result_values(given: dict[str, str]) -> TemplateValues:
    """Keep the sender values and display the outcome in the title."""
    required = {"branch", "time", "outcome", "types", "tests"}
    if given.keys() != required:
        raise ValueError(f"nightly.result: missing values {sorted(required - given.keys())}, "
                         + f"extra values {sorted(given.keys() - required)}")
    return TemplateValues({"outcome": given["outcome"]}, dict(given))


RESTART_FAILED_TEXT_ID = "shutdown.restart_failed"
_RESTART_FAILED_SUFFIX = " did not start on "


def restart_failed_values(given: dict[str, str]) -> TemplateValues:
    """Fit the session name in the title and keep the full resume argument."""
    required = {"session", "identity", "account"}
    extra = given.keys() - required - {"reference"}
    missing = required - given.keys()
    if missing or extra:
        raise ValueError(f"{RESTART_FAILED_TEXT_ID}: missing values {sorted(missing)}, extra values {sorted(extra)}")
    account = given["account"]
    session = given["session"]
    reference = given.get("reference") or session or given["identity"]
    if not reference.strip() or not account.strip():
        raise ValueError(f"{RESTART_FAILED_TEXT_ID}: the resume reference and account are required")
    title_session = session or "A session"
    width = 44 - len(_RESTART_FAILED_SUFFIX)
    account_room = width - min(len(title_session), len("A session"))
    title_account = account if len(account) <= account_room else account[:account_room - 1] + "…"
    title_room = width - len(title_account)
    if len(title_session) > title_room:
        title_session = title_session[:title_room - 1].rstrip(" -_./:\t–—") + "…"
    target = [f"--to={account}"] if account.startswith("-") else ["--to", account]
    command = [*target, "--", reference] if reference.startswith("-") else [reference, *target]
    return TemplateValues({"session": title_session, "account": title_account,
                           "resume_arguments": shlex.join(command)}, dict(given))


def hide_sender_values(given: dict[str, str], *, hidden: tuple[str, ...]) -> TemplateValues:
    """Keep sender values that the text does not display."""
    missing = sorted(set(hidden) - given.keys())
    if missing:
        raise ValueError(f"missing values {missing}")
    displayed = {field: value for field, value in given.items() if field not in hidden}
    return TemplateValues(displayed, dict(given))


def codex_workers_moved_title(account: str, target: str, *, uses_credits: bool) -> str:
    """Name the account that now pays for work."""
    if uses_credits:
        title = f"{account} is out; Codex now uses {target} credits"
        short_title = f"{account} out; uses {target} credits"
    else:
        title = f"{account} is out; Codex work now uses {target}"
        short_title = f"{account} out; work uses {target}"
    if len(title) <= 44:
        return title
    if len(short_title) <= 44:
        return short_title
    return "Codex work moves to paid credits" if uses_credits else "Codex work moves to another account"


def workers_moved_values(given: dict[str, str]) -> TemplateValues:
    """Use the new title for a saved move notice."""
    values = hide_sender_values(given, hidden=("account", "reset", "target", "funding"))
    transition = values.displayed.get("transition", "")
    if transition.startswith("Codex work now "):
        values.displayed["transition"] = codex_workers_moved_title(
            given["account"], given["target"], uses_credits=given["funding"] == "its own paid credits")
    return values


def remaining_credit_accounts(account: str, accounts: frozenset[str]) -> str:
    """Format the credit accounts without this account."""
    return " ".join(sorted(accounts - {account})) or "none"


def paid_credits_text_values(given: dict[str, str], *, queued: bool) -> TemplateValues:
    """Show the balance and the command to stop spending."""
    hidden = ("machine", "refill") if queued else ("machine", "refill", "next_reset")
    values = hide_sender_values(given, hidden=hidden)
    if missing := sorted({"account", "credits"} - values.displayed.keys()):
        raise ValueError(f"missing values {missing}")
    if "title" in values.displayed:
        raise ValueError("extra values ['title']")
    account = values.displayed.pop("account")
    credits = values.displayed.pop("credits")
    if "credit_accounts" not in values.displayed:
        from scripts.whoami.agent_accounts import codex_credit_accounts

        values.displayed["credit_accounts"] = remaining_credit_accounts(account, codex_credit_accounts())
    if credits == "unlimited":
        balance = "no limit"
    elif re.fullmatch(r"[0-9][0-9,]*", credits):
        balance = f"{credits} left"
    else:
        balance = "balance unknown"
    if queued:
        title = f"{account} uses credits until move"
        with_balance = f"{title}: {balance}"
        if balance != "balance unknown" and len(with_balance) <= 44:
            title = with_balance
        if len(title) > 44:
            title = "Codex uses credits until move"
        values.displayed["title"] = title
        return values

    status = "now spends paid credits"
    for title in (f"{account} {status}: {balance}", f"{account} uses credits: {balance}", f"{account} {status}"):
        if len(title) <= 44:
            values.displayed["title"] = title
            return values
    values.displayed["title"] = f"Codex {status}"
    return values


def permission_prompt_values(given: dict[str, str]) -> TemplateValues:
    """Shorten the title and keep the full session name."""
    values = hide_sender_values(given, hidden=("since",))
    if "unit" not in values.displayed:
        raise ValueError("missing value unit")
    if "title" in values.displayed:
        raise ValueError("extra values ['title']")
    unit = values.displayed["unit"]
    suffix = " stuck at a permission prompt"
    title = f"{unit} is{suffix}"
    if len(title) > 44:
        available = 44 - len(suffix)
        short_unit = unit if len(unit) <= available else unit[:available - 1] + "…"
        title = short_unit + suffix
    values.displayed["title"] = title
    return values


def credits_forecast_values(given: dict[str, str]) -> TemplateValues:
    """Render saved forecasts with the current text."""
    values = hide_sender_values(given, hidden=("horizon", "destination"))
    forecast = values.displayed.get("forecast", "")
    horizon = given["horizon"]
    if forecast.startswith("Codex account "):
        if horizon == "have no limit":
            forecast = "Paid credits have no limit."
        elif horizon == "run-out unknown":
            forecast = "No credit balance; the run-out time is unknown."
    elif old_run_out := re.fullmatch(
        r"Workers on Codex account .*; its ([0-9,]+) credits last until about (\S+) \d+-\d+-\d+ (\d+:\d+) .*\.",
        forecast,
    ):
        balance, day, time = old_run_out.groups()
        forecast = f"{balance} credits left; they run out about {day} {time}."
    elif old_balance := re.fullmatch(r"(?:Workers|Coding workers) on Codex account .*; ([0-9,]+) (?:paid )?credits left\.",
                                     forecast):
        balance = old_balance[1]
        if horizon == "use is zero":
            forecast = f"{balance} credits left; none used in the last hour."
        elif horizon == "run-out unknown":
            forecast = f"{balance} credits left; the run-out time is unknown."
    if "forecast" in values.displayed:
        values.displayed["forecast"] = forecast
    return values


def rust_result_values(given: dict[str, str], *, hidden: tuple[str, ...], title: str,
                       short_title: str = "", count_field: str = "") -> TemplateValues:
    """Fit the result in the phone title."""
    values = hide_sender_values(given, hidden=hidden)
    expected = {"version", count_field} if count_field else {"version"}
    if values.displayed.keys() != expected:
        raise ValueError(f"missing values {sorted(expected - values.displayed.keys())}; "
                         + f"extra values {sorted(values.displayed.keys() - expected)}")
    if count_field:
        count = values.displayed[count_field]
        word = count_field.removesuffix("s") if count == "1" else count_field
        values.displayed[count_field] = f"{count} {word}"
    short_title = short_title or title
    versionless_title = short_title.replace(" {version}", "")
    for form in (title, short_title, versionless_title):
        display_title = form.format_map(values.displayed)
        if len(display_title) <= 44:
            return TemplateValues({"result": display_title}, values.supplied)
    values.displayed[count_field] = count_field
    return TemplateValues({"result": versionless_title.format_map(values.displayed)}, values.supplied)


def nightly_not_run_values(given: dict[str, str]) -> TemplateValues:
    """Accept the legacy values that the installed timer sends."""
    if given.keys() != {"branch", "time", "reason", "action"}:
        return display_values(given)
    action = given["action"]
    if action == "None. The next nightly run tries again.":
        action = ""
    return TemplateValues({"reason": given["reason"], "action": action,
                           "next_run": nightly_schedule.nightly_next_run()}, dict(given))


AUTO_MODE_REFUSED_TITLE = "{session}: {count} {noun} refused"


def auto_mode_refused_values(given: dict[str, str]) -> TemplateValues:
    """Add the waits clause when it fits in the title."""
    values = hide_sender_values(given, hidden=tuple(field for field in ("waits", "rule") if field in given))
    waits = given.get("waits", "")
    clause = ""
    if waits and {"session", "count", "noun"} <= values.displayed.keys():
        suffix = f"; {waits}"
        title = AUTO_MODE_REFUSED_TITLE.format(**values.displayed)
        if len(title + suffix) <= 44:
            clause = suffix
    return TemplateValues(values.displayed | {"waits_clause": clause}, values.supplied)


def silence_auto_off_values(given: dict[str, str]) -> TemplateValues:
    """Shorten the held-push sentence for the title."""
    if "held" not in given:
        raise ValueError("silence.auto_off: missing values ['held']")
    held = given["held"]
    displayed = dict(given)
    if held == "No pushes were held while you were away.":
        displayed["held"] = "no held pushes"
    elif held == "The push held while you were away comes next.":
        displayed["held"] = "1 held push comes next"
    elif count := re.fullmatch(r"The ([0-9]+) pushes held while you were away come next\.", held):
        displayed["held"] = f"{count[1]} held pushes come next"
    else:
        raise ValueError(f"silence.auto_off: unknown held value {held!r}")
    return TemplateValues(displayed, dict(given))


NATE_ADHOC_TEXT_ID = "nate.adhoc"
NEEDS_GO_TEXT = "session.needs_go"
NEEDS_GO_ASK = ('~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py ask "L{item}"'
                + ' --kind go --target "<staff id>" --what "<change>" --why "<reason>"')
ENTRIES = (
    PushText("accounts.login_sitting", "accounts", "note", "Claude logins will expire", NoActionNeeded(),
        "You asked for early login reminders.\nWithout renewal, Claude work stops at these login end dates:\n{accounts}\nFor each account:\n"
        + "1. Sign out of claude.ai in the browser.\n2. Sign in as that account.\n"
        + "3. On each named machine, run /login in a Claude session on that account.",
        (PushExample("weekly", {"accounts": "claude1: Mac Sat 11-28, natedev Fri 12-11\n"
            + "claude2: Mac Sun 11-29, natedev Sat 12-12\nclaude3: Mac Mon 11-30, natedev Sun 12-13\n"
            + "claude4: Mac Tue 12-01, natedev Mon 12-14\nclaude5: Mac Wed 12-02, natedev Tue 12-15"}),
         PushExample("safety", {"accounts": "claude4: Mac Sat 11-28"})),
        purpose="The weekly sitting or safety margin requires a preventive Claude login.", header=False),
    # These examples use regional permission errors.
    PushText("aws.snapshot_failed", "buildlog", "note", "Cloud gates use old AWS prices",
        ActionTemplate("To allow AWS reads, in a terminal on natedev, run ~/.claude/scripts/lib/py "
                       + "~/.claude/scripts/aws_target/deploy.py up --approve-resources"),
        "Cloud gates run merge tests on AWS and pick a region from hourly AWS reads.\n{failures}\n"
        + "With old data, a gate can pick a region that costs more or is full.\n"
        + "The command only allows these reads and costs nothing. Then the hourly refresh works. "
        + "It needs your AWS admin passphrase.",
        (PushExample("permission", {
            "failures": "AWS refused the us-east-2 price read."}),
         PushExample("regional_permissions", {
            "failures": "AWS refused the us-east-2 price read.\nAWS refused the us-west-2 Standard quota read."})),
        purpose="AWS denied a regional read that the gate deployment can repair."),
    PushText("nightly.assigned", "sdlc", "note", "Nightly check {outcome}", NoActionNeeded(),
        "Fix assigned to: {unit}.",
        (PushExample("tests_failed", {"branch": "main", "time": "03:00 EST", "outcome": "failed",
            "types": "passed", "tests": "failed in production/test_staff.py", "received": "03:00 EST",
            "unit": "staff", "assigned": "03:05 EST"}),
         PushExample("types_failed", {"branch": "main", "time": "03:00 EST", "outcome": "failed",
            "types": "failed with 3 errors, 0 warnings, 0 notes", "tests": "passed", "received": "03:00 EST",
            "unit": "types", "assigned": "03:05 EST"})),
        purpose="sdlc assigned the failed nightly check to a unit director.", header=False,
        expand_values=partial(hide_sender_values, hidden=("branch", "time", "types", "tests", "received", "assigned"))),
    PushText("nightly.result", "nightly", "note", "Nightly check {outcome}", NoActionNeeded(),
        "",
        (PushExample("passed", {"branch": "main", "time": "03:53 EST", "outcome": "passed",
                               "types": "passed", "tests": "passed"}),
         PushExample("tests_failed", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
             "types": "passed", "tests": "failed in direct/test_simplify_lens.py, production/test_staff.py"}),
         PushExample("types_failed", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
             "types": "failed with 3 errors, 1 warning, 0 notes", "tests": "passed"}),
         PushExample("many_failed", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
             "types": "passed", "tests": "failed in direct/test_assign.py, direct/test_board.py, "
             + "message/test_send.py, notify/test_review.py, production/test_staff.py, "
             + "shutdown/test_restart.py, settings/test_settings.py, whoami/test_usage.py and 12 more"}),
         PushExample("did_not_finish", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
                                       "types": "did not run", "tests": "did not finish"})),
        purpose="The nightly check finished. The title gives the result.",
        header=False, expand_values=nightly_result_values),
    PushText("nightly.not_run", "nightly", "note", "Nightly check did not run", ActionTemplate("{action}", omit_when_empty=True),
        "Why: {reason}.\nNext run: {next_run}.",
        (PushExample("fetch_failed", {"next_run": "Tue 11-24 00:53 PST",
             "reason": "git fetch of main failed: ssh connection timed out",
             "action": ""}),
         PushExample("cold_credentials", {"next_run": "Tue 11-24 00:53 PST",
             "reason": "GitHub credentials are cold",
             "action": "In a terminal on natedev, run github-warmup"})),
        purpose="The nightly check did not run. The push gives the reason and next run.",
        expand_values=nightly_not_run_values),
    PushText("decision.choices", "{sender}", "decision", "{title}",
        ActionTemplate("On {machine}, reply in the {session} terminal: {choice_labels}."),
        "Situation: {situation}\n\nChoices:\n{choices}\n\n"
        + "Recommendation: {recommended_choice}. {recommendation_reason}",
        (PushExample("hana", {
            "sender": "hana", "title": "Assembly opening stalls on cable routes", "session": "hana", "machine": "natedev",
            "situation": "Opening an Assembly stalls for several seconds because each animation frame recalculates all 26 cable routes.",
            "choices": '[{"label":"Fix now","effect":"cables-unit makes this small fix, then returns to its hold. Assemblies open without this stall."},{"label":"Wait","effect":"cables-unit remains on hold. The stall remains until its cable work resumes."}]',
            "recommended_choice": "Fix now",
            "recommendation_reason": "The small fix removes the stall without resuming the rest of the held cable work."}),),
        purpose="A decision with structured choices and a recommendation.", session=SessionTemplate("{session}"),
        agent_written=True, expand_values=decision_values),
    PushText("hana_shot.mac_locked", "Hana screenshot", "blocked", "Hana screenshot waits for the Mac",
        ActionTemplate("Unlock the Mac."),
        "The screenshot starts when the Mac is ready. This alert stops on its own.",
        (PushExample("typical", {}),),
        purpose="A Hana screenshot waits for the Mac to unlock."),
    PushText("mac_idle.unreachable", "Mac idle watch", "blocked", "Mac does not answer; {tests}",
        ActionTemplate("Wake the Mac or check its network."),
        "",
        (PushExample("one_test", {"minutes": "10", "tests": waiting_tests(1)}),
         PushExample("three_tests", {"minutes": "12", "tests": waiting_tests(3)})),
        purpose="The Mac does not answer while natedev tests wait for it.",
        expand_values=mac_idle_unreachable_values),
    PushText("machine_watch.unreachable", "Machine watch", "blocked", "Builds cannot reach {machine}",
        ActionTemplate("Turn on the {machine} build machine and check its network connection."),
        "For {minutes} min, the build queue could not connect to the {machine} build machine. "
        + "Until {machine} answers again, its build steps wait or run more slowly on natedev instead.",
        (PushExample("guest", {"machine": "mac-linux", "minutes": "60"}),),
        purpose="Builds cannot connect to a machine in use."),
    PushText("nix_heal.failed", "nix-heal", "blocked", "{session} builds will fail: no nix shell",
        ActionTemplate("On {machine}, fix the nix shell in {cwd}, then restart {session}"),
        "The nix shell for {session}'s worktree did not load, so its builds will fail. "
        + "{cause}\n\n"
        + "The session's worktree changes stay on disk. This alert clears when the session has a nix shell.",
        (PushExample("refused", {"session": "handler", "machine": "natedev", "cwd": "/home/natepiano/worktrees/handler",
            "cause": "The automatic restart could not fix it. The .envrc file loaded, but no nix shell started."}),
         PushExample("still_broken", {"session": "handler", "machine": "Mac", "cwd": "/Users/nate/worktrees/handler",
            "cause": "An automatic restart did not fix it."})),
        purpose="A session has no nix shell and automatic recovery failed.",
        session=SessionTemplate("{session}")),
    # Stall-watch examples are the real prompts of 2026-10-23: a Bash call reads "run <command>" (cut to 89
    # characters and "…"), an edit "edit <file>", as stall_watch.prompt_request words them from the pending call.
    PushText("stall_watch.permission_prompt", "stall-watch", "blocked", "{title}",
        ActionTemplate("On {machine}, open the {unit} herdr tab and answer its prompt"),
        "It asks to {request}.",
        (PushExample("typical", {"unit": "handler", "machine": "natedev", "since": "09:01 AST",
            "request": "run cargo build -p cargo-handler"}),
         PushExample("edit", {"unit": "messaging", "machine": "natedev", "since": "06:05 AST",
            "request": "edit send.py"})),
        purpose="A session waits for you to answer its permission prompt.",
        session=SessionTemplate("{unit}"), expand_values=permission_prompt_values),
    PushText(RESTART_FAILED_TEXT_ID, "shutdown", "decision", "{session}" + _RESTART_FAILED_SUFFIX + "{account}",
        ActionTemplate("In the natedev session, run: /agents resume {resume_arguments}"),
        "",
        (PushExample("typical", {"session": "trunk", "identity": "019b3a40-4250-7000-8000-000000000101",
             "account": "claude4"}),
         PushExample("one_long_name", {"session": "account-migration-verification", "identity": "019b3a40-4250-7000-8000-000000000102",
             "account": "claude3"}),
         PushExample("unnamed", {"session": "", "identity": "019b3a40-4250-7000-8000-000000000103",
             "account": "codex2"})),
        purpose="A session resume failed and needs a retry.",
        expand_values=restart_failed_values),
    PushText("quota.claude_next_move", "quota_alert", "note", "{account} has {remaining} left with no move",
        ActionTemplate("In the natedev session, type /agents move <group> <account>."),
        "",
        (PushExample("uncovered_low", {"account": "claude3", "remaining": "2%"}),
         PushExample("uncovered_empty", {"account": "claude4", "remaining": "0%"})),
        purpose="A Claude account runs low, and no move is set up for it."),
    PushText("quota.claude_expiry", "quota_alert", "note", "{account} runs out {empty_at}",
        ActionTemplate("In the natedev session, type /agents move <role> <account>"),
        "",
        (PushExample("forecast", {"account": "claude3", "empty_at": "Sun 11-22 07:00 EST"}),),
        purpose="A Claude account is expected to run out soon and has no scheduled move."),
    PushText("quota.token_expiry", "quota_alert", "note", "{token} expires {date}",
        ActionTemplate("Read the Accounts page."),
        "",
        (PushExample("expiry", {"token": "hana-macos-runner-query", "date": "2027-11-22"}),),
        purpose="A token is near its end date."),
    PushText("quota.mac_token_login", "quota_alert", "note", "Mac token login failed {time}",
        ActionTemplate("Read the Accounts page."),
        "",
        (PushExample("refused", {"time": "Sat 11-21 10:07 EST"}),),
        purpose="A Mac relay reports that Claude rejected its fallback token."),
    PushText("quota.codex_next_move", "quota_alert", "note", "{account} has {remaining} left; {outcome_title}",
        NoActionNeeded(),
        "",
        (PushExample("credits_on", {"account": "codex2", "remaining": "<1%", "outcome_title": "paid credits next"}),
         PushExample("credits_off_codex", {"account": "codex2", "remaining": "2%", "outcome_title": "codex1 next"}),
         PushExample("credits_off_alone", {"account": "codex1", "remaining": "1%", "outcome_title": "units work alone next"})),
        purpose="A Codex account runs low. Its workers move or spend credits soon.", one_line=True),
    PushText("quota.codex_workers_moved", "quota_alert", "note", "{transition}",
        NoActionNeeded(),
        "",
        (PushExample("codex_one", {"account": "codex1", "reset": "Tue 2026-10-20 00:58 AST", "target": "codex2",
            "funding": "97% weekly usage left", "transition": "codex1 is out; Codex work now uses codex2"}),
         PushExample("codex_two", {"account": "codex2", "reset": "Thu 2026-10-22 07:00 AST", "target": "codex1",
            "funding": "its own paid credits", "transition": "codex2 is out; Codex now uses codex1 credits"})),
        purpose="A Codex account ran out, and its work moved to another account.",
        header=False, expand_values=workers_moved_values),
    PushText("quota.codex_workers_stay", "quota_alert", "note", "{account} is empty; {outcome}",
        NoActionNeeded(),
        "",
        (PushExample("no_room", {"account": "codex1", "outcome": "we're in self-actualize mode; worker work moves to claude3"}),
         PushExample("failed_move", {"account": "codex2", "outcome": "we're in self-actualize mode; worker work moves to claude3"}),
         PushExample("queued", {"account": "codex1", "outcome": "workers wait for their queued move"})),
        purpose="A Codex account ran out; its workers wait or their work moves.", one_line=True),
    PushText("quota.codex_credits_forecast", "quota_alert", "note", "{account} is running on paid credits",
        NoActionNeeded(),
        "{forecast}",
        (PushExample("codex_one", {"account": "codex1",
            "forecast": "6,370 credits left; they run out about Fri 13:05.",
            "destination": "When credits run out, workers move to codex2 (97% weekly usage left).",
            "horizon": "until Fri 13:05 AST"}),
         PushExample("codex_two", {"account": "codex2",
            "forecast": "2,114 credits left; they run out about Fri 12:51.",
            "destination": "When credits run out, workers move to codex1 and spend its own paid credits.",
            "horizon": "until Fri 12:51 AST"}),
         PushExample("zero_use", {"account": "codex1",
            "forecast": "6,370 credits left; none used in the last hour.",
            "destination": "When credits run out, workers move to codex2 (97% weekly usage left).",
            "horizon": "use is zero"}),
         PushExample("unlimited", {"account": "codex1", "forecast": "Paid credits have no limit.",
            "destination": "", "horizon": "have no limit"}),
         PushExample("unknown_balance", {"account": "codex1",
            "forecast": "No credit balance; the run-out time is unknown.",
            "destination": "When credits run out, workers move to codex2 (97% weekly usage left).",
            "horizon": "run-out unknown"}),
         PushExample("unknown_use", {"account": "codex2",
            "forecast": "2,114 credits left; the run-out time is unknown.",
            "destination": "When they run out, Codex work moves to Claude, the other coding assistant, until Tue 2026-10-20 00:58 AST.",
            "horizon": "run-out unknown"}),
         PushExample("queued", {"account": "codex1",
            "forecast": "6,370 credits left; they run out about Fri 13:05.",
            "destination": "When credits run out, workers wait for the account move you queued.",
            "horizon": "until Fri 13:05 AST"})),
        purpose="Tells when the paid credits of a Codex account run out.",
        header=False, expand_values=credits_forecast_values),
    PushText("quota.codex_refused", "quota_alert", "decision", "Codex work stopped for lack of usage",
        ActionTemplate("At chatgpt.com/codex/settings/usage, add credits for account {account}."),
        "Codex hit its usage limit on account {account} and stopped accepting coding jobs. "
        + "Moving those jobs to Claude, the other coding assistant, also failed, so the work is paused. "
        + "Adding credits lets Codex accept jobs again.",
        (PushExample("typical", {"account": "codex1"}),
         PushExample("long_account", {"account": "codex999"})),
        purpose="Codex stopped work for lack of usage, and Claude could not take it."),
    PushText("rust.passed", "rust_release", "decision", "{result}",
        ActionTemplate("In the hana session, reply upgrade (recommended) or wait."),
        "",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0"}),
         PushExample("long_versions", {"version": "100.100.100", "pin": "99.99.99"})),
        purpose="A new Rust compiler passed its check on Hana. Upgrade or wait.",
        expand_values=partial(rust_result_values, hidden=("pin",),
            title="Hana passes on Rust {version}")),
    PushText("rust.warnings", "rust_release", "decision", "{result}",
        ActionTemplate("In the hana session, reply wait (recommended) or upgrade."),
        "",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0", "warnings": "2"}),
         PushExample("many_warnings", {"version": "1.100.0", "pin": "1.99.0", "warnings": "14"})),
        purpose="A new Rust compiler adds warnings to Hana. Upgrade or wait.",
        expand_values=partial(rust_result_values, hidden=("pin",), count_field="warnings",
            title="Rust {version} adds {warnings} to Hana")),
    PushText("rust.errors", "rust_release", "decision", "{result}",
        ActionTemplate("In the hana session, reply wait (recommended) or upgrade."),
        "",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0", "errors": "1", "warnings": "2"}),
         PushExample("many_errors", {"version": "1.100.0", "pin": "1.99.0", "errors": "14", "warnings": "14"})),
        purpose="A new Rust compiler failed its check on Hana. Upgrade or wait.",
        expand_values=partial(rust_result_values, hidden=("pin", "warnings"), count_field="errors",
            title="Rust {version} fails Hana's check: {errors}", short_title="Rust {version}: {errors} in Hana")),
    PushText("rust.repair_failed", "rust_release", "decision", "{result}",
        ActionTemplate("In the hana session, reply wait (recommended) or upgrade."),
        "",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0"}),
         PushExample("long_versions", {"version": "100.100.100", "pin": "99.99.99"})),
        purpose="A new Rust compiler breaks the build repair tool. Upgrade or wait.",
        expand_values=partial(rust_result_values, hidden=("pin",),
            title="Rust {version} breaks the build repair tool", short_title="Rust {version} breaks build repair")),
    PushText("sweep.disk_floor", "disk_floor", "decision", "{machine} disk is low; {machine} tests can fail",
        ActionTemplate("{folder_action}"),
        "{machine} has {free} free, below the {floor} alert line, so {machine} test builds can fail. {recommendation}",
        (PushExample("available", {
            "machine": "natedev",
            "folder_action": "In the natedev Claude session, say which natedev folders to delete.",
            "free": "140.0 GiB", "floor": "150.0 GiB",
            "recommendation": "Delete ~/.local/share/Trash (recommended): this frees 26.0 GiB, which covers the 10.0 GiB shortfall."}),
         PushExample("unavailable", {
            "machine": "natedev",
            "folder_action": "In the natedev Claude session, reply measure or delete nothing.",
            "free": "140.0 GiB", "floor": "150.0 GiB",
            "recommendation": "Folder sizes are unknown. Measure (recommended): natedev lists folder sizes, then you choose. Delete nothing: free space stays low."}),
         PushExample("insufficient", {
            "machine": "natedev",
            "folder_action": "In the natedev Claude session, say which natedev folders to delete.",
            "free": "120.0 GiB", "floor": "150.0 GiB",
            "recommendation": "The largest folder is ~/Trash at 20.0 GiB. Deleting it frees only part of the 30.0 GiB shortfall; the natedev session lists every folder."}),
         PushExample("protected", {
            "machine": "Mac",
            "folder_action": "In the natedev Claude session, say which Mac folders to delete.",
            "free": "140.0 GiB", "floor": "150.0 GiB",
            "recommendation": "Review ~/old-output first (recommended): deleting it frees 26.0 GiB, which covers the 10.0 GiB shortfall."})),
        purpose="A low-disk alert recommends one folder for natedev review."),
    # Current disk-floor.nix: alertGib=150, build floor=300 GiB.
    PushText("nixos.disk_low", "disk_floor", "note", "Builds need more disk space",
        ActionTemplate("In a terminal on natedev, delete {shortfall} GiB of personal files you no longer need."),
        "The natedev computer has {free} GiB free on its main disk. Builds need 300 GiB. "
        + "Automated cleanup could not restore that space; builds may fail until you free more.",
        (PushExample("typical", {"free": "149", "shortfall": "151"}),
         PushExample("no_free_space", {"free": "0", "shortfall": "300"})),
        purpose="Cleanup did not free enough disk on natedev. Delete your files."),
    # GPU watch examples use the Xid codes in the user's 2026-10-30 alert request.
    PushText("nixos.gpu_stopped", "gpu", "decision", "natedev GPU stopped",
        ActionTemplate("Restart natedev."),
        "",
        (PushExample("xid_79_night", {"xid": "79", "meaning": XID_MEANINGS["79"], "time": "02:17 EDT"}),
         PushExample("xid_119", {"xid": "119", "meaning": XID_MEANINGS["119"], "time": "14:05 EDT"})),
        purpose="natedev's GPU stopped, and both displays are dark.",
        expand_values=partial(hide_sender_values, hidden=("xid", "meaning", "time"))),
    # Current ups.nix: low-runtime shutdown at five minutes or 30% charge.
    PushText("nixos.ups_battery", "ups", "blocked", "Power out: natedev on battery, {minutes} min left",
        ActionTemplate("Save your open work on natedev now."),
        "",
        (PushExample("typical", {"charge": "80", "minutes": "20"}),
         PushExample("shutdown_threshold", {"charge": "30", "minutes": "5"})),
        purpose="Power is out, and natedev runs on battery. Save your work.",
        expand_values=partial(hide_sender_values, hidden=("charge",))),
    PushText("nixos.ups_replace_battery", "ups", "note", "natedev UPS battery is worn out",
        ActionTemplate("Replace the battery in the natedev UPS."),
        "",
        (PushExample("typical", {}),),
        purpose="The UPS battery is worn out and needs replacement."),
    PushText("nixos.ups_no_contact", "ups", "note", "natedev lost contact with its UPS",
        ActionTemplate("Check the USB cable between the UPS and natedev."),
        "",
        (PushExample("typical", {}),),
        purpose="natedev cannot read the UPS and needs its USB connection checked."),
    PushText("nixos.ups_power_back", "ups", "note", "Power is back on natedev", NoActionNeeded(),
        "",
        (PushExample("typical", {"charge": "80"}),),
        purpose="Mains power returned to natedev after a battery alert.",
        header=False, expand_values=partial(hide_sender_values, hidden=("charge",))),
    PushText("nixos.ups_shutdown", "ups", "blocked", "Power out: natedev shuts down",
        ActionTemplate("When power returns, start natedev if it stays off for 3 minutes."),
        "",
        (PushExample("typical", {}),),
        purpose="Power is out, and the battery is low. natedev shuts down."),
    PushText("mac.locked", "{sender}", "blocked", "Mac locked: GPU shots stopped",
        ActionTemplate('Unlock the Mac and keep its display awake, then type "Mac unlocked" in the {sender} tab on natedev'),
        "Every Mac screenshot is black. {waiting}. Code and test work goes on.",
        (PushExample("typical", {"sender": "hana", "waiting": "One unit's phase waits for 2 shots"}),),
        purpose="The Mac is locked and screenshots wait for you to unlock it."),
    PushText("github.keys_cold", "{sender}", "blocked", "GitHub keys locked on {machine}",
        ActionTemplate("Run github-warmup in a terminal on {machine}"), "",
        (PushExample("typical", {"sender": "hana", "machine": "natedev"}),),
        purpose="GitHub keys are cold. Unlock them once for every session."),
    PushText("build_hold.blocking", "{sender}", "blocked", "{machine} build hold is blocking all work",
        ActionTemplate("Say 'release' in the {holder} tab"),
        "{holder} holds {machine} for: {reason}. That work finished at {finished}, but the hold still pauses every other build.",
        (PushExample("typical", {"sender": "hana", "machine": "natedev", "holder": "builds",
                                 "reason": "a compiler check", "finished": "09:10 EST"}),),
        purpose="Finished work left a build hold that you must release."),
    # Agent-written fields: short and to the point; live review selects a fixed text when one fits.
    PushText("alert.blocked", "{sender}", "blocked", "{title}", ActionTemplate("{action}"), "{body}",
        (PushExample("typical", {"sender": "hana", "title": "Hana crashes at start on the Mac",
            "action": "On the Mac, tell the hana tab to roll back or keep the Bevy update",
            "body": "Hana stops at start with a shader error since the Bevy update at 09:10. Mac screenshots wait until it starts again."}),),
        purpose="Work stopped. Write title, action and body short and to the point.", agent_written=True),
    PushText("alert.decision", "{sender}", "decision", "{title}", ActionTemplate("{action}"), "{body}",
        (PushExample("typical", {"sender": "builds", "title": "Keep the old build cache?",
            "action": "Say keep or delete in the builds tab",
            "body": "The old build cache uses 120 GiB. Deleting it frees the space; the next build of each repo takes about 10 minutes longer."}),),
        purpose="A choice for you. Write title, action and body short and to the point.", agent_written=True),
    # /nate adhoc: an agent writes the values at send time; review them like free text (user, 2026-10-28).
    # The user asked that the template name close each push; pushover.py's "Template:" line does that
    # for every push (user, 2026-10-27).
    PushText(NATE_ADHOC_TEXT_ID, "{sender}", "note", "{sender} {title}", NoActionNeeded(),
        "{description}",
        (PushExample("typical", {"sender": "natedev", "title": "the /nate skill is ready",
                                 "description": "You asked to be told when the /nate skill is ready. It is ready now: "
                                 + "/nate adhoc <instructions> sends you an update like this one."}),
         PushExample("long_sender", {"sender": "cargo-liner-style", "title": "renames are done",
                                     "description": "You asked to be told when the tile and handler renames finish. "
                                     + "Both are done, and both units run under their new names."})),
        purpose="An update that an agent writes for you with /nate adhoc.",
        header=False, agent_written=True),
    # send.py counts the refused commands and reviews the waits clause.
    PushText("auto_mode.refused", "{sender}", "decision", AUTO_MODE_REFUSED_TITLE + "{waits_clause}",
        ActionTemplate("On {machine}, in the {session} pane, type: Go: run the refused {noun}"),
        "",
        (PushExample("typical", {"sender": "cargo-liner-style", "session": "modules", "machine": "natedev",
            "count": "2", "noun": "commands", "waits": "its build waits"}),
         PushExample("one_with_rule", {"sender": "build-followups", "session": "messaging", "machine": "natedev",
            "count": "1", "noun": "command", "waits": "its work waits", "rule": "Bash(git push:*)"}),
         PushExample("file_change_no_waits", {"sender": "hana", "session": "demo", "machine": "natedev",
            "count": "1", "noun": "command"})),
        purpose="Auto mode refused a session's commands. The commands wait for you.",
        session=SessionTemplate("{session}"), expand_values=auto_mode_refused_values),
    # send.py fills the sender from the session or the script name.
    PushText("quota.paid_credits", "{sender}", "decision", "{title}",
        ActionTemplate(PAID_CREDIT_ACTION_PREFIX + "{credit_accounts}"),
        "",
        (PushExample("typical", {"sender": "quota_alert", "account": "codex2", "machine": "natedev",
            "credits": "25,345", "refill": "Tue 10-27 02:33 AST", "next_reset": "Sat 10-24 16:40 AST",
            "credit_accounts": "none"}),
         PushExample("from_session", {"sender": "natedev", "account": "codex1", "machine": "natedev",
            "credits": "6,370", "refill": "Thu 10-29 07:00 AST", "next_reset": "Sat 10-24 16:40 AST",
            "credit_accounts": "codex2 codex3"})),
        purpose="An account now spends paid credits. You can turn this off.",
        expand_values=partial(paid_credits_text_values, queued=False)),
    PushText("quota.paid_credits_queued", "{sender}", "decision", "{title}",
        ActionTemplate(PAID_CREDIT_ACTION_PREFIX + "{credit_accounts}"),
        "",
        (PushExample("typical", {"sender": "quota_alert", "account": "codex2", "machine": "natedev",
            "credits": "25,345", "refill": "Tue 10-27 02:33 AST", "credit_accounts": "none"}),),
        purpose="An account now spends paid credits, and you queued a workers move.",
        expand_values=partial(paid_credits_text_values, queued=True)),
    # The user approved this layout on 2026-11-24. send.py supplies the sender for the footer.
    PushText(NEEDS_GO_TEXT, "{sender}", "decision", "{session} waits for your go",
        ActionTemplate("Press Go on List item {item}: {what}"), "",
        (PushExample("typical", {"sender": "natedev", "session": "trunk", "item": "412",
            "what": "remove arrange_jacks/ from phases/130/files; 140 owns it"}),
         PushExample("delete_worktree", {"sender": "build-followups", "session": "herdr", "item": "413",
            "what": "delete the herdr-panes worktree and its branch"})),
        purpose="A session waits for your Go on a List item.",
        session=SessionTemplate("{session}")),
    # Auto silence (user spec, 2026-10-20: "a very compact format"): auto_silence.py sends one when the
    # user goes idle, before silence starts, and one when their next prompt ends it.
    PushText("silence.auto_on", "{sender}", "note", "Auto silence on after {minutes} min idle", NoActionNeeded(),
        "",
        (PushExample("typical", {"sender": "natedev", "minutes": "45"}),
         PushExample("long_idle", {"sender": "natedev", "minutes": "1440"})),
        purpose="You went idle, and auto silence starts to keep pushes.",
        header=False),
    PushText("silence.auto_off", "{sender}", "note", "You are back: {held}", NoActionNeeded(),
        "",
        (PushExample("typical", {"sender": "natedev", "held": "The 3 pushes held while you were away come next."}),
         PushExample("one_held", {"sender": "natedev", "held": "The push held while you were away comes next."}),
         PushExample("none_held", {"sender": "natedev", "held": "No pushes were held while you were away."})),
        purpose="You came back, and auto silence ended.",
        header=False, expand_values=silence_auto_off_values),
)
