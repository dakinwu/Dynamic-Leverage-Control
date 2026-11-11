import { describe, expect, it } from "vitest";
import { deriveDecision, exposureGap, type DecisionInput } from "./decision";

function input(overrides: {
  mode?: "planning" | "observed";
  ready?: boolean;
  target?: string | null;
  actual?: string | null;
} = {}): DecisionInput {
  return {
    account: { mode: overrides.mode ?? "observed", strategy_nav: "500000", planning_nav: "500000" },
    exposure: {
      target_exposure: overrides.target === undefined ? "1.2" : overrides.target,
      actual_exposure: overrides.actual === undefined ? "1.0" : overrides.actual,
    },
    strategy_readiness: { ready: overrides.ready ?? true, detail: "需要 120 個有效估值月點" },
  };
}

describe("exposureGap", () => {
  it("is target minus actual, so a positive gap means increase", () => {
    expect(exposureGap("1.2", "1.0")).toBeCloseTo(0.2);
    expect(exposureGap("1.0", "1.3")).toBeCloseTo(-0.3);
  });

  it("is unknown when either side is unknown", () => {
    expect(exposureGap(null, "1.0")).toBeNull();
    expect(exposureGap("1.2", null)).toBeNull();
  });
});

describe("deriveDecision", () => {
  it("asks for an account sync first in planning mode", () => {
    const decision = deriveDecision(input({ mode: "planning" }));
    expect(decision.headline).toBe("先完成帳戶同步");
    expect(decision.nextStep).toBe("到「帳務」頁按同步唯讀帳戶");
  });

  it("waits for strategy data before any direction", () => {
    const decision = deriveDecision(input({ ready: false }));
    expect(decision.headline).toBe("策略尚未完成評估");
    expect(decision.detail).toBe("需要 120 個有效估值月點");
    expect(decision.direction).toBe("等待策略資料完整");
    expect(decision.canPreviewPaper).toBe(false);
  });

  it("pauses the exposure judgement while actual exposure is unknown", () => {
    const decision = deriveDecision(input({ actual: null }));
    expect(decision.headline).toBe("暫停曝險判斷");
    expect(decision.gap).toBeNull();
    expect(decision.recommendation).toBe("等待完整帳戶資料");
    expect(decision.direction).toBe("等待帳戶與策略資料");
    expect(decision.nextStep).toBe("檢查資料品質與對帳警示");
  });

  it("points to an increase when actual is below target", () => {
    const decision = deriveDecision(input({ target: "1.2", actual: "1.0" }));
    expect(decision.headline).toBe("維持監控，不送出訂單");
    expect(decision.recommendation).toBe("實際曝險低於目標 0.20x：記錄為增加曝險候選，目前不下單");
    expect(decision.direction).toBe("買入／增加允許標的（提高曝險）");
    expect(decision.canPreviewPaper).toBe(true);
  });

  it("points to a reduction when actual is above target", () => {
    const decision = deriveDecision(input({ target: "1.0", actual: "1.3" }));
    expect(decision.recommendation).toBe("實際曝險高於目標 0.30x：記錄為降低曝險候選，目前不下單");
    expect(decision.direction).toBe("賣出／轉回現金或 1x（降低曝險）");
  });

  it("holds inside the 0.05x tolerance", () => {
    const decision = deriveDecision(input({ target: "1.2", actual: "1.17" }));
    expect(decision.recommendation).toBe("實際曝險接近目標：維持監控");
    expect(decision.direction).toBe("不調整（已接近目標）");
    expect(decision.canPreviewPaper).toBe(false);
  });
});
