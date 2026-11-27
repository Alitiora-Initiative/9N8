export const NETWORKS = {
  solana: { code: "solana", label: "Solana Mainnet", connected: true, executionEnabled: true,
    note: "The existing Solana control panel and server-side safety gates remain the only supported on-chain execution path." },
  base: { code: "base", label: "Base", connected: false, executionEnabled: false,
    note: "WalletConnect pairing and address ownership verification are available in the secure wallet app. Base RPC market monitoring, token-risk checks, swap routing, trade signing and receipt reconciliation are not connected." },
  ethereum: { code: "ethereum", label: "Ethereum", connected: false, executionEnabled: false,
    note: "WalletConnect pairing and address ownership verification are available in the secure wallet app. Ethereum RPC market monitoring, token-risk checks, swap routing, trade signing and receipt reconciliation are not connected." },
  bnb: { code: "bnb", label: "BNB Smart Chain", connected: false, executionEnabled: false,
    note: "WalletConnect pairing and address ownership verification are available in the secure wallet app. BNB RPC market monitoring, token-risk checks, PancakeSwap routing, trade signing and receipt reconciliation are not connected." },
  robinhood: { code: "robinhood", label: "Robinhood / Equities", connected: false, executionEnabled: false,
    note: "Robinhood is a brokerage service, not a blockchain. No approved Robinhood OAuth/API brokerage connector or verified tokenized-equity route is connected." },
} as const;

export type NetworkCode = keyof typeof NETWORKS;

export function getNetwork(code: string) {
  const key = code.trim().toLowerCase() as NetworkCode;
  return Object.prototype.hasOwnProperty.call(NETWORKS, key) ? NETWORKS[key] : undefined;
}

export function networkExecutionEnabled(code: string): boolean {
  return getNetwork(code)?.executionEnabled === true;
}

export function isEvmAddressCandidate(value: string): boolean {
  return /^0x[a-fA-F0-9]{40}$/.test(value.trim());
}

export function isSolanaAddressCandidate(value: string): boolean {
  return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value.trim());
}
