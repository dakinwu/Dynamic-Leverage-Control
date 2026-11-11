import { useState } from "react";
import { ActionError, EmptyState, PageHeader, TermGlossary } from "../components";
import { strategyCatalog } from "../routes";
import type { Dashboard, OperationsSummary } from "../types";
import { useAction } from "../useAction";
export function StrategiesPage({ dashboard, operations, onSelectStrategy }: {
  dashboard: Dashboard;
  operations: OperationsSummary;
  onSelectStrategy: (version: string, mode?: string) => Promise<void>;
}) {
  const select = useAction(onSelectStrategy);
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
          <button type="button" className="primary-action" disabled={select.pending} onClick={() => void select.run(selectedVersion, selectedVersion === "dvl-tw-v2-shadow" ? "shadow" : "paper")}>{select.pending ? "套用中…" : "套用到 Shadow／Paper"}</button>
        </div>
        <ActionError message={select.error} />
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
