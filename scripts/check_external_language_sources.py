from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "platforms/metatrader4/9N8SignalBridge.mq4": ["void OnTick()", "WebRequest("],
    "platforms/metatrader5/9N8SignalBridge.mq5": ["void OnTick()", "WebRequest("],
    "platforms/tradingview/9N8_ResearchSignals.pine": ["//@version=6", "ta.atr(", "alertcondition("],
    "contracts/solidity/9N8RiskPolicy.sol": ["pragma solidity ^0.8.26", "function check(", "modifier onlyOwner"],
    "services/native-risk/src/orderbook.cpp": ["analyze_book", "estimate_market_impact_bps"],
    "services/solana-core/src/lib.rs": ["constant_product_quote", "SignatureWindow"],
    "services/market-gateway/main.go": ["GET /healthz", "POST /v1/ohlcv/normalize"],
}
for relative, markers in REQUIRED.items():
    source = (ROOT / relative).read_text(encoding="utf-8")
    for marker in markers:
        if marker not in source:
            raise SystemExit(f"{relative}: missing {marker!r}")
for relative in ("platforms/metatrader4/9N8SignalBridge.mq4","platforms/metatrader5/9N8SignalBridge.mq5"):
    source = (ROOT / relative).read_text(encoding="utf-8")
    if "OrderSend(" in source or "CTrade trade" in source or "trade.Buy(" in source:
        raise SystemExit(f"{relative}: signal-only module contains broker order placement")
print("External language source contracts: PASS")
