"""Format the priority list for the Builds page."""

from typing import assert_never

from scripts.build_queue import admission_records


def asker_text(asker: admission_records.Asker) -> str:
    match asker:
        case admission_records.StaffAsker(name=name):
            return name
        case admission_records.UnstaffedSession(session_id=session_id):
            return session_id
        case admission_records.PlainShellAsker():
            return "nate (plain shell)"
        case _:
            assert_never(asker)


def priority_entry_text(entry: admission_records.PriorityEntry) -> str:
    """Name the unit or the one run, then the actor that asked and the user's words."""
    target = (f"{entry['unit']} · {entry['step']} (one run)" if admission_records.is_run_entry(entry)
              else entry["unit"])
    asker = admission_records.entry_asker(entry)
    reason = (entry["why"] if isinstance(asker, admission_records.AskerNotRecorded)
              else f'{asker_text(asker)}: "{entry["why"]}"')
    return f"{target} ({reason})"


def priority_text(priority: admission_records.QueuePriority | admission_records.NoQueuePriority) -> str:
    return ("" if isinstance(priority, admission_records.NoQueuePriority) else
            "queue priority: " + ", ".join(priority_entry_text(entry) for entry in priority.entries))
