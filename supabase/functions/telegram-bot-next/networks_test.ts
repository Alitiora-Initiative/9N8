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

Deno.test("network registry exposes Solana, Base, Ethereum and BNB", () => {
  assert(Object.keys(NETWORKS).join(",") === "solana,base,ethereum,bnb", "unexpected network registry");
});

Deno.test("only the existing Solana path is enabled for execution", () => {
  assert(networkExecutionEnabled("solana"), "Solana should remain enabled");
  assert(!networkExecutionEnabled("base"), "Base must stay disabled");
  assert(!networkExecutionEnabled("ethereum"), "Ethereum must stay disabled");
  assert(!networkExecutionEnabled("bnb"), "BNB must stay disabled");
});

Deno.test("network lookup normalizes case and rejects unknown networks", () => {
  assert(getNetwork(" BASE ")?.code === "base", "case-insensitive lookup failed");
  assert(getNetwork("unknown") === undefined, "unknown network should not resolve");
});

Deno.test("address format recognition is narrow and does not initiate execution", () => {
  assert(isEvmAddressCandidate("0x0000000000000000000000000000000000000001"), "valid EVM address not recognized");
  assert(!isEvmAddressCandidate("0x1234"), "short EVM value must be rejected");
  assert(isSolanaAddressCandidate("11111111111111111111111111111111"), "valid Solana system address not recognized");
  assert(!isSolanaAddressCandidate("not-an-address"), "arbitrary text must not be classified as an address");
});
