# 產品／營運控制台差距盤點

盤點日期：2026-09-08

2026-09-08 第一批可信度重構已完成：策略狀態可跨交易日保存且同日重跑不重複推進；估值按月份去重並排除決策日後才可得的快照；回測成本、淨報酬、首日回撤與下行風險口徑已統一；0–2 倍配置不再靜默截斷；預設研究資本更新為 500,000 元。完整 Strategy NAV 帳本及 point-in-time 歷史回放仍屬後續缺口。

本次優化已補上：Freshness Contract／Automation Status read model、持久化 Operator Queue（含 acknowledged／resolved／reopened）、Manual Halt audit state、策略選定 API、Paper Evidence Chain API、交易日排程計算與每次 run 的獨立 run_id，以及前端對應的 Queue／資料新鮮度／策略切換／紙上交易模擬控制。另已完成無效策略目標明確為 NULL、固定 2.0x 候選、120 個月歷史完整度 read model、0050／00631L／現金分段配置與交易日唯讀帳戶自動同步。

本文件以 `docs/PRODUCT.md` 與 `docs/OPERATOR-CONSOLE.md` 對照目前程式。正式下單維持關閉不是缺口，而是 MVP 的明確非目標。

## 已符合或可用

- Localhost Docker Compose、PostgreSQL 稽核資料庫與 Traditional Chinese responsive dashboard。
- Shioaji 唯讀登入、單一帳戶綁定、遮罩帳號、NAV／現金／T+2／持倉同步。
- 未允許標的保留顯示但不納入策略交易；無法以官方 PCF 對帳時會 fail-closed。
- 官方 TWSE／TAIFEX／發行商 PCF 原始快照保存、checksum 與來源時間戳。
- 固定 1.2x、固定 2.0x 與 DVL-TW v2 Shadow 候選、不可變 Strategy Evaluation、原因與 diagnostics。
- 基本 Risk Gate、2.0x 現金帳戶硬上限與正式委託 disabled。
- 基本 backtest、allocation recipe、paper order/fill 資料模型與單元／整合測試。

## 已補齊的營運控制台能力

| 區域 | 現況 | 影響 |
|---|---|---|
| 總覽 | 顯示 Operator Status、目標／實際曝險、方向、目標標的配方、30 日觀測圖、Freshness、Automation、Queue 與兩段式紙上調整預覽 | 真實 NAV 與 Actual Exposure 仍取決於唯讀同步與官方 PCF |
| 策略 | 可切換固定 1.2x、固定 2.0x、DVL V2 Shadow；顯示估值→點位→槓桿工作表、權重與資料診斷；缺資料時明確顯示未評估 | 歷史回測／OOS／Promotion Gate 尚未在日常頁完整呈現 |
| 資料 | 顯示 owner、Freshness Contract、年齡、有效期限、stale reason、snapshot id 與 checksum | published/effective 欄位與矛盾／corporate-action read model 仍待研究階段補齊 |
| 風控 | 顯示 2.0x 硬上限、fail-closed 流程與可持久化 Manual Halt；風控頁與首次出現名詞均附註解 | 尚未提供完整的 halt 演練報告與風控決策時間軸 |
| 帳務 | 保留 Planning／Observed 分界、策略持倉／外部持倉、NAV／現金／T+2；Paper evidence API 已可追查 strategy→risk→intent | Paper order、fill、結算與對帳尚未由 scheduler 自動落 ledger |
| 排程 | 交易日語意排程、09:05 唯讀帳戶同步、09:15 paper／close 視窗、Automation Status 與每次獨立 run_id 已落庫 | TWSE 假日與補班目前使用可替換的本地 fallback，尚未接完整日曆 feed |

## 尚未實作的後續缺口

1. 將目前的 Paper Evidence scaffold 擴充為 deterministic Paper Order／Fill／T+2 settlement／reconciliation ledger，並提供 60 日時間軸與 replay。
2. 接入 TWSE 交易日曆與補班 feed，保留本地 fallback 並記錄日曆版本與失效原因。
3. 補齊真實 ETF NAV／PCF、費稅、滑價、corporate actions、walk-forward、OOS、敏感度與 Promotion Gate 報告。
4. Telegram 已支援 scheduler、資料新鮮度、帳戶同步、Risk Gate、Manual Halt 與 Queue 狀態變化的去重通知；仍需補控制台深連結與測試訊息 UI。
5. 每日加密備份與定期 restore test；目前沒有營運自動化證據。

## 可以降級或移除的元素

- 目前 UI 的「現金帳戶可執行上限 2.0x」與「3.2x Shadow 候選」應保留，但必須繼續標示為不同 maturity；不能把 3.2x 顯示成可買目標。
- FinMind 保留為 supplemental history／cross-check 即可，不應進入決策主路徑；若暫時沒有使用，可從啟用清單移到「未啟用的選配資料源」。
- 00685L PCF 可保留作替代方案研究，但在 0050／00631L primary recipe 尚未完成前，不應與主策略並列成可執行標的。
- `dvl-tw-v1.0`、V1.1 raw、V1.1 winsorized 可保留後端 immutable registry；若沒有回測結果，前端不必放成日常操作按鈕，以免造成候選過多的錯覺。

## 建議下一階段順序

1. 先把 Paper Evidence scaffold 接到 deterministic Paper Order／Fill／T+2／reconciliation，完成 60 日時間軸與 replay。
2. 再接入 TWSE 假日／補班日曆，並把日曆版本納入 Freshness Contract。
3. 最後補實際 ETF after-cost 回測、walk-forward／OOS、敏感度與 Promotion Gate；在這之前不提升任何執行成熟度。
