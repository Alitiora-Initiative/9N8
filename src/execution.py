"""Deliberately non-functional execution boundary for this market-scanner release.

There are no withdrawal, transfer, wallet-sweep or arbitrary API-call methods here.
Do not add a live adapter until provider permissions, order sizing, order state,
reconciliation, kill-switches and integration tests have been reviewed.
"""

class ExecutionDisabled(RuntimeError):
    pass


class TradeExecutor:
    def place_order(self, *_args, **_kwargs):
        raise ExecutionDisabled(
            "Order execution is disabled. This release only scans real market data."
        )
