import { formatMoney } from "./format";
import type { OperationsSummary } from "./types";

/** Gaps within this band are treated as "at target". */
export const GAP_TOLERANCE = 0.05;

export type DecisionInput = {
  account: Pick<OperationsSummary["account"], "mode" | "strategy_nav" | "planning_nav">;
  exposure: Pick<OperationsSummary["exposure"], "target_exposure" | "actual_exposure">;
  strategy_readiness: Pick<OperationsSummary["strategy_readiness"], "ready" | "detail">;
};

export type Decision = {
  /** target − actual: positive means exposure would be increased. */
  gap: number | null;
  headline: string;
  detail: string;
  recommendation: string;
  direction: string;
  nextStep: string;
  canPreviewPaper: boolean;
};

/** The one sign convention for the console: target − actual. */
export function exposureGap(target: string | null, actual: string | null): number | null {
  return target === null || actual === null ? null : Number(target) - Number(actual);
}

export function deriveDecision(operations: DecisionInput): Decision {
  const { account, exposure } = operations;
  const ready = operations.strategy_readiness.ready;
  const planning = account.mode === "planning";
  const actualUnknown = exposure.actual_exposure === null;
  const gap = exposureGap(exposure.target_exposure, exposure.actual_exposure);
  const increase = gap !== null && gap > GAP_TOLERANCE;
  const reduce = gap !== null && gap < -GAP_TOLERANCE;
  const strategyNav = account.strategy_nav ?? account.planning_nav;

  const headline = planning
    ? "先完成帳戶同步"
    : !ready
      ? "策略尚未完成評估"
      : actualUnknown
        ? "暫停曝險判斷"
        : "維持監控，不送出訂單";
  const detail = planning
    ? "目前只有規劃資產，Observed NAV 與實際曝險尚未成立。"
    : !ready
      ? operations.strategy_readiness.detail
      : actualUnknown
        ? "帳戶或 ETF 曝險資料尚未完成對帳，平台不會產生交易動作。"
        : `目標曝險 ${Number(exposure.target_exposure ?? 0).toFixed(2)}x；策略曝險 ${Number(exposure.actual_exposure).toFixed(2)}x（以策略資本 ${formatMoney(strategyNav)} 為分母）。帳戶總曝險僅供參考，正式下單路徑仍為關閉。`;
  const recommendation = gap === null
    ? ready ? "等待完整帳戶資料" : "策略尚未完成評估，先補齊資料"
    : increase
      ? `實際曝險低於目標 ${gap.toFixed(2)}x：記錄為增加曝險候選，目前不下單`
      : reduce
        ? `實際曝險高於目標 ${Math.abs(gap).toFixed(2)}x：記錄為降低曝險候選，目前不下單`
        : "實際曝險接近目標：維持監控";
  const direction = !ready
    ? "等待策略資料完整"
    : gap === null
      ? "等待帳戶與策略資料"
      : increase
        ? "買入／增加允許標的（提高曝險）"
        : reduce
          ? "賣出／轉回現金或 1x（降低曝險）"
          : "不調整（已接近目標）";
  const nextStep = planning
    ? "到「帳務」頁按同步唯讀帳戶"
    : !ready
      ? "到「資料」頁執行歷史回補，直到顯示 120 個月完整度"
      : actualUnknown
        ? "檢查資料品質與對帳警示"
        : "查看「策略」頁的理由、回測與敏感度";

  return {
    gap,
    headline,
    detail,
    recommendation,
    direction,
    nextStep,
    canPreviewPaper: ready && (increase || reduce),
  };
}
