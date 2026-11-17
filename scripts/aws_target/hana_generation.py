"""Identify one verified Hana boot without importing controller code."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HanaHostGeneration:
    deployment: str
    start_token: str
    boot_id: str
