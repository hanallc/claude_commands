"""Name the model-operation variables that no agent or server start may inherit."""
from __future__ import annotations

from collections.abc import Mapping

OPERATION_ENVIRONMENT = ("MODELS_LAUNCH_CONFIGURATION", "MODELS_REGISTRY_EDIT_BASE")


def agent_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove operation state before an agent or server starts."""
    return {key: value for key, value in environment.items()
            if key not in (*OPERATION_ENVIRONMENT, "STAFF_OPERATION_FD")}
