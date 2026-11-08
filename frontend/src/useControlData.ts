import { useCallback, useEffect, useState } from "react";
import type { AccountSyncResult, Dashboard, OperationsSummary, PaperIntentResult, QueueItem, StrategyFillInput, StrategyFillResult, SystemStatus } from "./types";

type ControlData = {
  dashboard: Dashboard | null;
  operations: OperationsSummary | null;
  system: SystemStatus | null;
  loading: boolean;
  error: string | null;
  syncing: boolean;
  syncMessage: string | null;
  refresh: () => Promise<void>;
  syncAccount: () => Promise<void>;
  recordStrategyFill: (input: StrategyFillInput) => Promise<StrategyFillResult>;
  acknowledgeQueue: (itemId: string, reason?: string) => Promise<QueueItem>;
  resolveQueue: (itemId: string, reason?: string) => Promise<QueueItem>;
  setManualHalt: (active: boolean, reason?: string) => Promise<void>;
  selectStrategy: (version: string, mode?: string) => Promise<void>;
  createPaperIntent: (currentExposure: number, requestedExposure: number, reason: string) => Promise<PaperIntentResult>;
};

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json() as Promise<T>;
}

export function useControlData(): ControlData {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [operations, setOperations] = useState<OperationsSummary | null>(null);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [dashboardPayload, operationsPayload, systemPayload] = await Promise.all([
        getJson<Dashboard>("/api/v1/dashboard"),
        getJson<OperationsSummary>("/api/v1/operations/summary"),
        getJson<SystemStatus>("/api/v1/system"),
      ]);
      setDashboard(dashboardPayload);
      setOperations(operationsPayload);
      setSystem(systemPayload);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const syncAccount = useCallback(async () => {
    setSyncing(true);
    setError(null);
    setSyncMessage(null);
    try {
      const response = await fetch("/api/v1/account/sync", { method: "POST" });
      if (!response.ok) throw new Error(`帳戶同步失敗 (${response.status})`);
      const payload = await response.json() as AccountSyncResult;
      setSyncMessage(`已同步 ${payload.account_masked}`);
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "帳戶同步失敗");
    } finally {
      setSyncing(false);
    }
  }, [refresh]);

  const recordStrategyFill = useCallback(async (input: StrategyFillInput) => {
    const response = await fetch("/api/v1/strategy-ledger/fills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: string } | null;
      throw new Error(body?.detail ?? `成交回報失敗 (${response.status})`);
    }
    const result = await response.json() as StrategyFillResult;
    await refresh();
    return result;
  }, [refresh]);

  const transitionQueue = useCallback(async (itemId: string, transition: "acknowledge" | "resolve", reason = "operator reviewed") => {
    const response = await fetch(`/api/v1/operations/queue/${encodeURIComponent(itemId)}/${transition}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, actor: "operator" }),
    });
    if (!response.ok) throw new Error(`Queue 更新失敗 (${response.status})`);
    const payload = await response.json() as QueueItem;
    await refresh();
    return payload;
  }, [refresh]);

  const acknowledgeQueue = useCallback((itemId: string, reason?: string) => transitionQueue(itemId, "acknowledge", reason), [transitionQueue]);
  const resolveQueue = useCallback((itemId: string, reason?: string) => transitionQueue(itemId, "resolve", reason), [transitionQueue]);

  const setManualHalt = useCallback(async (active: boolean, reason = "operator control") => {
    const response = await fetch(active ? "/api/v1/risk/manual-halt" : "/api/v1/risk/manual-halt/release", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, actor: "operator" }),
    });
    if (!response.ok) throw new Error(`Manual Halt 更新失敗 (${response.status})`);
    await refresh();
  }, [refresh]);

  const selectStrategy = useCallback(async (version: string, mode = "shadow") => {
    const response = await fetch("/api/v1/strategies/selection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy_version: version, mode, reason: "operator selected for Shadow/Paper review", changed_by: "operator" }),
    });
    if (!response.ok) throw new Error(`策略切換失敗 (${response.status})`);
    await refresh();
  }, [refresh]);

  const createPaperIntent = useCallback(async (currentExposure: number, requestedExposure: number, reason: string) => {
    const strategyVersion = operations?.strategy.strategy_version ?? dashboard?.selected_strategy.strategy_version ?? "dvl-tw-v2-shadow";
    const response = await fetch("/api/v1/paper/intents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy_version: strategyVersion, current_exposure: currentExposure, requested_exposure: requestedExposure, reason }),
    });
    if (!response.ok) throw new Error(`紙上交易模擬建立失敗 (${response.status})`);
    const payload = await response.json() as PaperIntentResult;
    await refresh();
    return payload;
  }, [dashboard, operations, refresh]);

  return {
    dashboard,
    operations,
    system,
    loading,
    error,
    syncing,
    syncMessage,
    refresh,
    syncAccount,
    recordStrategyFill,
    acknowledgeQueue,
    resolveQueue,
    setManualHalt,
    selectStrategy,
    createPaperIntent,
  };
}
