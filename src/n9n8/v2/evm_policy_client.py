from __future__ import annotations

import os


def read_evm_risk_policy(rpc_url: str, contract_address: str, token_address: str, position_bps: int) -> dict[str, object]:
    """Read-only EVM policy check. This client has no transaction/signing method."""
    if not rpc_url.startswith("https://"):
        raise ValueError("EVM RPC URL must use HTTPS")
    if not 1 <= int(position_bps) <= 200:
        raise ValueError("position_bps must be within the configured 2% maximum")
    try:
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError("Install requirements-evm.txt for Web3.py") from exc
    web3=Web3(Web3.HTTPProvider(rpc_url,request_kwargs={"timeout":10}))
    if not web3.is_connected():
        raise RuntimeError("EVM RPC provider is not connected")
    contract_abi=[
        {"inputs":[],"name":"halted","outputs":[{"type":"bool","name":""}],"stateMutability":"view","type":"function"},
        {"inputs":[{"type":"address","name":"token"},{"type":"uint16","name":"positionBps"}],"name":"check","outputs":[{"type":"bool","name":""}],"stateMutability":"view","type":"function"},
    ]
    contract=web3.eth.contract(address=Web3.to_checksum_address(contract_address),abi=contract_abi)
    halted=bool(contract.functions.halted().call())
    if halted:
        return {"chain_id":int(web3.eth.chain_id),"halted":True,"approved":False}
    approved=bool(contract.functions.check(Web3.to_checksum_address(token_address),int(position_bps)).call())
    return {"chain_id":int(web3.eth.chain_id),"halted":False,"approved":approved,"read_only":True}
