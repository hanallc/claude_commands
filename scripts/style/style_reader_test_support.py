"""Select the Python reader oracle for tests and their child processes."""

from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from scripts.style import style_config


@contextmanager
def oracle_reader() -> Generator[Path]:
    """Select Python and provide an oracle for tests that explicitly select Rust."""
    with tempfile.TemporaryDirectory(prefix="style-reader-oracle-") as temporary:
        binary = Path(temporary) / "style-reader"
        checkout = Path(__file__).resolve().parents[2]
        _ = binary.write_text(
            f"#!{sys.executable}\n"
            + "import sys\n"
            + f"sys.path.insert(0, {str(checkout)!r})\n"
            + "from scripts.style.style_reader_oracle import main\n"
            + "raise SystemExit(main())\n"
        )
        binary.chmod(0o755)
        with (patch.dict(os.environ, {"STYLE_READER_BINARY": str(binary),
                                      "STYLE_TEST_READER_SOURCE": style_config.ReaderSource.PYTHON.value}),
              patch.object(style_config, "refill_reader", return_value=style_config.ReaderSource.PYTHON)):
            yield binary
