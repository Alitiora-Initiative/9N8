import {
  TRIAL_DRYTEST_URL,
  trialDrytestKeyboard,
  trialDrytestText,
} from "./drytest.ts";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

Deno.test("trial drytest opens the existing public-market simulator", () => {
  assert(
    TRIAL_DRYTEST_URL === "https://9n8-secure-connect.vercel.app/?view=simulator",
    "trial drytest URL must stay on the known simulator route",
  );
  assert(
    JSON.stringify(trialDrytestKeyboard()).includes(TRIAL_DRYTEST_URL),
    "button must open the same simulator URL",
  );
});

Deno.test("trial drytest clearly disclaims real-money execution", () => {
  const text = trialDrytestText();
  assert(text.includes("public Binance Spot"), "must identify the real public market-data source");
  assert(text.includes("virtual fills only"), "must say execution is virtual");
  assert(text.includes("No exchange API key"), "must say credentials are not used");
  assert(text.includes("real order"), "must explicitly disclaim real orders");
  assert(text.includes("does not alter your separate Paper/Live settings"), "must preserve Paper/Live separation");
});
