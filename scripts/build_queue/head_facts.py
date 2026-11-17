"""Format the priority list for the Builds page."""

from scripts.build_queue import admission_records


def priority_text(priority: admission_records.QueuePriority | admission_records.NoQueuePriority) -> str:
    return ("" if isinstance(priority, admission_records.NoQueuePriority) else
            "queue priority: " + ", ".join(f"{entry['unit']} ({entry['why']})" for entry in priority.entries))
