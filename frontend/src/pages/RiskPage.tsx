import { ActionError, PageHeader, TermGlossary } from "../components";
import type { OperationsSummary, SystemStatus } from "../types";
import { useAction } from "../useAction";
export function RiskPage({ operations, system, onSetManualHalt }: {
  operations: OperationsSummary;
  system: SystemStatus;
  onSetManualHalt: (active: boolean, reason?: string) => Promise<void>;
}) {
  const halt = useAction(onSetManualHalt);
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
          <button type="button" className={operations.manual_halt.active ? "secondary-action" : "danger-action"} disabled={halt.pending} onClick={() => void halt.run(!operations.manual_halt.active, operations.manual_halt.active ? "operator reviewed and released" : "operator emergency pause")}>
            {halt.pending ? "處理中…" : operations.manual_halt.active ? "解除 Manual Halt" : "啟用 Manual Halt"}
          </button>
          <ActionError message={halt.error} />
        </article>
      </section>
      <TermGlossary />
    </>
  );
}
