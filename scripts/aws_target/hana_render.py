"""Render the Hana guest files from the repository configuration."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from scripts.aws_target import hana_guest, hana_receiver
from scripts.lib import conf


class RenderArguments(argparse.Namespace):
    device: Path = Path()
    out_dir: Path = Path()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--device', type=Path, required=True)
    _ = parser.add_argument('out_dir', type=Path)
    arguments = parser.parse_args(argv, namespace=RenderArguments())
    configuration = conf.read(Path(__file__).resolve().parents[2] / 'config/hana_aws.conf')
    files = (*hana_guest.render_files(hana_guest.read_settings(configuration), device=arguments.device),
             *hana_receiver.render_files(hana_receiver.read_settings(configuration)))
    output = arguments.out_dir
    output.mkdir(parents=True, exist_ok=True)
    for file in files:
        destination = output / file.path.name
        _ = destination.write_text(file.content, encoding='utf-8')
        destination.chmod(file.mode)
    render_runtime(Path(__file__).resolve().parents[2], output / 'runtime')
    return 0


def render_runtime(checkout: Path, destination: Path) -> None:
    """Install the reviewed source closure with its package paths intact."""
    for entry in (checkout / 'scripts/aws_target/hana_render.files').read_text().splitlines():
        relative = Path(entry)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('The runtime file list contains an unsafe path')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(checkout / relative, target)
        target.chmod(0o644)
    runner = destination / 'runner.py'
    _ = runner.write_text('import sys\nfrom pathlib import Path\n'
        + 'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
        + 'from scripts.aws_target.hana_runner import main\nraise SystemExit(main())\n')
    runner.chmod(0o644)


if __name__ == '__main__':
    raise SystemExit(main())
