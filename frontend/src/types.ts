export type Strategy = {
  version: string;
  as_of: string;
  target_exposure: string | null;
  valid: boolean;
  safety_state: string;
  reasons: string[];
  diagnostics: Record<string, string>;
};

export type DataSnapshot = {
  snapshot_id: string;
  kind: string;
  source: string;
  observed_at: string;
  received_at: string;
  quality: string;
};

export type Dashboard = {
  status: string;
  account_nav: string;
  exposure_ceiling: string;
  live_canary_budget: string;
  production_orders_enabled: boolean;
  strategies: Strategy[];
  snapshots: DataSnapshot[];
  alerts: string[];
  selected_strategy: StrategySelection;
  market_prices: Record<string, string>;
};

export type StrategySelection = {
  strategy_version: string;
  mode: string;
  changed_at: string;
  changed_by: string;
  reason: string;
};

export type SystemStatus = {
  status: string;
  database: string;
  production_order_submission: string;
  shioaji_mode: string;
  shioaji_secret_present: boolean;
  eligibility_gate: string;
};

export type ActivationItem = {
  key: string;
  label: string;
  status: "ready" | "required" | "blocked";
  detail: string;
};

export type OperationsSummary = {
  operator_status: "NORMAL" | "ATTENTION" | "ACTION_REQUIRED" | "BLOCKED";
  summary: string;
  next_action: string;
  production_order_submission: string;
  account: {
    mode: "planning" | "observed";
    binding_configured: boolean;
    planning_nav: string;
    strategy_nav: string | null;
    observed_nav: string | null;
    strategy_cash: string | null;
    net_contributions: string | null;
    peak_nav: string | null;
    principal_loss_amount: string | null;
    principal_loss_ratio: string | null;
    current_drawdown: string | null;
    maximum_drawdown: string | null;
    pending_settlement: string | null;
    account_masked: string | null;
    quality: string;
    observed_at: string | null;
    positions: Array<{
      instrument: string;
      quantity: string;
      last_price: string;
      market_value: string;
    }>;
    external_instruments: string[];
  };
  exposure: {
    target_exposure: string | null;
    actual_exposure: string | null;
    account_exposure: string | null;
    quality: string;
    exposure_ceiling: string;
  };
  activation_items: ActivationItem[];
  strategy: StrategySelection;
  queue: QueueItem[];
  freshness: FreshnessStatus[];
  automation: AutomationStatus[];
  manual_halt: ManualHalt;
  exposure_history: Array<{ observed_at: string; actual_exposure: string; target_exposure: string | null }>;
  strategy_readiness: StrategyReadiness;
  operations_updated_at: string | null;
  research: {
    evidence_status: string;
    selected_strategy_version: string;
    selected_status: string;
    selected_detail: string;
    research_date?: string;
    comparison?: {
      period: string;
      stages: Array<{
        id: string; label: string;
        normal: { cagr: number; cagr_gap_pp: number; drawdown: number; volatility: number };
        stress: { cagr: number; cagr_gap_pp: number; drawdown: number; volatility: number };
      }>;
    };
    records: Array<{
      id: string;
      status: string;
      period: string;
      normal_cagr: number;
      stress_cagr: number;
      report_url: string;
      identity_sha256: string;
      identity: { strategy_version: string };
    }>;
  };
};

export type StrategyReadiness = {
  strategy_version: string;
  ready: boolean;
  observed_points: number;
  required_points: number;
  missing_inputs: string[];
  detail: string;
};

export type QueueItem = {
  item_id: string;
  severity: string;
  status: string;
  title: string;
  detail: string;
  impact: string;
  next_action: string;
  evidence_refs: string[];
  first_seen_at: string;
  last_seen_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  reopened_count: number;
};

export type FreshnessStatus = {
  source_trade_date: string | null;
  expected_trade_date: string | null;
  received_at: string | null;
  contract_key: string;
  kind: string;
  source: string;
  owner: string;
  status: string;
  observed_at: string | null;
  age_minutes: number | null;
  max_age_minutes: number;
  reason: string;
  snapshot_id: string | null;
};

export type AutomationStatus = {
  automation_id: string;
  status: string;
  last_started_at: string | null;
  last_success_at: string | null;
  next_run_at: string | null;
  last_run_id: string | null;
  failure_reason: string | null;
};

export type ManualHalt = {
  active: boolean;
  reason: string | null;
  actor: string | null;
  enabled_at: string | null;
  released_at: string | null;
  release_reason: string | null;
};

export type AccountSyncResult = {
  snapshot_id: string;
  created: boolean;
  account_mode: "observed";
  account_masked: string;
  nav: string;
  strategy_nav: string;
  strategy_cash: string;
  net_contributions: string;
  peak_nav: string;
  principal_loss_amount: string;
  principal_loss_ratio: string;
  current_drawdown: string;
  maximum_drawdown: string;
  pending_settlement: string;
  actual_exposure: string | null;
  account_exposure: string | null;
  exposure_quality: string;
  reconciliation_break: boolean;
  reconciliation_reason: string | null;
  quality: string;
};

export type StrategyFillInput = {
  execution_id: string;
  instrument: "0050" | "00631L" | "00685L";
  side: "BUY" | "SELL";
  quantity: string;
  price: string;
  commission: string;
  tax: string;
  occurred_at: string;
  note: string;
};

export type StrategyFillResult = {
  created: boolean;
  ledger: {
    current_nav: string;
    cash: string;
    reconciled: boolean;
    reconciliation_reason: string | null;
  };
};

export type PaperIntentResult = {
  run_id: string;
  approved: boolean;
  maturity: string;
  recommendation: string;
  evidence: Array<{ evidence_id: string; kind: string; evidence_hash: string }>;
  allocations: Array<{ instrument: string; weight: string; assumed_exposure: string }>;
};
