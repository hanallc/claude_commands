"""Start a Review Links test server with a defined recovery lifetime."""
from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import serve


class ReviewLinksTestServer(serve.ReviewLinksServer):
    @override
    def service_actions(self) -> None:
        """Let tests control when server maintenance runs."""


@contextmanager
def started_review_server(address: tuple[str, int], root: Path, *,
                          initialize_review: bool = False) -> Generator[serve.ReviewLinksServer]:
    """Finish startup recovery before handing the server to the test."""
    constructor_thread = threading.current_thread()
    startup_workers: list[threading.Thread] = []

    class StartupRecoveryThread(threading.Thread):
        @override
        def start(self) -> None:
            super().start()
            if threading.current_thread() is constructor_thread:
                startup_workers.append(self)

    # The Review cache creates and closes its own worker.
    with patch.object(serve, "threading", wraps=threading, Thread=StartupRecoveryThread):
        server = ReviewLinksTestServer(address, root, initialize_review=initialize_review)
    with server:
        for worker in startup_workers:
            worker.join(TEST_HANG_GUARD_SECS)
            if worker.is_alive():
                raise AssertionError("Startup recovery did not finish.")
        yield server
