"""Read Linux process stat fields."""
from pathlib import Path


PROC_ROOT = Path('/proc')


def process_fields(pid: int, proc_root: Path = PROC_ROOT) -> list[str]:
    """Read stat fields after the process name, which can contain spaces."""
    return (proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
