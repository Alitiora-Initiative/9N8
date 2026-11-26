export const NETWORKS = {
  solana: { code: "solana", label: "Solana Mainnet", connected: true, executionEnabled: true,
    note: "The existing Solana control panel and server-side safety gates remain the only supported on-chain execution path." },
  base: { code: "base", label: "Base", connected: false, executionEnabled: false,
    note: "EVM wallet onboarding, Base RPC monitoring, risk checks and order execution are not deployed." },
  ethereum: { code: "ethereum", label: "Ethereum", connected: false, executionEnabled: false,
    note: "EVM wallet onboarding, Ethereum RPC monitoring, risk checks and order execution are not deployed." },
  bnb: { code: "bnb", label: "BNB Chain", connected: false, executionEnabled: false,
    note: "EVM wallet onboarding, BNB Chain RPC monitoring, risk checks and order execution are not deployed." },
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
