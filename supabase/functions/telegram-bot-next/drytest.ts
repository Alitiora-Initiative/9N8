export const TRIAL_DRYTEST_URL = "https://9n8-secure-connect.vercel.app/?view=simulator";

export function trialDrytestText(): string {
  return [
    "🧪 9N8 TRIAL DRYTEST",
    "",
    "Market data: public Binance Spot candles, ticker and order-book depth.",
    "Execution: virtual fills only, with visible-depth VWAP, estimated fees and slippage.",
    "",
    "No exchange API key, connected wallet, wallet signature, real order, transfer or withdrawal is used.",
    "This sandbox does not alter your separate Paper/Live settings or grant live-trading access.",
    "",
    "Use the simulator to test the workflow with virtual funds. Real-money trading remains locked.",
  ].join("\n");
}

export function trialDrytestKeyboard() {
  return {
    inline_keyboard: [
      [{ text: "📈 Open Trial Drytest", web_app: { url: TRIAL_DRYTEST_URL } }],
      [{ text: "↩ Back to 9N8 Menu", callback_data: "n8:services" }],
    ],
  };
}
