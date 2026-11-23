"""Publish settings from the shared Go button."""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import re
from subprocess import run

from scripts.lib import conf
from scripts.review_links.accounts_page import TUNE, Refused, Saved, SaveFailed

PAGE_PATH = "/console/tune"
DECIMAL_VALUE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")


@dataclass(frozen=True)
class TunableSetting:
    """One setting that a page can publish within a range and step."""

    document: str
    page_title: str
    minimum: Decimal
    maximum: Decimal
    step: Decimal

    def validate(self, value: str) -> Decimal | Refused:
        if len(value) > 64 or not DECIMAL_VALUE.fullmatch(value):
            return Refused("Enter a decimal number.")
        number = Decimal(value)
        if not self.minimum <= number <= self.maximum:
            return Refused(f"Enter a value from {self.minimum} to {self.maximum}.")
        if (Fraction(number) - Fraction(self.minimum)) % Fraction(self.step):
            return Refused(f"Use steps of {self.step} from {self.minimum}.")
        return number


SETTINGS: dict[tuple[str, str], TunableSetting] = {}


@dataclass(frozen=True)
class TuneOutcome:
    """The tune result and the live setting read after the attempt."""

    result: Saved | Refused | SaveFailed
    live: conf.UsableLiveValue[str] | conf.UnavailableValue


def save(page: str, key: str, value: str) -> TuneOutcome:
    """Check the registered setting, publish it, and read its live value."""
    if (setting := SETTINGS.get((page, key))) is None:
        reason = "This page cannot change that setting."
        return TuneOutcome(Refused(reason), conf.UnavailableValue(reason))
    checked = setting.validate(value)
    result = checked if isinstance(checked, Refused) else _publish(setting, key, format(checked, "f"))
    live = conf.read(setting.document).origin(key, _live_decimal)
    if isinstance(live, conf.RecoveredValue):
        live = conf.UnavailableValue(live.reason)
    return TuneOutcome(result, live)


def _live_decimal(value: str) -> str:
    if len(value) > 64 or not DECIMAL_VALUE.fullmatch(value):
        raise ValueError("The live setting is not a decimal number.")
    return value


def _publish(setting: TunableSetting, key: str, value: str) -> Saved | SaveFailed:
    try:
        done = run((*TUNE, setting.document, key, value,
                    "--why", f"The user confirmed the change with Go on the {setting.page_title} page.",
                    "--user-words", f"{key} set to {value} with Go on the {setting.page_title} page"),
                   capture_output=True, text=True, check=False)
    except OSError as error:
        return SaveFailed(" ".join(str(error).split()))
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        return SaveFailed(" ".join(lines[-1].split()) if lines else f"Tune exited {done.returncode}.")
    return Saved()
