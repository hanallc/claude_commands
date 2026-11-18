"""Default paths shared by build-state owners and their write guard."""

from pathlib import Path


DEFAULT_HOLDER_DIRECTORY = Path(".local/state/build-hold")
DEFAULT_RELEASE_DIRECTORY = DEFAULT_HOLDER_DIRECTORY.with_name(DEFAULT_HOLDER_DIRECTORY.name + "-release")
DEFAULT_ENFORCER_DIRECTORY = Path(".local/state/build-hold-enforcer")
DEFAULT_PROC_ROOT = Path("/proc")
DEFAULT_ADMISSION_SOCKET = Path("/run/build-admission/build-admission.sock")
