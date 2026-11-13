import { useCallback, useEffect, useRef, useState } from "react";
import { latestOnly, postJson, responseError } from "./request";
import type { AccountSyncResult, Dashboard, OperationsSummary, PaperIntentResult, QueueItem, StrategyFillInput, StrategyFillResult, StrategyLedger, SystemStatus } from "./types";

/** Background refresh so freshness and queue state do not go stale on screen. */
export const REFRESH_INTERVAL_MS = 60_000;

type ControlData = {
  dashboard: Dashboard | null;
  operations: OperationsSummary | null;
  system: SystemStatus | null;
  /** Null until account sync has initialized the strategy ledger. */
  ledger: StrategyLedger | null;
  /** Only the first load; later refreshes keep the current page mounted. */
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  syncAccount: () => Promise<AccountSyncResult>;
  recordStrategyFill: (input: StrategyFillInput) => Promise<StrategyFillResult>;
  confirmDividend: (actionId: string, receivedAmount: string) => Promise<StrategyFillResult>;
  acknowledgeQueue: (itemId: string, reason?: string) => Promise<QueueItem>;
  resolveQueue: (itemId: string, reason?: string) => Promise<QueueItem>;
  setManualHalt: (active: boolean, reason?: string) => Promise<void>;
  selectStrategy: (version: string, mode?: string) => Promise<void>;
  createPaperIntent: (currentExposure: number, requestedExposure: number, reason: string) => Promise<PaperIntentResult>;
};

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw await responseError(response, url);
  return response.json() as Promise<T>;
}

/** Like getJson, but a 404 (resource not created yet) is null rather than an error. */
async function getOptionalJson<T>(url: string): Promise<T | null> {
  const response = await fetch(url);
  if (response.status === 404) return null;
  if (!response.ok) throw await responseError(response, url);
  return response.json() as Promise<T>;
}

/** POST and return the JSON body, or throw with the server's detail. */
async function post<T>(url: string, label: string, body?: unknown): Promise<T> {
  const response = await postJson(url, body);
  if (!response.ok) throw await responseError(response, label);
  return response.json() as Promise<T>;
}

export function useControlData(): ControlData {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [operations, setOperations] = useState<OperationsSummary | null>(null);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [ledger, setLedger] = useState<StrategyLedger | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gate = useRef(latestOnly());

  const refresh = useCallback(async () => {
    const token = gate.current.begin();
    setRefreshing(true);
    try {
      const [dashboardPayload, operationsPayload, systemPayload, ledgerPayload] = await Promise.all([
        getJson<Dashboard>("/api/v1/dashboard"),
        getJson<OperationsSummary>("/api/v1/operations/summary"),
        getJson<SystemStatus>("/api/v1/system"),
        getOptionalJson<StrategyLedger>("/api/v1/strategy-ledger"),
      ]);
      if (!gate.current.isCurrent(token)) return; // A newer refresh already started.
      setDashboard(dashboardPayload);
      setOperations(operationsPayload);
      setSystem(systemPayload);
      setLedger(ledgerPayload);
      setError(null);
    } catch (reason: unknown) {
      if (!gate.current.isCurrent(token)) return;
      setError(reason instanceof Error ? reason.message : "Unknown error");
    } finally {
      if (gate.current.isCurrent(token)) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const syncAccount = useCallback(async () => {
    const result = await post<AccountSyncResult>("/api/v1/account/sync", "帳戶同步失敗");
    await refresh();
    return result;
  }, [refresh]);

  const recordStrategyFill = useCallback(async (input: StrategyFillInput) => {
    const result = await post<StrategyFillResult>("/api/v1/strategy-ledger/fills", "成交回報失敗", input);
    await refresh();
    return result;
  }, [refresh]);

  const confirmDividend = useCallback(async (actionId: string, receivedAmount: string) => {
    const result = await post<StrategyFillResult>(
      `/api/v1/strategy-ledger/dividends/${encodeURIComponent(actionId)}/confirm`,
      "配息確認失敗",
      { received_amount: receivedAmount },
    );
    await refresh();
    return result;
  }, [refresh]);

  const transitionQueue = useCallback(async (itemId: string, transition: "acknowledge" | "resolve", reason = "operator reviewed") => {
    const result = await post<QueueItem>(
      `/api/v1/operations/queue/${encodeURIComponent(itemId)}/${transition}`,
      "Queue 更新失敗",
      { reason, actor: "operator" },
    );
    await refresh();
    return result;
  }, [refresh]);

  const acknowledgeQueue = useCallback((itemId: string, reason?: string) => transitionQueue(itemId, "acknowledge", reason), [transitionQueue]);
  const resolveQueue = useCallback((itemId: string, reason?: string) => transitionQueue(itemId, "resolve", reason), [transitionQueue]);

  const setManualHalt = useCallback(async (active: boolean, reason = "operator control") => {
    await post(active ? "/api/v1/risk/manual-halt" : "/api/v1/risk/manual-halt/release", "Manual Halt 更新失敗", { reason, actor: "operator" });
    await refresh();
  }, [refresh]);

  const selectStrategy = useCallback(async (version: string, mode = "shadow") => {
    await post("/api/v1/strategies/selection", "策略切換失敗", { strategy_version: version, mode, reason: "operator selected for Shadow/Paper review", changed_by: "operator" });
    await refresh();
  }, [refresh]);

  const createPaperIntent = useCallback(async (currentExposure: number, requestedExposure: number, reason: string) => {
    const strategyVersion = operations?.strategy.strategy_version ?? dashboard?.selected_strategy.strategy_version ?? "dvl-tw-v2-shadow";
    const result = await post<PaperIntentResult>("/api/v1/paper/intents", "紙上交易模擬建立失敗", { strategy_version: strategyVersion, current_exposure: currentExposure, requested_exposure: requestedExposure, reason });
    await refresh();
    return result;
  }, [dashboard, operations, refresh]);

  return {
    dashboard,
    operations,
    system,
    ledger,
    loading,
    refreshing,
    error,
    refresh,
    syncAccount,
    recordStrategyFill,
    confirmDividend,
    acknowledgeQueue,
    resolveQueue,
    setManualHalt,
    selectStrategy,
    createPaperIntent,
  };
}
