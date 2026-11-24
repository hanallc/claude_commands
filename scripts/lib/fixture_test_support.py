"""Set up nested unittest fixtures with cleanup registered first."""

from collections.abc import Callable
from typing import cast
import unittest


# Name the owner of queue requests that a test starts.
FIXTURE_OWNER = "test fixture"


# Match the callback entries stored by unittest.
type CleanupCallback = tuple[Callable[..., object], tuple[object, ...], dict[str, object]]


def _call_class_cleanup(callback: Callable[..., object], /, *args: object, **kwargs: object) -> None:
    _ = callback(*args, **kwargs)


def _run_cleanups(
    callbacks: list[CleanupCallback], call_cleanup: Callable[..., object], message: str,
) -> None:
    errors: list[BaseException] = []
    while callbacks:
        callback, args, kwargs = callbacks.pop()
        try:
            _ = call_cleanup(callback, *args, **kwargs)
        except BaseException as error:
            errors.append(error)

    for index, error in enumerate(errors):
        if not isinstance(error, Exception):
            related = errors[:index] + errors[index + 1:]
            if related:
                if error.__cause__ is not None:
                    related.insert(0, error.__cause__)
                raise error from BaseExceptionGroup(message, related)
            raise error
    if errors:
        raise BaseExceptionGroup(message, errors)


def clean_up_fixture(fixture: unittest.TestCase) -> None:
    """Run every cleanup and report its exceptions."""
    callbacks = cast(list[CleanupCallback], getattr(fixture, "_cleanups"))
    call_cleanup = cast(Callable[..., object], getattr(fixture, "_callCleanup"))
    _run_cleanups(callbacks, call_cleanup, f"{type(fixture).__name__} cleanup failed")


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
        callbacks = cast(list[CleanupCallback], getattr(fixture_class, "_class_cleanups"))
        _run_cleanups(callbacks, _call_class_cleanup, f"{fixture_class.__name__} class cleanup failed")

    _ = register_cleanup(clean_up_class)
    return fixture_class
