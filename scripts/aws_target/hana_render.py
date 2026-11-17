"""Render the Hana guest files from the repository configuration."""
from __future__ import annotations

import argparse
from pathlib import Path

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
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
