"""The Builds page's files: its source, its published folder and its data file. The Disks page reads them too."""
from __future__ import annotations

import os
from pathlib import Path

from scripts.lib import conf

PAGE_NAME = "memory_live.html"
DATA_NAME = "memory.json"
PAGE_SOURCE = Path(__file__).with_name(PAGE_NAME)


def configured_output_folder(settings: conf.ConfigRead) -> Path:
    """Expand the configured page folder."""
    return Path(os.path.expandvars(settings.text("output_folder"))).expanduser()
