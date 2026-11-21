"""Set up nested unittest fixtures with cleanup registered first."""

from collections.abc import Callable
from types import TracebackType
from typing import cast
import unittest
from unittest.mock import patch


def clean_up_fixture(fixture: unittest.TestCase) -> None:
    """Report every exception from the fixture's cleanup callbacks."""
    errors: list[BaseException] = []
    call_cleanup = cast(Callable[..., None], getattr(fixture, "_callCleanup"))

    def collect_cleanup_error(*args: object, **kwargs: object) -> None:
        try:
            call_cleanup(*args, **kwargs)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            errors.append(error)

    # Standalone unittest fixtures discard exceptions when no result is active.
    with patch.object(fixture, "_callCleanup", collect_cleanup_error):
        succeeded = fixture.doCleanups()
    message = f"{type(fixture).__name__} cleanup failed"
    if errors:
        raise BaseExceptionGroup(message, errors)
    if not succeeded:
        raise AssertionError(message)


def set_up_fixture[Fixture: unittest.TestCase](
    register_cleanup: Callable[[Callable[[], None]], object],
    fixture: Fixture,
) -> Fixture:
    """Register the fixture's cleanup before setup can fail."""
    _ = register_cleanup(lambda: clean_up_fixture(fixture))
    fixture.setUp()
    return fixture


def register_fixture_class_cleanup[Fixture: unittest.TestCase](
    register_cleanup: Callable[[Callable[[], None]], object],
    fixture_class: type[Fixture],
) -> type[Fixture]:
    """Register cleanup and report errors from the nested fixture class."""
    def clean_up_class() -> None:
        fixture_class.doClassCleanups()
        records = cast(list[tuple[type[Exception], Exception, TracebackType]],
                       getattr(fixture_class, "tearDown_exceptions"))
        errors = [error for _, error, _ in records]
        if errors:
            raise ExceptionGroup(f"{fixture_class.__name__} class cleanup failed", errors)

    _ = register_cleanup(clean_up_class)
    return fixture_class
