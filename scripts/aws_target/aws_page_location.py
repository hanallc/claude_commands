"""Find the publication folder for the AWS console pages."""

import os
from pathlib import Path

from scripts.lib import conf


def output_folder() -> Path:
    return Path(os.path.expandvars(conf.read('aws_page').text('output_folder'))).expanduser()
