"""Use a fixed Console text verdict in filing tests."""
from collections.abc import Generator
from contextlib import contextmanager
import runpy
import sys
from unittest.mock import patch

from scripts.notify import review


def _accept(_key: review.QuestionKey, words: str, *_args: object, **_options: object) -> review.QuestionAccepted:
    return review.QuestionAccepted(words)


@contextmanager
def accepting_console_text() -> Generator[None]:
    """Accept model requests and keep the fast checks active."""
    with patch.object(review, "review_question", new=_accept):
        yield


if __name__ == "__main__":
    sys.argv = sys.argv[1:]
    with accepting_console_text():
        _ = runpy.run_path(sys.argv[0], run_name="__main__")
