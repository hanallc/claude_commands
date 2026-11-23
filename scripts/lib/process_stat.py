"""Read Linux process stat fields."""
from pathlib import Path


PROC_ROOT = Path('/proc')


def name_and_fields(pid: int, proc_root: Path = PROC_ROOT) -> tuple[str, list[str]]:
    """Read the process name and the stat fields after it. The name can contain spaces and parentheses."""
    name, close, rest = (proc_root / str(pid) / 'stat').read_text().partition('(')[2].rpartition(')')
    if not close:
        raise IndexError(f'process {pid} stat has no name')
    return name, rest.split()


def process_fields(pid: int, proc_root: Path = PROC_ROOT) -> list[str]:
    """Read stat fields after the process name, which can contain spaces."""
    return name_and_fields(pid, proc_root)[1]
