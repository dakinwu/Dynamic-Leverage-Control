import { useEffect, useMemo, useState } from "react";
import { useControlData } from "./useControlData";
import type { ActivationItem, Dashboard, OperationsSummary, PaperIntentResult, QueueItem, StrategyFillInput, StrategyFillResult, SystemStatus } from "./types";

const routes = [
  { path: "/", label: "總覽", short: "總" },
  { path: "/strategies", label: "策略", short: "策" },
  { path: "/data", label: "資料", short: "資" },
  { path: "/risk", label: "風控", short: "風" },
  { path: "/account", label: "帳務", short: "帳" },
] as const;

const strategyCatalog = [
  { version: "constant-1.2-v1", label: "固定 1.2x", mode: "paper" },
  { version: "constant-2.0-v1", label: "固定 2.0x", mode: "paper" },
  { version: "dvl-tw-v2-shadow", label: "DVL-TW v2（Shadow）", mode: "shadow" },
] as const;

function allocationForExposure(target: number | null): { base: number; leveraged: number; cash: number } {
  if (target === null || !Number.isFinite(target)) return { base: 0, leveraged: 0, cash: 0 };
  const clipped = Math.max(0, Math.min(2, target));
  if (clipped <= 1) return { base: clipped, leveraged: 0, cash: 1 - clipped };
  return { base: 2 - clipped, leveraged: clipped - 1, cash: 0 };
}

const money = new Intl.NumberFormat("zh-TW", {
  style: "currency",
  currency: "TWD",
  maximumFractionDigits: 0,
});

function formatMoney(value: string | null): string {
  return value === null ? "未知" : money.format(Number(value));
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function routeFor(pathname: string): (typeof routes)[number] {
  return routes.find((route) => route.path === pathname) ?? routes[0];
}

function StatusDot({ state }: { state: "ready" | "required" | "blocked" }) {
  return <span className={`status-dot ${state}`} aria-hidden="true" />;
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

function PageHeader({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
    </header>
  );
}

function ActivationList({ items }: { items: ActivationItem[] }) {
  return (
    <ul className="health-list">
      {items.map((item) => (
        <li key={item.key}>
          <StatusDot state={item.status} />
          <span>{item.label}<small>{item.detail}</small></span>
          <strong>{item.status}</strong>
        </li>
      ))}
    </ul>
  );
}

function TermGlossary() {
  return (
    <details className="glossary">
      <summary>專有名詞小字典（點開查看）</summary>
      <div className="glossary-grid">
        <div><strong>曝險 Exposure</strong><span>帳戶對台股漲跌的敏感度。1.0x 約等於投入一倍 NAV；2.0x 約等於兩倍名目曝險。</span></div>
        <div><strong>NAV</strong><span>帳戶全部持倉加現金的總淨資產，不等於可立即買進的現金。</span></div>
        <div><strong>Target / Actual / Gap</strong><span>目標曝險／目前曝險／兩者差距。Target 大於 Actual 才是增加曝險方向。</span></div>
        <div><strong>P/E percentile</strong><span>目前本益比在自身歷史區間的位置；百分位越高代表相對越昂貴。</span></div>
        <div><strong>EPS YoY</strong><span>市場 EPS 相對約一年前的成長率，用來調整估值候選值。</span></div>
        <div><strong>RV20 / VIX</strong><span>RV20 是過去 20 個交易日實現波動；VIX 是市場對未來波動的隱含估計。</span></div>
        <div><strong>Cap</strong><span>每一層風控上限，最後取最保守者；它是限制，不是買進訊號。</span></div>
        <div><strong>DATA_UNSAFE / PANIC_LOCK</strong><span>資料不可信／恐慌鎖定。兩者都禁止系統增加曝險。</span></div>
        <div><strong>Risk Gate</strong><span>獨立於策略的安全閘門；可以放行、降低、延後或拒絕一個研究意圖。</span></div>
        <div><strong>Exposure Ceiling</strong><span>曝險硬上限，目前現金帳戶為 2.0x；策略候選超過此值只能留在 Shadow/Paper。</span></div>
        <div><strong>Manual Halt</strong><span>人工暫停新的自動意圖；資料蒐集與對帳繼續，不等於自動清算。</span></div>
        <div><strong>Fail-closed</strong><span>安全證據不足時不放行，而不是用預設值猜測或默認可以買進。</span></div>
        <div><strong>Shadow</strong><span>策略只計算與記錄，不改變 Paper 配置，也不會觸發券商動作。</span></div>
        <div><strong>Paper 預覽／紀錄</strong><span>把目標比例與方向保存成可追溯的模擬意圖；目前不會送出委託。本人完成券商成交後，再從帳務頁把成交與費用登錄至策略台帳。</span></div>
        <div><strong>Freshness Contract</strong><span>每個資料來源允許的最大年齡；超過期限就標記 STALE 並禁止增加曝險。</span></div>
        <div><strong>Operator Queue</strong><span>需要人處理的事件清單；已看（Acknowledged）不等於已解決（Resolved）。</span></div>
      </div>
    </details>
  );
}

function OverviewPage({
  operations,
  dashboard,
  onAcknowledge,
  onResolve,
  onCreatePaper,
}: {
  operations: OperationsSummary;
  dashboard: Dashboard;
  onAcknowledge: (itemId: string) => Promise<QueueItem>;
  onResolve: (itemId: string) => Promise<QueueItem>;
  onCreatePaper: (currentExposure: number, requestedExposure: number, reason: string) => Promise<PaperIntentResult>;
}) {
  const [paperMessage, setPaperMessage] = useState<string | null>(null);
  const [paperPreviewOpen, setPaperPreviewOpen] = useState(false);
  const target = operations.exposure.target_exposure;
  const actual = operations.exposure.actual_exposure;
  const unresolved = operations.queue.filter((item) => item.status !== "RESOLVED");
  const targetNumber = target === null ? null : Number(target);
  const actualNumber = actual === null ? null : Number(actual);
  const gap = targetNumber !== null && actualNumber !== null ? targetNumber - actualNumber : null;
  const oneXPosition = operations.account.positions.find((position) => position.instrument === "0050");
  const strategyNav = operations.account.strategy_nav ?? operations.account.planning_nav;
  const strategyReady = operations.strategy_readiness.ready;
  const recipe = allocationForExposure(targetNumber);
  const estimateShares = (instrument: string, weight: number): string => {
    const price = Number(dashboard.market_prices[instrument] ?? 0);
    if (!price || !weight) return "—";
    return Math.floor((Number(strategyNav) * weight) / price).toLocaleString("zh-TW");
  };
  const exposureRecommendation = gap === null
    ? strategyReady ? "等待完整帳戶資料" : "策略尚未完成評估，先補齊資料"
    : gap > 0.05
      ? `實際曝險低於目標 ${gap.toFixed(2)}x：記錄為增加曝險候選，目前不下單`
      : gap < -0.05
        ? `實際曝險高於目標 ${Math.abs(gap).toFixed(2)}x：記錄為降低曝險候選，目前不下單`
        : "實際曝險接近目標：維持監控";

  return (
    <>
      <PageHeader
        eyebrow="OPERATOR CONSOLE"
        title="今日營運總覽"
        description="先確認安全狀態與待處理事項，再查看曝險與帳戶證據。"
      />

      <section className={`operator-banner state-${operations.operator_status.toLowerCase()}`}>
        <div>
          <span className="eyebrow">OPERATOR STATUS</span>
          <strong>{operations.operator_status.replace("_", " ")}</strong>
        </div>
        <div className="operator-copy">
          <h2>{operations.summary}</h2>
          <p>下一步：{operations.next_action}</p>
        </div>
        <span className="mode-pill">{operations.account.mode === "planning" ? "PLANNING MODE" : "OBSERVED"}</span>
      </section>

      <section className="panel operator-playbook">
        <div className="panel-heading"><span>今天可以怎麼用</span><small>READ ONLY WORKFLOW</small></div>
        <div className="playbook-grid">
          <div><strong>1｜先看狀態</strong><span>確認資料庫、Shioaji 唯讀、帳戶同步與資料品質都是 ready。</span></div>
          <div><strong>2｜再看差距</strong><span>比較 Target Exposure 與 Actual Exposure；差距只代表研究上的再平衡需求。</span></div>
          <div><strong>3｜最後看建議</strong><span>到策略頁查看原因與 cap。資料不足就補資料，不要把舊目標當成新訊號。</span></div>
        </div>
      </section>

      {operations.account.mode === "planning" && (
        <section className="truth-warning">
          <strong>目前不是券商帳戶真相</strong>
          <span>下列規劃 NAV 只供設定參考；Observed NAV、Strategy Cash 與 Actual Exposure 均維持未知。</span>
        </section>
      )}

      <section className="decision-grid" aria-label="目前判斷與下一步">
        <article className={`panel decision-card ${operations.operator_status.toLowerCase()}`}>
          <div className="panel-heading"><span>目前判斷</span><small>{operations.strategy.strategy_version} · {operations.strategy.mode.toUpperCase()}</small></div>
          <h2>
            {operations.account.mode === "planning"
              ? "先完成帳戶同步"
              : !strategyReady
                ? "策略尚未完成評估"
              : operations.exposure.actual_exposure === null
                ? "暫停曝險判斷"
                : "維持監控，不送出訂單"}
          </h2>
          <p>
            {operations.account.mode === "planning"
              ? "目前只有規劃資產，Observed NAV 與實際曝險尚未成立。"
              : !strategyReady
                ? operations.strategy_readiness.detail
              : operations.exposure.actual_exposure === null
                ? "帳戶或 ETF 曝險資料尚未完成對帳，平台不會產生交易動作。"
                : `目標曝險 ${Number(operations.exposure.target_exposure ?? 0).toFixed(2)}x；策略曝險 ${Number(operations.exposure.actual_exposure).toFixed(2)}x（以策略資本 ${formatMoney(strategyNav)} 為分母）。帳戶總曝險僅供參考，正式下單路徑仍為關閉。`}
          </p>
          <div className="decision-signal">
            <span>觀察建議</span>
            <strong>{exposureRecommendation}</strong>
          </div>
          <div className="exposure-action-key">
            <span>如果真的執行，方向會是</span>
            <strong>
              {!strategyReady
                ? "等待策略資料完整"
                : gap === null
                ? "等待帳戶與策略資料"
                : gap > 0.05
                  ? "買入／增加允許標的（提高曝險）"
                  : gap < -0.05
                    ? "賣出／轉回現金或 1x（降低曝險）"
                    : "不調整（已接近目標）"}
            </strong>
            <small>這只是方向提示；本專案不會送出正式委託。</small>
            {strategyReady && gap !== null && actualNumber !== null && targetNumber !== null && Math.abs(gap) > 0.05 && (
              <button
                type="button"
                className="primary-action paper-intent-button"
                onClick={() => { setPaperMessage(null); setPaperPreviewOpen(true); }}
              >
                建立紙上調整預覽（只記錄、不下單）
              </button>
            )}
            {paperPreviewOpen && strategyReady && gap !== null && actualNumber !== null && targetNumber !== null && (
              <div className="paper-preview">
                <strong>先確認這次紙上調整</strong>
                <span>目前 {actualNumber.toFixed(2)}x → 目標 {targetNumber.toFixed(2)}x</span>
                <span>0050 {Math.round(recipe.base * 100)}% · 00631L {Math.round(recipe.leveraged * 100)}% · 現金 {Math.round(recipe.cash * 100)}%</span>
                <span>股數估算：0050 {estimateShares("0050", recipe.base)} 股 · 00631L {estimateShares("00631L", recipe.leveraged)} 股</span>
                 <small>確認後只建立策略／風控／意圖模擬證據，不會送出券商委託；費用、滑價、成交與 T+2 目前尚未計入。</small>
                <div className="paper-preview-actions">
                  <button type="button" className="primary-action" onClick={() => void onCreatePaper(actualNumber, targetNumber, "overview exposure gap review").then((result) => { setPaperMessage(`${result.recommendation} · Run ${result.run_id.slice(0, 8)}`); setPaperPreviewOpen(false); }).catch((error: unknown) => setPaperMessage(error instanceof Error ? error.message : "紙上調整建立失敗"))}>確認建立模擬紀錄</button>
                  <button type="button" className="secondary-action" onClick={() => setPaperPreviewOpen(false)}>取消</button>
                </div>
              </div>
            )}
            {paperMessage && <small className="paper-result">{paperMessage}</small>}
          </div>
          <div className="decision-next">
            <span>下一步</span>
            <strong>
              {operations.account.mode === "planning"
                ? "到「帳務」頁按同步唯讀帳戶"
                : !strategyReady
                  ? "到「資料」頁執行歷史回補，直到顯示 120 個月完整度"
                : operations.exposure.actual_exposure === null
                  ? "檢查資料品質與對帳警示"
                  : "查看「策略」頁的理由、回測與敏感度"}
            </strong>
          </div>
        </article>

        <article className="panel evidence-card">
          <div className="panel-heading"><span>判斷依據</span><small>AS OF ACCOUNT SYNC</small></div>
          <dl className="metric-list">
            <div><dt>目標曝險</dt><dd>{operations.exposure.target_exposure === null ? "—" : `${Number(operations.exposure.target_exposure).toFixed(2)}x`}</dd></div>
            <div><dt>實際曝險</dt><dd>{operations.exposure.actual_exposure === null ? "—" : `${Number(operations.exposure.actual_exposure).toFixed(2)}x`}</dd></div>
            <div><dt>策略資本（曝險分母）</dt><dd>{formatMoney(strategyNav)}</dd></div>
            <div><dt>帳戶總 NAV（含外部）</dt><dd>{formatMoney(operations.account.observed_nav)}</dd></div>
            <div><dt>帳戶總曝險（參考）</dt><dd>{operations.exposure.account_exposure === null ? "—" : `${Number(operations.exposure.account_exposure).toFixed(2)}x`}</dd></div>
            <div><dt>最新同步</dt><dd>{operations.account.observed_at ? formatTime(operations.account.observed_at) : "尚未同步"}</dd></div>
          </dl>
          {!dashboard.strategies.some((strategy) => strategy.version === "dvl-tw-v2-shadow") && dashboard.strategies[0]?.version === "constant-1.2-v1" && (
            <p className="panel-note">目前策略是 constant-1.2-v1 固定曝險版本，尚未使用 RV20、VIX 或估值訊號；因此這裡是風控觀察建議，不是行情買賣訊號。</p>
          )}
        </article>
      </section>

      <section className="panel target-instrument-card">
        <div className="panel-heading"><span>目標曝險要用哪些標的？</span><small>RESEARCH RECIPE · READ ONLY</small></div>
        <p className="panel-note">目前研究配方使用 0050（約 1x）與 00631L（約 2x）。下表是把目標曝險換成比例的示意，不是正式委託數量。</p>
        <div className="recipe-grid">
          <div><strong>0050</strong><span>1x 基準部位</span><b>{targetNumber === null ? "—" : `${Math.round(recipe.base * 100)}%`}</b></div>
          <div><strong>00631L</strong><span>2x 槓桿部位</span><b>{targetNumber === null ? "—" : `${Math.round(recipe.leveraged * 100)}%`}</b></div>
          <div><strong>現金</strong><span>不承擔市場曝險</span><b>{targetNumber === null ? "—" : `${Math.round(recipe.cash * 100)}%`}</b></div>
        </div>
        <p className="panel-note">配置公式：0–1.0x 使用 0050＋現金；1.0–2.0x 使用 0050＋00631L。這是目標比例示意，目前 Paper 只保存調整意圖；尚未計入股數、費用、滑價與結算。</p>
      </section>

      <section className="hero-grid">
        <article className="panel exposure-panel">
          <div className="panel-heading"><span>曝險控制</span><small>{operations.exposure.quality.toUpperCase()}</small></div>
          <div className="exposure-comparison">
            <div><span>目標曝險</span><strong>{target === null ? "未知" : `${Number(target).toFixed(1)}x`}</strong></div>
            <div><span>實際曝險</span><strong className={actual === null ? "unknown" : ""}>{actual === null ? "未知" : `${Number(actual).toFixed(2)}x`}</strong></div>
            <div><span>曝險差距</span><strong>{target !== null && actual !== null ? `${(Number(actual) - Number(target)).toFixed(2)}x` : "無法計算"}</strong></div>
          </div>
          <div className="range-track"><i style={{ width: target === null ? "0%" : `${(Number(target) - 1) * 100}%` }} /></div>
          <div className="range-labels"><span>1.0x</span><span>硬上限 {Number(operations.exposure.exposure_ceiling).toFixed(1)}x</span></div>
          <p className="panel-note">Strategy Exposure 要等唯讀帳戶同步、策略標的持倉與 ETF 官方曝險完成對帳後才能成立。</p>
        </article>

        <article className="panel account-panel">
          <div className="panel-heading"><span>Strategy Account</span><small>{operations.account.mode.toUpperCase()}</small></div>
          <div className="account-truth">
            <span>Observed NAV（帳戶總資產）</span>
            <strong>{formatMoney(operations.account.observed_nav)}</strong>
          </div>
          <p className="panel-note">策略曝險改以策略資本 {formatMoney(strategyNav)} 為分母；外部持倉不會稀釋策略曝險，但仍保留在帳戶總 NAV 與帳務審計中。</p>
          <dl className="metric-list">
            {operations.account.mode === "planning" && <div><dt>規劃 NAV</dt><dd>{formatMoney(operations.account.planning_nav)}</dd></div>}
            <div><dt>策略 NAV</dt><dd>{formatMoney(operations.account.strategy_nav ?? operations.account.planning_nav)}</dd></div>
            <div><dt>策略現金</dt><dd>{formatMoney(operations.account.strategy_cash)}</dd></div>
            <div><dt>本金虧損</dt><dd>{formatMoney(operations.account.principal_loss_amount)}</dd></div>
            <div><dt>歷史最大回撤</dt><dd>{operations.account.maximum_drawdown === null ? "未知" : `${(Number(operations.account.maximum_drawdown) * 100).toFixed(2)}%`}</dd></div>
            <div><dt>整體帳戶 T+2</dt><dd>{formatMoney(operations.account.pending_settlement)}</dd></div>
            <div><dt>帳戶綁定</dt><dd>{operations.account.mode === "observed" ? "已驗證" : operations.account.binding_configured ? "已設定，待驗證" : "尚未設定"}</dd></div>
          </dl>
        </article>
      </section>

      <section className="panel page-panel exposure-history-panel">
        <div className="panel-heading"><span>30 日曝險控制圖</span><small>OBSERVED ACCOUNT HISTORY</small></div>
        {operations.exposure_history.length ? (
          <div className="exposure-history" aria-label="30 day exposure history">
            {operations.exposure_history.map((point) => {
              const value = Number(point.actual_exposure);
              const height = Math.max(8, Math.min(100, (value / Number(operations.exposure.exposure_ceiling)) * 100));
              return <div className="history-bar" key={point.observed_at} title={`${formatTime(point.observed_at)} · ${value.toFixed(2)}x`}><i style={{ height: `${height}%` }} /><small>{new Date(point.observed_at).getDate()}</small></div>;
            })}
          </div>
        ) : <EmptyState>完成多次唯讀同步後，這裡會顯示實際曝險趨勢。</EmptyState>}
        <p className="panel-note">這是觀測歷史，不是績效曲線；每根柱狀代表一次成功對帳的 Actual Exposure。</p>
      </section>

      <section className="panel calculation-panel">
        <div className="panel-heading"><span>數字怎麼算</span><small>ACCOUNTING EVIDENCE</small></div>
        <div className="calculation-grid">
          <div>
            <span>Observed NAV（券商回報）</span>
            <strong>{formatMoney(operations.account.observed_nav)}</strong>
            <p>這是券商現金加上全部持倉市值，外部股票仍會包含在這個帳戶真實值。</p>
          </div>
          <div>
            <span>Strategy NAV（曝險分母）</span>
            <strong>{formatMoney(strategyNav)}</strong>
            <p>目前使用設定的策略資本；外部股票不會加入策略曝險分母。</p>
          </div>
          <div>
            <span>Strategy Exposure</span>
            <strong>{actual === null ? "—" : `${actualNumber?.toFixed(4)}x`}</strong>
            <p>
              {oneXPosition
                ? `策略範圍內的曝險名目值 ÷ ${formatMoney(strategyNav)}；0050 目前市值 ${formatMoney(oneXPosition.market_value)}，外部持倉不納入。`
                : "持有策略標的後，會依允許標的與官方 ETF 曝險資料計算。"}
            </p>
          </div>
        </div>
      </section>

      <TermGlossary />

      <section className="lower-grid">
        <article className="panel queue-panel">
          <div className="panel-heading"><span>Operator Queue</span><small>{unresolved.length} 待處理</small></div>
          {unresolved.length ? (
            <ul className="queue-list">
              {unresolved.map((item) => (
                <li key={item.item_id}>
                  <span className={`severity ${item.severity.toLowerCase()}`}>{item.severity}</span>
                  <div><strong>{item.title}</strong><small>{item.detail} · {item.next_action}</small></div>
                  <div className="queue-actions">
                    {item.status === "OPEN" || item.status === "REOPENED" ? (
                      <button type="button" onClick={() => void onAcknowledge(item.item_id)}>已看</button>
                    ) : (
                      <button type="button" onClick={() => void onResolve(item.item_id)}>標記解決</button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : <EmptyState>目前沒有待處理項目。</EmptyState>}
        </article>

        <article className="panel automation-panel">
          <div className="panel-heading"><span>自動化與資料</span><small>{operations.automation[0]?.status ?? "UNKNOWN"}</small></div>
          <dl className="metric-list">
            <div><dt>Strategy Runs</dt><dd>{dashboard.strategies.length}</dd></div>
            <div><dt>權威快照</dt><dd>{dashboard.snapshots.length}</dd></div>
            <div><dt>最弱資料源</dt><dd>{operations.freshness.find((item) => item.status !== "FRESH")?.owner ?? "全部新鮮"}</dd></div>
            <div><dt>下次排程</dt><dd>{operations.automation[0]?.next_run_at ? formatTime(operations.automation[0].next_run_at) : "待 Scheduler 心跳"}</dd></div>
            <div><dt>正式委託</dt><dd className="safe-lock">永久關閉</dd></div>
          </dl>
        </article>
      </section>
    </>
  );
}

function StrategiesPage({ dashboard, operations, onSelectStrategy }: {
  dashboard: Dashboard;
  operations: OperationsSummary;
  onSelectStrategy: (version: string, mode?: string) => Promise<void>;
}) {
  const defaultStrategy = dashboard.strategies.find((item) => item.version === "dvl-tw-v2-shadow") ?? dashboard.strategies[0] ?? null;
  const [selectedVersion, setSelectedVersion] = useState(operations.strategy.strategy_version || defaultStrategy?.version || "");
  const latest = dashboard.strategies.find((item) => item.version === selectedVersion) ?? (selectedVersion ? null : defaultStrategy);
  const target = latest?.target_exposure === null || latest?.target_exposure === undefined ? null : Number(latest.target_exposure);
  const strategyLabel = strategyCatalog.find((item) => item.version === selectedVersion)?.label ?? selectedVersion;
  const valuationRows = [
    [">= 31", "EPS × 31", "1.0", "估值偏高：降至基準"],
    ["28", "EPS × 28", "1.6", "估值回落：研究性提高"],
    ["26", "EPS × 26", "2.0", "估值中位：現金帳戶上限"],
    ["21", "EPS × 21", "3.0", "危機區：僅 Shadow/Paper"],
    ["20", "EPS × 20", "3.2", "極端低估：僅研究，不可執行"],
  ];
  const diagnostic = latest?.diagnostics ?? {};
  const inputMetric = (key: string, suffix = "") => {
    const value = diagnostic[key];
    return value === undefined || value === "missing" ? "等待資料" : `${value}${suffix}`;
  };
  return (
    <>
      <section className="strategy-explainer-grid">
        <article className="panel page-panel">
          <div className="panel-heading"><span>這個策略是什麼</span><small>{strategyLabel || "NO SELECTION"}</small></div>
          <h2>{latest && !latest.valid ? "尚未完成評估" : target === 1.2 ? "固定 1.20x 曝險" : target === 2 ? "固定 2.00x 曝險" : "策略目標曝險"}</h2>
          <p className="strategy-lead">曝險（exposure）是「資產對市場漲跌的敏感度倍數」。1.0x 約等於持有一份大盤；1.2x 代表希望承擔約 1.2 倍大盤波動，而不是保證報酬 1.2 倍。</p>
          <div className="formula-box"><code>目標曝險 = {target === null ? "未評估" : `${target.toFixed(2)}x`}</code></div>
          <p className="panel-note">{latest?.version === "dvl-tw-v2-shadow" ? "DVL-TW v2 會以 P/E percentile、EPS、RV20、VIX、回撤與融資成本逐層取最保守上限；3.2x 僅為 Shadow/Paper 候選。資料不足時不會沿用舊目標。" : "固定曝險策略不會因為今天上漲或下跌而自行改變目標；它是基準線，不是行情買賣訊號。"}</p>
        </article>
        <article className="panel page-panel">
          <div className="panel-heading"><span>怎麼讀這個頁面</span><small>DECISION GUIDE</small></div>
          <ol className="strategy-guide">
            <li><strong>目標</strong><span>策略希望維持的曝險倍數。</span></li>
            <li><strong>狀態</strong><span>NORMAL 代表策略計算本身有效，不代表現在應該買進。</span></li>
            <li><strong>理由</strong><span>列出該次策略執行採用的訊號或限制。</span></li>
            <li><strong>資料日</strong><span>這次決策使用的資料日期，與你按下同步的時間不同。</span></li>
          </ol>
        </article>
      </section>
      <section className="panel page-panel valuation-workbench">
        <div className="panel-heading"><span>估值 → 點位 → 槓桿工作表</span><small>RESEARCH HYPOTHESIS</small></div>
        <p className="panel-note">這張表重現你提供的文章邏輯：預估點位 = 市場 EPS × TTM P/E。它是研究假說，不是保證報酬；v2 實際決策仍會再套用 EPS、RV20、VIX、回撤與資金成本上限。</p>
        <div className="strategy-inputs" aria-label="Strategy inputs">
          <span><small>TTM P/E</small><strong>{inputMetric("pe_ttm")}</strong></span>
          <span><small>P/E percentile</small><strong>{inputMetric("pe_percentile")}</strong></span>
          <span><small>EPS YoY</small><strong>{inputMetric("eps_yoy")}</strong></span>
          <span><small>RV20 / VIX</small><strong>{inputMetric("rv20")} / {inputMetric("taiwan_vix")}</strong></span>
        </div>
        <div className="valuation-table" role="table">
          <div className="valuation-row valuation-head" role="row"><span>TTM P/E</span><span>預估點位公式</span><span>候選槓桿</span><span>0050 權重</span><span>00631L 權重</span><span>現金</span><span>觸發條件</span><span>本次 v2</span></div>
          {valuationRows.map(([pe, formula, leverage, trigger]) => {
            const leverageNumber = Number(leverage);
            const baseWeight = Math.max(0, Math.min(1, 2 - leverageNumber));
            const leveragedWeight = Math.max(0, Math.min(1, leverageNumber - 1));
            const cashWeight = 1 - baseWeight - leveragedWeight;
            return (
              <div className="valuation-row" role="row" key={pe}>
                <strong>{pe}</strong><span>{formula}</span><span>{leverageNumber.toFixed(1)}x</span><span>{Math.round(baseWeight * 100)}%</span><span>{Math.round(leveragedWeight * 100)}%</span><span>{Math.round(cashWeight * 100)}%</span><span>{trigger}</span><span>{latest?.version === "dvl-tw-v2-shadow" && latest.valid ? `${target?.toFixed(1)}x` : "—"}</span>
              </div>
            );
          })}
        </div>
        <div className="strategy-evidence-strip">
          <span><small>升／降槓桿原則</small><strong>P/E percentile 越高越保守；回撤、RV20、VIX、資金成本取最小上限。</strong></span>
          <span><small>回測與 Promotion Gate</small><strong>{latest?.valid ? "目前僅代表一次資料評估；研究資格請見下方各版本證據。短期觀察天數不代表收益通過。" : "等待 120 個有效估值月點後，才可產生可比較的動態目標。"}</strong></span>
        </div>
        <p className="panel-note">目前若顯示 DATA_UNSAFE，代表 P/E、EPS 或波動資料不足；請先到「資料」頁補齊，不要依表格自行下單。</p>
      </section>
      <ResearchEvidence operations={operations} />
      <PageHeader eyebrow="STRATEGY RUNS" title="策略" description="查看不可變策略版本、最新決策、理由與安全狀態。" />
      <section className="panel page-panel">
        <div className="panel-heading"><span>策略候選</span><small>INDEPENDENT LANES</small></div>
        <div className="selected-strategy-banner">
          <div><span>目前選定</span><strong>{operations.strategy.strategy_version}</strong><small>{operations.strategy.mode.toUpperCase()} · {operations.strategy.reason}</small></div>
          <button type="button" className="primary-action" onClick={() => void onSelectStrategy(selectedVersion, selectedVersion === "dvl-tw-v2-shadow" ? "shadow" : "paper")}>套用到 Shadow／Paper</button>
        </div>
        {dashboard.strategies.length ? (
          <>
          <div className="strategy-switcher" role="tablist" aria-label="策略版本">
            {strategyCatalog.map((candidate) => (
              <button
                key={candidate.version}
                type="button"
                role="tab"
                aria-selected={candidate.version === selectedVersion}
                className={candidate.version === selectedVersion ? "active" : ""}
                onClick={() => setSelectedVersion(candidate.version)}
              >
                {candidate.label}
              </button>
            ))}
          </div>
          <div className="strategy-table" role="table">
            <div className="table-row table-head" role="row"><span>版本</span><span>目標</span><span>狀態</span><span>資料日</span></div>
            {strategyCatalog.map((candidate) => {
              const strategy = dashboard.strategies.find((item) => item.version === candidate.version);
              return (
              <article className="strategy-record" key={candidate.version}>
                <div className="table-row" role="row">
                  <strong>{candidate.label}</strong>
                  <span className="exposure-value">{strategy?.target_exposure === null || strategy?.target_exposure === undefined ? "未評估" : `${Number(strategy.target_exposure).toFixed(1)}x`}</span>
                  <span className={`state-pill ${strategy ? strategy.safety_state.toLowerCase() : "pending"}`}>{strategy ? strategy.safety_state : "尚未執行"}</span>
                  <span className="muted">{strategy?.as_of ?? "—"}</span>
                </div>
                <p>{strategy ? (strategy.reasons.length ? strategy.reasons.join(" · ") : "尚無理由碼") : "尚未產生此版本的 Strategy Run；切換只會影響後續 Shadow／Paper 評估。"}</p>
                {strategy && strategy.version === latest?.version && Object.keys(strategy.diagnostics).length > 0 && (
                  <div className="strategy-diagnostics">
                    {Object.entries(strategy.diagnostics).map(([key, value]) => (
                      <span key={key}><strong>{key}</strong>{value}</span>
                    ))}
                  </div>
                )}
              </article>
              );
            })}
          </div>
          </>
        ) : <EmptyState>尚無 Strategy Run。平台不會用展示數字冒充策略訊號。</EmptyState>}
      </section>
      <TermGlossary />
    </>
  );
}

function DataPage({ operations, dashboard }: { operations: OperationsSummary; dashboard: Dashboard }) {
  const readiness = operations.strategy_readiness;
  return (
    <>
      <PageHeader eyebrow="AUTHORITATIVE DATA" title="資料" description="檢查啟用條件、資料來源、品質與實際觀測時間。" />
      <section className="content-grid">
        <article className="panel page-panel">
          <div className="panel-heading"><span>啟用清單</span><small>CAPABILITY CHECK</small></div>
          <ActivationList items={operations.activation_items} />
        </article>
        <article className="panel page-panel">
          <div className="panel-heading"><span>最近權威快照</span><small>{dashboard.snapshots.length} SNAPSHOTS</small></div>
          {dashboard.snapshots.length ? (
            <ul className="snapshot-list">
              {dashboard.snapshots.map((snapshot) => (
                <li key={snapshot.snapshot_id}>
                  <div><strong>{snapshot.kind}</strong><small>{snapshot.source}</small></div>
                  <div><span className={`quality ${snapshot.quality.toLowerCase()}`}>{snapshot.quality}</span><small>{formatTime(snapshot.observed_at)}</small></div>
                </li>
              ))}
            </ul>
          ) : <EmptyState>尚無權威資料快照；資料型策略維持 fail-closed。</EmptyState>}
        </article>
      </section>
      <section className={`panel page-panel readiness-card ${readiness.ready ? "ready" : "attention"}`}>
        <div className="panel-heading"><span>策略資料完整度</span><small>{readiness.strategy_version}</small></div>
        <div className="readiness-summary">
          <strong>{readiness.ready ? "可評估" : "尚未完成評估"}</strong>
          <span>{readiness.observed_points} / {readiness.required_points} 個有效估值月點</span>
        </div>
        <p className="panel-note">{readiness.detail}</p>
        <p className="panel-note">ETF、大盤與 VIX 的 FRESH 會核對交易日期；其他來源仍依各自期限。UNKNOWN 代表日期證據不足。資料可算不等於研究通過。</p>
      </section>
      <section className="panel page-panel freshness-panel">
        <div className="panel-heading"><span>Freshness Contract</span><small>權威來源狀態</small></div>
        <div className="freshness-table">
          <div className="freshness-row freshness-head"><span>來源</span><span>狀態</span><span>行情日期／年齡</span><span>預期日期／期限</span><span>原因</span></div>
          {operations.freshness.map((item) => (
            <div className="freshness-row" key={item.contract_key}>
              <strong>{item.owner}</strong>
              <span className={`quality ${item.status.toLowerCase()}`}>{item.status}</span>
              <span>{item.expected_trade_date ? (item.source_trade_date ?? "日期未知") : item.age_minutes === null ? "—" : `${item.age_minutes} 分鐘`}</span>
              <span>{item.expected_trade_date ?? `${item.max_age_minutes} 分鐘`}</span>
              <span className="muted">{item.reason}</span>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

function RiskPage({ operations, system, onSetManualHalt }: {
  operations: OperationsSummary;
  system: SystemStatus;
  onSetManualHalt: (active: boolean, reason?: string) => Promise<void>;
}) {
  return (
    <>
      <PageHeader eyebrow="RISK GATE" title="風控" description="安全狀態由證據解除，不能從介面直接覆寫。" />
      <section className="content-grid">
        <article className="panel page-panel">
          <div className="panel-heading"><span>安全邊界</span><small>{operations.operator_status}</small></div>
          <dl className="metric-list large">
            <div><dt>正式下單路徑</dt><dd className="safe-lock">{system.production_order_submission}</dd></div>
            <div><dt>Exposure Ceiling</dt><dd>{Number(operations.exposure.exposure_ceiling).toFixed(1)}x</dd></div>
            <div><dt>Eligibility Gate</dt><dd>{system.eligibility_gate}</dd></div>
            <div><dt>Actual Exposure 品質</dt><dd>{operations.exposure.quality}</dd></div>
          </dl>
        </article>
        <article className="panel page-panel risk-flow-card">
          <div className="panel-heading"><span>放行流程</span><small>FAIL CLOSED</small></div>
          <ol className="risk-flow-list">
            <li><strong>資料</strong><span>行情、VIX、估值與 PCF 必須新鮮且可追溯。</span></li>
            <li><strong>帳務</strong><span>券商帳戶、現金、結算與允許標的完成對帳。</span></li>
            <li><strong>上限</strong><span>候選策略可到 3.2x，但現金帳戶執行上限仍為 2.0x。</span></li>
            <li><strong>委託</strong><span>目前正式委託永久關閉，只能觀察或進行 Paper。</span></li>
          </ol>
        </article>
        <article className="panel page-panel manual-halt-card">
          <div className="panel-heading"><span>Manual Halt</span><small>{operations.manual_halt.active ? "ACTIVE" : "READY"}</small></div>
          <h2>{operations.manual_halt.active ? "目前已暫停紙上交易模擬" : "可暫停新的紙上交易模擬"}</h2>
          <p>{operations.manual_halt.active ? `原因：${operations.manual_halt.reason ?? "未提供"}` : "啟用後不會清算持倉；資料同步與對帳仍會繼續。"}</p>
          <button type="button" className={operations.manual_halt.active ? "secondary-action" : "danger-action"} onClick={() => void onSetManualHalt(!operations.manual_halt.active, operations.manual_halt.active ? "operator reviewed and released" : "operator emergency pause")}>
            {operations.manual_halt.active ? "解除 Manual Halt" : "啟用 Manual Halt"}
          </button>
        </article>
      </section>
      <TermGlossary />
    </>
  );
}

function AccountPage({
  operations,
  system,
  syncing,
  syncMessage,
  onSync,
  onRecordFill,
}: {
  operations: OperationsSummary;
  system: SystemStatus;
  syncing: boolean;
  syncMessage: string | null;
  onSync: () => Promise<void>;
  onRecordFill: (input: StrategyFillInput) => Promise<StrategyFillResult>;
}) {
  const localNow = new Date(Date.now() - new Date().getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
  const [fill, setFill] = useState({
    execution_id: "",
    instrument: "0050" as StrategyFillInput["instrument"],
    side: "BUY" as StrategyFillInput["side"],
    quantity: "",
    price: "",
    commission: "0",
    tax: "0",
    occurred_at: localNow,
    note: "",
  });
  const [fillMessage, setFillMessage] = useState<string | null>(null);
  const [recordingFill, setRecordingFill] = useState(false);
  const strategyNav = operations.account.strategy_nav ?? operations.account.planning_nav;
  async function submitFill(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRecordingFill(true);
    setFillMessage(null);
    try {
      const result = await onRecordFill({
        ...fill,
        occurred_at: new Date(fill.occurred_at).toISOString(),
      });
      setFillMessage(
        result.created
          ? `成交已入帳；策略 NAV ${formatMoney(result.ledger.current_nav)}，請重新同步確認持股。`
          : "此成交編號已登錄，未重複入帳。",
      );
      setFill((current) => ({ ...current, execution_id: "", quantity: "", note: "" }));
    } catch (reason: unknown) {
      setFillMessage(reason instanceof Error ? reason.message : "成交回報失敗");
    } finally {
      setRecordingFill(false);
    }
  }
  return (
    <>
      <PageHeader eyebrow="ACCOUNT TRUTH" title="帳務" description="只有完成唯讀同步與全量對帳後，才會呈現真實 NAV、現金與持倉。" />
      <section className="panel page-panel account-truth-page">
        <div className="truth-mode"><span>目前模式</span><strong>{operations.account.mode === "planning" ? "Planning Mode" : "Observed Account State"}</strong></div>
        <div className="account-actions">
          <div>
            <strong>{operations.account.account_masked ?? "尚未驗證策略帳戶"}</strong>
            <span>{operations.account.observed_at ? `最後同步 ${formatTime(operations.account.observed_at)}` : "同步後才會顯示實際帳戶數字"}</span>
          </div>
          <button
            type="button"
            className="primary-action"
            disabled={syncing || !operations.account.binding_configured}
            onClick={() => void onSync()}
          >
            {syncing ? "同步中…" : operations.account.binding_configured ? "同步唯讀帳戶" : "需先設定帳戶 ID"}
          </button>
        </div>
        {syncMessage && <p className="sync-message">{syncMessage}</p>}
        <div className="account-grid">
          <div><span>Strategy NAV</span><strong>{formatMoney(strategyNav)}</strong></div>
          <div><span>Observed NAV</span><strong>{formatMoney(operations.account.observed_nav)}</strong></div>
          <div><span>策略現金</span><strong>{formatMoney(operations.account.strategy_cash)}</strong></div>
          <div><span>投入本金</span><strong>{formatMoney(operations.account.net_contributions)}</strong></div>
          <div><span>本金虧損</span><strong>{formatMoney(operations.account.principal_loss_amount)}</strong></div>
          <div><span>本金虧損率</span><strong>{operations.account.principal_loss_ratio === null ? "未知" : `${(Number(operations.account.principal_loss_ratio) * 100).toFixed(2)}%`}</strong></div>
          <div><span>目前回撤</span><strong>{operations.account.current_drawdown === null ? "未知" : `${(Number(operations.account.current_drawdown) * 100).toFixed(2)}%`}</strong></div>
          <div><span>歷史最大回撤</span><strong>{operations.account.maximum_drawdown === null ? "未知" : `${(Number(operations.account.maximum_drawdown) * 100).toFixed(2)}%`}</strong></div>
          <div><span>整體帳戶 T+2</span><strong>{formatMoney(operations.account.pending_settlement)}</strong></div>
        </div>
        <p className="panel-note">Strategy NAV 由 50 萬本金、策略持倉市值、策略現金及實際費用逐筆計算；Observed NAV 是整個券商帳戶總資產。外部持倉與整體帳戶現金只作核對，不會灌入策略績效。</p>
        <form className="fill-form" onSubmit={(event) => void submitFill(event)}>
          <div className="panel-heading"><span>補登本人已完成的券商成交</span><small>不會送出委託</small></div>
          <div className="fill-fields">
            <label><span>券商成交編號</span><input required value={fill.execution_id} onChange={(event) => setFill({ ...fill, execution_id: event.target.value })} /></label>
            <label><span>標的</span><select value={fill.instrument} onChange={(event) => setFill({ ...fill, instrument: event.target.value as StrategyFillInput["instrument"] })}><option>0050</option><option>00631L</option><option>00685L</option></select></label>
            <label><span>方向</span><select value={fill.side} onChange={(event) => setFill({ ...fill, side: event.target.value as StrategyFillInput["side"] })}><option value="BUY">買進</option><option value="SELL">賣出</option></select></label>
            <label><span>股數</span><input required min="0.000001" step="any" type="number" value={fill.quantity} onChange={(event) => setFill({ ...fill, quantity: event.target.value })} /></label>
            <label><span>成交價</span><input required min="0.000001" step="any" type="number" value={fill.price} onChange={(event) => setFill({ ...fill, price: event.target.value })} /></label>
            <label><span>手續費</span><input required min="0" step="any" type="number" value={fill.commission} onChange={(event) => setFill({ ...fill, commission: event.target.value })} /></label>
            <label><span>交易稅</span><input required min="0" step="any" type="number" value={fill.tax} onChange={(event) => setFill({ ...fill, tax: event.target.value })} /></label>
            <label><span>成交時間</span><input required type="datetime-local" value={fill.occurred_at} onChange={(event) => setFill({ ...fill, occurred_at: event.target.value })} /></label>
            <label className="fill-note"><span>備註</span><input value={fill.note} onChange={(event) => setFill({ ...fill, note: event.target.value })} /></label>
          </div>
          <button className="primary-action" disabled={recordingFill || operations.account.mode !== "observed"} type="submit">{recordingFill ? "入帳中…" : "確認成交並入帳"}</button>
          {fillMessage && <p className="sync-message">{fillMessage}</p>}
          <p className="panel-note">請只填券商已成交的結果。入帳後台帳會先標示等待確認；再按上方「同步唯讀帳戶」，持股數量一致才會恢復曝險判定。</p>
        </form>
        <div className="position-section">
          <div className="panel-heading">
            <span>券商持倉</span>
            <small>{operations.account.positions.length} POSITIONS</small>
          </div>
          {operations.account.positions.length ? (
            <div className="position-table">
              <div className="position-row position-head">
                <span>標的</span><span>股數</span><span>現價</span><span>市值</span>
              </div>
              {operations.account.positions.map((position) => (
                <div className="position-row" key={position.instrument}>
                  <strong>{position.instrument}</strong>
                  <span>{Number(position.quantity).toLocaleString("zh-TW")}</span>
                  <span>{formatMoney(position.last_price)}</span>
                  <span>{formatMoney(position.market_value)}</span>
                </div>
              ))}
            </div>
          ) : <EmptyState>同步後會在這裡顯示策略帳戶持倉。</EmptyState>}
          {operations.account.external_instruments.length > 0 && (
            <p className="panel-note">
              外部持倉 {operations.account.external_instruments.join("、")} 由平台忽略，絕不納入自動交易。
            </p>
          )}
        </div>
        {operations.account.mode === "planning" && (
          <div className="planning-note">
            <strong>規劃 NAV：{formatMoney(operations.account.planning_nav)}</strong>
            <span>此金額來自設定，不是券商餘額，也不參與 Actual Exposure 判定。</span>
          </div>
        )}
      </section>
    </>
  );
}

function App() {
  const {
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
  } = useControlData();
  const [pathname, setPathname] = useState(window.location.pathname);
  const currentRoute = useMemo(() => routeFor(pathname), [pathname]);

  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function navigate(path: string) {
    if (path === pathname) return;
    window.history.pushState({}, "", path);
    setPathname(path);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand-mark" href="/" onClick={(event) => { event.preventDefault(); navigate("/"); }}>DVL</a>
        <nav aria-label="主要導覽">
          {routes.map((route) => (
            <a
              href={route.path}
              key={route.path}
              className={`nav-item ${currentRoute.path === route.path ? "active" : ""}`}
              aria-current={currentRoute.path === route.path ? "page" : undefined}
              onClick={(event) => { event.preventDefault(); navigate(route.path); }}
            >
              <span className="nav-short">{route.short}</span><span className="nav-label">{route.label}</span>
            </a>
          ))}
        </nav>
        <div className="paper-badge">PAPER · READ ONLY</div>
      </aside>

      <main>
        <div className="system-strip">
          <span><StatusDot state={system?.database === "ok" ? "ready" : "blocked"} />資料庫 {system?.database ?? "connecting"}</span>
          <span><StatusDot state={system?.shioaji_secret_present ? "ready" : "required"} />Shioaji {system?.shioaji_mode ?? "—"}</span>
          <strong>正式下單 OFF</strong>
          <button type="button" onClick={() => void refresh()} disabled={loading}>{loading ? "讀取中" : "重新整理"}</button>
        </div>

        {error && <section className="critical-banner">無法取得營運資料：{error}</section>}
        {loading && !operations && <section className="loading-state">正在建立營運狀態，尚未使用任何預設值…</section>}

        {!loading && dashboard && operations && system && (
          currentRoute.path === "/" ? <OverviewPage operations={operations} dashboard={dashboard} onAcknowledge={acknowledgeQueue} onResolve={resolveQueue} onCreatePaper={createPaperIntent} />
            : currentRoute.path === "/strategies" ? <StrategiesPage dashboard={dashboard} operations={operations} onSelectStrategy={selectStrategy} />
              : currentRoute.path === "/data" ? <DataPage operations={operations} dashboard={dashboard} />
                : currentRoute.path === "/risk" ? <RiskPage operations={operations} system={system} onSetManualHalt={setManualHalt} />
                  : <AccountPage
                    operations={operations}
                    system={system}
                    syncing={syncing}
                    syncMessage={syncMessage}
                    onSync={syncAccount}
                    onRecordFill={recordStrategyFill}
                  />
        )}
      </main>
    </div>
  );
}

export default App;


function ResearchEvidence({ operations }: { operations: OperationsSummary }) {
  const research = operations.research;
  if (!research) return null;
  return (
    <section className="panel page-panel">
      <div className="panel-heading"><span>研究資格與版本證據</span><small>{research.research_date ?? "尚無驗證報告"}</small></div>
      <p>日常運行：<strong>{research.selected_strategy_version}</strong> ／ 尚未取得實盤研究資格</p>
      <p className="panel-note">{research.selected_detail}</p>
      <p className="panel-note">操作待辦最後更新：{operations.operations_updated_at ? new Date(operations.operations_updated_at).toLocaleString("zh-TW") : "尚待排程更新"}。讀取頁面不會產生通知或更新待辦。</p>
      {research.evidence_status !== "verified-archive" && <p>研究檔案遺失或驗證失敗，不能引用舊資格。</p>}
      {research.comparison && <div className="benchmark-comparison">
        <h3>各階段與 0050 的差距</h3>
        <p className="panel-note">{research.comparison.period}，扣既定成本的歷史比較。正常／壓力使用各自同條件的 0050；負差距代表落後。這是 rearm 研究，並非目前運行 V2 的績效。</p>
        <div className="comparison-scroll"><table>
          <thead><tr><th>階段</th><th>正常年化</th><th>與 0050 差距</th><th>壓力年化</th><th>與 0050 差距</th><th>正常最大回撤</th><th>正常波動</th></tr></thead>
          <tbody>{research.comparison.stages.map((stage) => <tr key={stage.id}>
            <td>{stage.label}</td><td>{(100 * stage.normal.cagr).toFixed(2)}%</td>
            <td>{stage.normal.cagr_gap_pp.toFixed(2)} 百分點</td>
            <td>{(100 * stage.stress.cagr).toFixed(2)}%</td>
            <td>{stage.stress.cagr_gap_pp.toFixed(2)} 百分點</td>
            <td>{(100 * stage.normal.drawdown).toFixed(2)}%</td>
            <td>{(100 * stage.normal.volatility).toFixed(2)}%</td>
          </tr>)}</tbody>
        </table></div>
        <p className="panel-note">年化是整段資產成長換算的每年複利速度；回撤是從高點下跌的幅度；波動是每日報酬起伏。本輪未改研究規則，績效維持不變；仍未通過原投資標準。</p>
      </div>}
      {research.records.map((item) => (
        <div className="research-evidence-row" key={item.identity_sha256}>
          <strong>{item.identity.strategy_version}</strong>
          <span>{item.status === "reject-live-candidacy" ? "研究完成：未通過原投資標準" : "僅對照／診斷，不具實盤資格"}</span>
          <span>正常年化 {(item.normal_cagr * 100).toFixed(2)}% ／ 壓力年化 {(item.stress_cagr * 100).toFixed(2)}%</span>
          <small>{item.period}；已看過的歷史，不是新樣本外。</small>
          <a href={item.report_url} target="_blank" rel="noreferrer">查看報告與限制</a>
        </div>
      ))}
    </section>
  );
}
