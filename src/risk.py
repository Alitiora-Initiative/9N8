"""Fail-closed controls. Trading stays disabled until a reviewed executor exists."""
from dataclasses import dataclass


@dataclass
class RiskPolicy:
    paused: bool = False
    autotrade: bool = False

    def validate(self, proposal: dict) -> tuple[bool, str]:
        if self.paused:
            return False, "Trading is paused."
        if self.autotrade:
            return False, "Automated execution is not implemented in this release."
        if not isinstance(proposal, dict) or not proposal.get("symbol"):
            return False, "Invalid proposal."
        return False, "No order executor is installed. Live trading is disabled."


policy = RiskPolicy()
