import { ActivationList, EmptyState, PageHeader } from "../components";
import { formatTime } from "../format";
import type { Dashboard, OperationsSummary } from "../types";
export function DataPage({ operations, dashboard }: { operations: OperationsSummary; dashboard: Dashboard }) {
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
