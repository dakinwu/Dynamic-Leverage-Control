import { useState } from "react";
import { ActionError, EmptyState, PageHeader, TermGlossary } from "../components";
import { deriveDecision } from "../decision";
import { allocationForExposure, formatMoney, formatTime } from "../format";
import type { Dashboard, OperationsSummary, PaperIntentResult, QueueItem } from "../types";
import { useAction } from "../useAction";

function QueueRow({ item, onAcknowledge, onResolve }: {
  item: QueueItem;
  onAcknowledge: (itemId: string) => Promise<QueueItem>;
  onResolve: (itemId: string) => Promise<QueueItem>;
}) {
  const acknowledge = useAction(onAcknowledge);
  const resolve = useAction(onResolve);
  const pending = acknowledge.pending || resolve.pending;
  return (
    <li>
      <span className={`severity ${item.severity.toLowerCase()}`}>{item.severity}</span>
      <div><strong>{item.title}</strong><small>{item.detail} · {item.next_action}</small></div>
      <div className="queue-actions">
        {item.status === "OPEN" || item.status === "REOPENED" ? (
          <button type="button" disabled={pending} onClick={() => void acknowledge.run(item.item_id)}>{pending ? "處理中…" : "已看"}</button>
        ) : (
          <button type="button" disabled={pending} onClick={() => void resolve.run(item.item_id)}>{pending ? "處理中…" : "標記解決"}</button>
        )}
        <ActionError message={acknowledge.error ?? resolve.error} />
      </div>
    </li>
  );
}
export function OverviewPage({
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
  const createPaper = useAction(onCreatePaper);
  const target = operations.exposure.target_exposure;
  const actual = operations.exposure.actual_exposure;
  const unresolved = operations.queue.filter((item) => item.status !== "RESOLVED");
  const targetNumber = target === null ? null : Number(target);
  const actualNumber = actual === null ? null : Number(actual);
  const decision = deriveDecision(operations);
  const gap = decision.gap;
  const oneXPosition = operations.account.positions.find((position) => position.instrument === "0050");
  const strategyNav = operations.account.strategy_nav ?? operations.account.planning_nav;
  const strategyReady = operations.strategy_readiness.ready;
  const recipe = allocationForExposure(targetNumber);
  const estimateShares = (instrument: string, weight: number): string => {
    const price = Number(dashboard.market_prices[instrument] ?? 0);
    if (!price || !weight) return "—";
    return Math.floor((Number(strategyNav) * weight) / price).toLocaleString("zh-TW");
  };

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
          <h2>{decision.headline}</h2>
          <p>{decision.detail}</p>
          <div className="decision-signal">
            <span>觀察建議</span>
            <strong>{decision.recommendation}</strong>
          </div>
          <div className="exposure-action-key">
            <span>如果真的執行，方向會是</span>
            <strong>{decision.direction}</strong>
            <small>這只是方向提示；本專案不會送出正式委託。</small>
            {decision.canPreviewPaper && actualNumber !== null && targetNumber !== null && (
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
                {targetNumber > 2 && <small>目標超過 2.0x 現金帳戶上限：會記錄為 EXPOSURE_CEILING 阻擋，下方比例是 2.0x 的示意。</small>}
                <span>0050 {Math.round(recipe.base * 100)}% · 00631L {Math.round(recipe.leveraged * 100)}% · 現金 {Math.round(recipe.cash * 100)}%</span>
                <span>股數估算：0050 {estimateShares("0050", recipe.base)} 股 · 00631L {estimateShares("00631L", recipe.leveraged)} 股</span>
                 <small>確認後只建立策略／風控／意圖模擬證據，不會送出券商委託；費用、滑價、成交與 T+2 目前尚未計入。</small>
                <div className="paper-preview-actions">
                  <button type="button" className="primary-action" disabled={createPaper.pending} onClick={() => void createPaper.run(actualNumber, targetNumber, "overview exposure gap review").then((result) => { if (result) { setPaperMessage(`${result.recommendation} · Run ${result.run_id.slice(0, 8)}`); setPaperPreviewOpen(false); } })}>{createPaper.pending ? "建立中…" : "確認建立模擬紀錄"}</button>
                  <button type="button" className="secondary-action" disabled={createPaper.pending} onClick={() => setPaperPreviewOpen(false)}>取消</button>
                </div>
                <ActionError message={createPaper.error} />
              </div>
            )}
            {paperMessage && <small className="paper-result">{paperMessage}</small>}
          </div>
          <div className="decision-next">
            <span>下一步</span>
            <strong>{decision.nextStep}</strong>
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
            <div><span>曝險差距（目標−實際）</span><strong>{gap === null ? "無法計算" : `${gap > 0 ? "+" : ""}${gap.toFixed(2)}x`}</strong></div>
          </div>
          <div className="range-track"><i style={{ width: target === null ? "0%" : `${Math.max(0, Math.min(100, (Number(target) - 1) * 100))}%` }} /></div>
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
                <QueueRow key={item.item_id} item={item} onAcknowledge={onAcknowledge} onResolve={onResolve} />
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
