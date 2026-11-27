"""Read named wait limits from waits.conf."""
from dataclasses import dataclass
from enum import StrEnum

from scripts.lib import conf


class WaitKind(StrEnum):
    LOCK = "lock.seconds"
    NETWORK_CALL = "network_call.seconds"
    STOP_GRACE = "stop_grace.seconds"
    STALL_ALERT = "stall_alert.minutes"


class WaitSize(StrEnum):
    FAST = "size.fast.seconds"
    SHORT = "size.short.seconds"
    STANDARD = "size.standard.seconds"
    MEDIUM = "size.medium.seconds"
    LONG = "size.long.seconds"
    EXTENDED = "size.extended.seconds"
    MAXIMUM = "size.maximum.seconds"


class OwnWait(StrEnum):
    STEVIE_POOL = "stevie_pool.seconds"


type WaitKey = WaitKind | WaitSize | OwnWait


@dataclass(frozen=True)
class WaitLimit:
    key: WaitKey
    seconds: float

    def limit_message(self, operation: str) -> str:
        return f"Waited {self.seconds:g} seconds for {operation} (waits.conf {self.key})."


def read(key: WaitKey) -> WaitLimit:
    """Read a wait in seconds. Raise conf.Unusable when the key has no usable value."""
    settings = conf.read_or_repository("waits")
    if isinstance(key, OwnWait):
        seconds = settings.number(key)
    else:
        seconds = settings.value(key, conf.positive_whole_seconds)
    if key is WaitKind.STALL_ALERT:
        seconds *= 60
    return WaitLimit(key, seconds)
