import {
  getNetwork,
  isEvmAddressCandidate,
  isSolanaAddressCandidate,
  NETWORKS,
  networkExecutionEnabled,
} from "./networks.ts";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

Deno.test("network registry exposes Solana, Base, Ethereum, BNB and Robinhood brokerage status", () => {
  assert(Object.keys(NETWORKS).join(",") === "solana,base,ethereum,bnb,robinhood", "unexpected network registry");
  assert(NETWORKS.robinhood.executionEnabled === false, "Robinhood is a brokerage and must not be treated as an available chain adapter");
});

Deno.test("only the existing Solana path is enabled for execution", () => {
  assert(networkExecutionEnabled("solana"), "Solana should remain enabled");
  assert(!networkExecutionEnabled("base"), "Base must stay disabled");
  assert(!networkExecutionEnabled("ethereum"), "Ethereum must stay disabled");
  assert(!networkExecutionEnabled("bnb"), "BNB must stay disabled");
  assert(!networkExecutionEnabled("robinhood"), "Robinhood brokerage must stay disabled until its connector is certified");
});

Deno.test("network lookup normalizes case and rejects unknown networks", () => {
  assert(getNetwork(" BASE ")?.code === "base", "case-insensitive lookup failed");
  assert(getNetwork("unknown") === undefined, "unknown network should not resolve");
  assert(getNetwork(" ROBINHOOD ")?.label === "Robinhood / Equities", "Robinhood brokerage lookup failed");
});

Deno.test("address format recognition is narrow and does not initiate execution", () => {
  assert(isEvmAddressCandidate("0x0000000000000000000000000000000000000001"), "valid EVM address not recognized");
  assert(!isEvmAddressCandidate("0x1234"), "short EVM value must be rejected");
  assert(isSolanaAddressCandidate("11111111111111111111111111111111"), "valid Solana system address not recognized");
  assert(!isSolanaAddressCandidate("not-an-address"), "arbitrary text must not be classified as an address");
});
