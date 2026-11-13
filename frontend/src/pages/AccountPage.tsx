import { useState } from "react";
import { ActionError, EmptyState, PageHeader } from "../components";
import { formatMoney, formatPercent, formatTime, sumAmounts } from "../format";
import type { AccountSyncResult, OperationsSummary, StrategyFillInput, StrategyFillResult, StrategyLedger, SystemStatus } from "../types";
import { useAction } from "../useAction";

function DividendConfirmation({ actionId, credited, onConfirm }: {
  actionId: string;
  credited: string;
  onConfirm: (actionId: string, receivedAmount: string) => Promise<StrategyFillResult>;
}) {
  const [received, setReceived] = useState(credited);
  const confirm = useAction(onConfirm);
  return (
    <li>
      <div><strong>{actionId}</strong><small>暫以 {formatMoney(credited)} 入帳，請輸入銀行實際入帳金額</small></div>
      <label><span>實收金額</span><input type="number" min="0" step="any" value={received} onChange={(event) => setReceived(event.target.value)} /></label>
      <button type="button" className="primary-action" disabled={confirm.pending || received === ""} onClick={() => void confirm.run(actionId, received)}>{confirm.pending ? "確認中…" : "確認實收"}</button>
      <ActionError message={confirm.error} />
    </li>
  );
}

export function AccountPage({
  operations,
  system,
  ledger,
  onSync,
  onRecordFill,
  onConfirmDividend,
}: {
  operations: OperationsSummary;
  system: SystemStatus;
  ledger: StrategyLedger | null;
  onSync: () => Promise<AccountSyncResult>;
  onRecordFill: (input: StrategyFillInput) => Promise<StrategyFillResult>;
  onConfirmDividend: (actionId: string, receivedAmount: string) => Promise<StrategyFillResult>;
}) {
  const unconfirmed = Object.entries(ledger?.unconfirmed_dividends ?? {});
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
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const sync = useAction(onSync);
  const syncing = sync.pending;
  async function runSync() {
    setSyncMessage(null);
    const result = await sync.run();
    if (result) setSyncMessage(`已同步 ${result.account_masked}`);
  }
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
            onClick={() => void runSync()}
          >
            {syncing ? "同步中…" : operations.account.binding_configured ? "同步唯讀帳戶" : "需先設定帳戶 ID"}
          </button>
        </div>
        {syncMessage && <p className="sync-message">{syncMessage}</p>}
        <ActionError message={sync.error} />
        <div className="account-grid">
          <div><span>Strategy NAV</span><strong>{formatMoney(strategyNav)}</strong></div>
          <div><span>Observed NAV</span><strong>{formatMoney(operations.account.observed_nav)}</strong></div>
          <div><span>策略現金</span><strong>{formatMoney(operations.account.strategy_cash)}</strong></div>
          <div><span>投入本金</span><strong>{formatMoney(operations.account.net_contributions)}</strong></div>
          <div><span>本金虧損</span><strong>{formatMoney(operations.account.principal_loss_amount)}</strong></div>
          <div><span>本金虧損率</span><strong>{formatPercent(operations.account.principal_loss_ratio)}</strong></div>
          <div><span>目前回撤</span><strong>{formatPercent(operations.account.current_drawdown)}</strong></div>
          <div><span>歷史最大回撤</span><strong>{formatPercent(operations.account.maximum_drawdown)}</strong></div>
          <div><span>整體帳戶 T+2</span><strong>{formatMoney(operations.account.pending_settlement)}</strong></div>
          <div><span>應收配息（已計入 NAV）</span><strong>{ledger ? formatMoney(String(sumAmounts(ledger.receivables))) : "未知"}</strong></div>
        </div>
        <p className="panel-note">Strategy NAV 由 50 萬本金、策略持倉市值、策略現金及實際費用逐筆計算；Observed NAV 是整個券商帳戶總資產。外部持倉與整體帳戶現金只作核對，不會灌入策略績效。</p>
        {unconfirmed.length > 0 && (
          <section className="dividend-confirmations">
            <div className="panel-heading"><span>待確認配息</span><small>{unconfirmed.length} PAYMENTS</small></div>
            <p className="panel-note">發放日已依官方配息表以應收金額入帳；補充保費或匯費可能使實收較少，請以銀行入帳金額確認。</p>
            <ul>
              {unconfirmed.map(([actionId, credited]) => (
                <DividendConfirmation key={actionId} actionId={actionId} credited={credited} onConfirm={onConfirmDividend} />
              ))}
            </ul>
          </section>
        )}
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
