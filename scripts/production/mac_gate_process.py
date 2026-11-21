"""Load the assigned Mac gate runner."""

from importlib import import_module
from pathlib import Path
from typing import cast

from scripts.production.gate_routes import MacGateRoute


MAC_GATE_MODULE = "scripts.mac_test.mac_gate"
MAC_GATE_SCRIPT = Path(*MAC_GATE_MODULE.split(".")).with_suffix(".py")


def load_mac_gate_route(module: str) -> MacGateRoute:
    return cast(MacGateRoute, cast(object, import_module(module)))
