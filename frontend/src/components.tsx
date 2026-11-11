import type { ReactNode } from "react";
import type { ActivationItem } from "./types";

export function StatusDot({ state }: { state: "ready" | "required" | "blocked" }) {
  return <span className={`status-dot ${state}`} aria-hidden="true" />;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

export function PageHeader({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
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

export function ActivationList({ items }: { items: ActivationItem[] }) {
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

/** A failed action's message, shown next to the control that triggered it. */
export function ActionError({ message }: { message: string | null }) {
  return message ? <p className="action-error" role="alert">{message}</p> : null;
}

export function TermGlossary() {
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
