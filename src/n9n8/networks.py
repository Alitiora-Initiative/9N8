"""Network availability exposed by the 9N8 Telegram control interface.

Only Solana has an implemented wallet, launch listener, token-risk gate, swap
executor, and position monitor in the current runtime. EVM entries are listed
for visibility but intentionally remain non-executable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NetworkDefinition:
    code: str
    name: str
    native_symbol: str
    dex_targets: str
    execution_enabled: bool
    execution_note: str


NETWORKS: tuple[NetworkDefinition, ...] = (
    NetworkDefinition(
        code="solana",
        name="Solana Mainnet",
        native_symbol="SOL",
        dex_targets="Pump.fun · Raydium",
        execution_enabled=True,
        execution_note="Solana wallet, Helius listener, token-risk gate, Jupiter router, Jito submission, and position monitor are implemented.",
    ),
    NetworkDefinition(
        code="base",
        name="Base",
        native_symbol="ETH",
        dex_targets="Uniswap V2/V3 · Aerodrome",
        execution_enabled=False,
        execution_note="Base RPC, encrypted EVM wallet, pool listener, GoPlus/Honeypot checks, DEX router, and position monitor are not wired into this runtime.",
    ),
    NetworkDefinition(
        code="ethereum",
        name="Ethereum",
        native_symbol="ETH",
        dex_targets="Uniswap V2/V3",
        execution_enabled=False,
        execution_note="Ethereum RPC, encrypted EVM wallet, pool listener, GoPlus/Honeypot checks, DEX router, and position monitor are not wired into this runtime.",
    ),
    NetworkDefinition(
        code="bnb",
        name="BNB Chain",
        native_symbol="BNB",
        dex_targets="PancakeSwap · Four.meme",
        execution_enabled=False,
        execution_note="BNB Chain RPC, encrypted EVM wallet, pool listener, GoPlus/Honeypot checks, DEX router, and position monitor are not wired into this runtime.",
    ),
)

_NETWORKS_BY_CODE = {network.code: network for network in NETWORKS}


def get_network(code: str) -> NetworkDefinition | None:
    return _NETWORKS_BY_CODE.get(str(code).strip().lower())


def execution_enabled(code: str) -> bool:
    network = get_network(code)
    return bool(network and network.execution_enabled)
