# 營運控制台規格

狀態：已接受

> 文件性質：目標 UX／營運規格。此文件定義控制台應如何運作；目前哪些項目已完成、部分完成或尚未實作，請以 [PRODUCT-GAP-AUDIT.md](./PRODUCT-GAP-AUDIT.md) 為準。

本文件定義 Dynamic Leverage Control 從展示型 Dashboard 升級為可每日使用的私人 Operator Console。它的第一責任不是呈現最多資訊，而是讓操作人員在 30 秒內回答：系統是否安全、是否需要處理、目標與實際曝險是否一致，以及所有結論是否有足夠證據。

## 產品邊界

- 單一使用者、單一明確綁定的 Strategy Account。
- Shioaji 僅允許 Read-only Broker Sync；正式下單路徑維持不存在。
- Shadow 與 Paper 自動運行，人工操作只觸發可稽核的診斷或安全動作。
- Planning Mode 可以協助設定與預覽，但不能冒充 Observed Account State。
- Telegram 是 Operator Queue 的通知出口，不是帳務資料庫或風控覆寫入口。
- 桌面版支援完整營運與研究；手機版只處理狀態、警報、Manual Halt 與必要確認。

## 使用者的每日流程

1. 打開總覽。
2. 讀取唯一的 Operator Status 與一句人話結論。
3. 若為 Normal，確認下次排程與資料新鮮度後離開。
4. 若為 Attention、Action Required 或 Blocked，從 Operator Queue 開啟最高優先項目。
5. 查看原因、影響、證據與建議下一步。
6. 必要時執行重新同步、手動 Strategy Run、Acknowledgement 或 Manual Halt。
7. 回到總覽確認底層條件是否已產生 Resolution；Acknowledgement 本身不得解除安全狀態。

## 資訊架構

| 路徑 | 分頁 | 責任 | 明確不負責 |
|---|---|---|---|
| `/` | 總覽 | 今日狀態、曝險差距、帳戶摘要、下次排程、Operator Queue | 修改策略參數、完整研究圖表 |
| `/strategies` | 策略 | Strategy Revision、Strategy Run、理由、回測、敏感度、Promotion Gate | 帳戶真相與警報解除 |
| `/data` | 資料 | 來源、Freshness Contract、快照、PCF/NAV、缺漏、矛盾、時間血緣 | 以補值解除 Data Unsafe |
| `/risk` | 風控 | Risk Gate、Panic Lock、Manual Halt、異常狀態、演練紀錄 | 任意覆寫策略結果 |
| `/account` | 帳務 | Observed Account State、Strategy Cash、T+2、持倉、Paper Evidence Chain、對帳 | Household Reserve、未綁定帳戶 |

每個 Operator Queue 項目與每筆 Strategy Run 都必須有穩定深層連結，Telegram 可直接指向證據頁。

## 首頁線框

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ DVL  總覽  策略  資料  風控  帳務                     唯讀券商 ●  13:45:08 │
├────────────────────────────────────────────────────────────────────────────┤
│ OPERATOR STATUS: NORMAL                                                    │
│ 今日無需處理；實際曝險位於允許區間。                                       │
│ 狀態自 13:31 起 │ 下次評估：週五收盤後 │ 最弱資料：PCF 08:32              │
├──────────────────────────────────────┬─────────────────────────────────────┤
│ 曝險控制                             │ Strategy Account                    │
│ 目標 1.20x  實際 1.18x  差距 -0.02x │ NAV          NT$400,000             │
│ 允許區間 1.08x ─────●───── 1.32x    │ Strategy Cash NT$76,420              │
│ 建議：無需再平衡                     │ T+2 待交割    NT$0                   │
│ [查看計算證據]                       │ [查看持倉與對帳]                     │
├──────────────────────────────────────┴─────────────────────────────────────┤
│ 最近 30 日：目標曝險 ──  實際曝險 ──  資料缺口 ▒                         │
├──────────────────────────────────────┬─────────────────────────────────────┤
│ OPERATOR QUEUE                       │ 自動化與資料                         │
│ ✓ 無 BLOCKER                         │ Strategy Run   成功  13:31           │
│ ! 1 WARNING：PCF 將於明日開盤失效    │ Broker Sync    成功  13:45           │
│ [查看全部]                           │ TWSE Snapshot  成功  14:35           │
├──────────────────────────────────────┴─────────────────────────────────────┤
│ [立即重新同步] [執行手動 Strategy Run]                 [啟用 Manual Halt] │
└────────────────────────────────────────────────────────────────────────────┘
```

當 Operator Status 不是 Normal，狀態卡取代一般摘要，顯示最高嚴重度原因、影響、已禁止的動作與唯一主要處理入口。首頁不以多個獨立燈號要求使用者自行推理。

## 首頁欄位

### Operator Status

- 狀態：Normal、Attention、Action Required、Blocked。
- 一句人話結論。
- `state_since`。
- 最高嚴重度三個原因及其 Operator Queue 連結。
- 建議下一步；沒有下一步時明示「無需處理」。
- 下次自動 Strategy Run 與風控評估時間。
- 最弱必要資料源、觀測時間、接收時間與剩餘有效時間。

### 曝險控制

- Target Exposure。
- Strategy Exposure；以設定的 Strategy NAV 為分母，無法可靠計算時必須顯示 Unknown。Observed Account NAV 與 Account Exposure 只作帳戶層級參考。
- 曝險差距與允許區間。
- 每項持倉採用的 observed/official exposure 及來源。
- Execution Recipe、目標權重、診斷用建議股數。
- 計算時間、有效時間、資料品質與輸入快照連結。
- 再平衡結論：無需處理、Paper Intent Pending、Increase Blocked、Reduction Required。

Target Exposure 不得代替 Actual Exposure；ETF 名稱中的「2x」也不得代替官方或觀測曝險。

### Strategy Account

- 明確遮罩的 Strategy Account Binding。
- Observed Account State 或清楚標記的 Planning Mode。
- Strategy NAV。
- Strategy Cash。
- T+2 應收應付與最早交割日。
- 持倉市值及未完成 Paper Intent 數量。
- 最近一次 Broker Sync、對帳結果與任何 Reconciliation Break。
- 今日損益只作次要資訊，不以紅綠損益取代現金與風險狀態。

### 30 日控制圖

首頁只保留一張營運圖：Target Exposure、Actual Exposure、允許區間與資料缺口。績效、估值、回測與參數敏感度圖移至策略頁。

### Operator Queue

- 僅顯示未 Resolution 的項目。
- 排序依序為嚴重度、未 Acknowledged、發生時間。
- 相同根因合併為同一項並累計發生次數，避免通知洗版。
- 每項顯示狀態、影響、首次與最近發生時間、建議動作、證據與負責的解除條件。

## Operator Status 推導

Operator Status 是所有底層狀態的最高嚴重度摘要，不是可手動修改的欄位。

| Operator Status | 典型條件 | 系統行為 |
|---|---|---|
| Blocked | Broker Unsafe、Manual Halt、帳戶綁定失敗、必要資料缺失且無法安全減碼 | 不產生新自動 Intent；繼續蒐集資料與對帳 |
| Action Required | Reconciliation Break、失敗排程超過重試邊界、Paper 對帳失敗 | 建立 Operator Queue 項目並通知 Telegram |
| Attention | 資料將過期、ETF 接近異常門檻、排程延遲但仍在有效窗內 | 繼續監控，不授權越過現有安全邊界 |
| Normal | 所有必要資料有效、帳戶已對帳、沒有未處理高嚴重度項目 | 按既定排程執行 Shadow/Paper |

底層條件解除後才可 Resolution。Acknowledgement 只記錄「已看過」，不改變 Operator Status 的安全事實。

## Operator Queue 模型

### 嚴重度

1. `BLOCKER`：不得建立新的自動 Intent。
2. `ACTION_REQUIRED`：需要人工處理。
3. `WARNING`：可繼續運行，但必須觀察。
4. `INFO`：稽核紀錄，不傳 Telegram。

### 生命週期

```text
Open ── acknowledge ──> Acknowledged
  │                          │
  └──── condition clears ────┴──> Resolved
                                      │
                                      └── same condition returns ──> Reopened
```

每次轉移保存 actor、時間、理由、證據與 correlation ID。使用者不能刪除項目、跳過 Resolution 條件或用 Acknowledgement 覆寫 Risk Gate。

## Freshness Contract

不同資料具有不同時鐘，禁止以單一「最後更新」代表全部有效。

| 資料類別 | 初始有效性規則 | 過期影響 |
|---|---|---|
| Broker session/account | 連線期間持續接收事件；重連後立即全量對帳 | Broker Unsafe |
| 盤中 quote | 交易時段使用串流，超出設定秒數即 stale | 禁止以該價格產生 Intent |
| TWSE 日行情 | 依交易日與官方發布時間判斷 | 依賴收盤資料的 Strategy Run 無效 |
| 發行商 PCF/NAV/exposure | 每個交易日保存原始快照與 as-of | Actual Exposure 不得假設為 2.0x |
| 估值資料 | 依 publication/effective date，而非匯入日 | 估值策略 Data Unsafe |
| Strategy Run | 必須引用仍可驗證的輸入快照 | 禁止重用為新的交易依據 |

具體秒數與截止時間使用 effective-dated configuration 管理，不寫死在前端。首頁顯示最後已知值時，必須同時顯示資料年齡、過期狀態與禁止用途。

## 帳戶真相與 Planning Mode

1. 設定中只允許一個遮罩後可辨識的 Strategy Account Binding。
2. Shioaji 回傳零個、多個符合帳戶或綁定不一致時，進入 Broker Unsafe。
3. Observed Account State 必須整合持倉、Strategy Cash、settlements、未完成委託與外部交易。
4. 未知持倉會被標記為外部持倉並保留在帳務畫面；平台不會賣出、接管或納入策略曝險，也不會稀釋 Strategy NAV 口徑。只有融資、放空、非現金結構或允許標的對帳失敗才建立 Reconciliation Break。
5. Broker Sync 不可用時可以進入 Planning Mode，但規劃 NAV、配置與股數不能顯示為實際值，也不能授權增加曝險。

## Shadow 與 Paper 自動化

- 排程自動建立 immutable Strategy Run。
- Strategy Run 引用明確的 Strategy Revision 與輸入 snapshot IDs。
- 相同輸入與版本必須得到相同決策；每次執行仍保留獨立 run identity。
- 「立即重跑」建立 `trigger=manual` 的新 Strategy Run，不更新舊資料。
- 每個 Paper Evidence Chain 串接 Strategy Run、Trade Intent、Risk Decision、Paper Order、Fill、T+2 與 reconciliation。
- Manual Halt 停止新的自動 Intent，但資料蒐集、Broker Sync、對帳與既有證據保存繼續運行。

## Telegram

目前通知出口會在以下狀態變化時去重發送：Scheduler 成功／降級、必要資料過期或恢復、唯讀帳戶同步失敗或恢復、Risk Gate 阻擋／放行、Manual Halt 啟用／解除，以及 Operator Queue 新增／ACK／RESOLVED／REOPENED。

訊息包含：

- 嚴重度與 Operator Status 改變。
- 簡短原因與影響。
- 發生時間。
- Operator Queue item ID（若事件來自 Queue）。

目前訊息尚未附帶控制台深層連結；可在後續接入固定 localhost URL 後再補上。

訊息不包含完整帳號、完整持倉、帳戶總額、secret、CA、原始例外堆疊或可繞過風控的按鈕。Telegram Acknowledgement 若實作，仍需寫入相同稽核生命週期。

## 啟用清單

最低監控條件未滿足時，以啟用清單取代正式總覽：

1. runtime secrets 已安全掛載。
2. Shioaji 唯讀登入成功。
3. Strategy Account Binding 唯一且驗證成功。
4. 0050、00631L 與監控替代品 contract 可取得。
5. TWSE 排程與資料庫可用。
6. Freshness Contract 已載入。
7. Telegram 測試訊息成功，或明確選擇暫不啟用通知。
8. Manual Halt 演練完成。

清單驗證的是實際能力，不是欄位是否填寫。CA 在目前唯讀階段可以列為未需要；進入 Sandbox 或 Live Canary 前另設 Eligibility Gate。

## 操作與防呆

| 操作 | 要求 | 不得造成 |
|---|---|---|
| 立即重新同步 | idempotency key、顯示進度與結果 | 重複寫入不同真相 |
| 手動 Strategy Run | 確認版本、快照、理由，建立新 run | 改寫既有 run |
| Acknowledgement | 記錄 actor/time/note | Resolution 或解除 Risk Gate |
| 啟用/解除 Manual Halt | 二次確認、理由、稽核事件 | 自動清算持倉 |
| 重新綁定帳戶 | 二次確認、重新全量對帳 | 沿用舊帳戶狀態 |
| 變更 Telegram 目的地 | 測試訊息與遮罩顯示 | 暴露 token 或帳務資料 |

## 載入、斷線與錯誤

- `Connecting`：尚未取得任何權威值；顯示 skeleton，不顯示預設金額。
- `Last Known / Stale`：可顯示最後已知值，但連同 as-of、年齡、過期原因與禁止用途。
- `Unavailable`：來源不可用且沒有足夠證據；相關欄位顯示 Unknown。
- `Planning Mode`：只顯示明確的規劃假設，與真實帳戶視覺上不可混淆。
- API 部分失敗採局部降級；Operator Status 仍反映最高嚴重度，不用一條泛用「Dashboard unavailable」遮蔽原因。

## 五個分頁的詳細內容

### 總覽

- Operator Status 與下一步。
- Target/Actual Exposure 及差距。
- Strategy Account 摘要。
- 30 日控制圖。
- Operator Queue 摘要。
- 自動化排程與資料新鮮度摘要。
- Manual Halt 與診斷操作。

### 策略

- Strategy Revision 清單與不可變參數。
- 最新及歷史 Strategy Run。
- 每個決策的 reasons、diagnostics、輸入快照及 safety state。
- 候選策略並排比較，但不合併中間邏輯。
- actual ETF return、成本、walk-forward、敏感度與 Promotion Gate。
- 研究輸出與 Trade-authoritative 狀態清楚分離。

### 資料

- 各資料源的 owner、權威等級與 Freshness Contract。
- observed_at、published_at、effective_at、received_at。
- 原始快照、checksum、quality 與 stale reason。
- 發行商 PCF/NAV/actual exposure 與 corporate actions。
- 缺漏、矛盾、補值及 Supplemental Data 標記。
- 不提供「忽略資料品質並繼續」操作。

### 風控

- Risk Gate 最新決策與證據。
- Panic Lock、Data Unsafe、ETF Abnormal、Broker Unsafe、Manual Halt。
- Exposure Ceiling 與各 maturity 的較低上限。
- 安全狀態演練、最近成功時間與恢復證據。
- Manual Halt 控制與 immutable audit trail。

### 帳務

- Observed Account State 與 account binding。
- 持倉、Strategy Cash、T+2、未完成 broker events。
- Reconciliation Break 與處理流程。
- Paper Evidence Chain 時間軸。
- Paper NAV、成本、稅、滑價及 settlement ledger。
- 不納入 Household Reserve 或其他未綁定帳戶。

## 後端讀模型需求

前端不應自行拼湊營運結論。後端需要投影下列 read models：

- `operations_summary`：Operator Status、原因、下一步、排程、最弱資料。
- `exposure_summary`：target、actual、band、gap、components、quality、as-of。
- `account_summary`：observed/planning mode、NAV、cash、settlements、positions、reconciliation。
- `operator_queue`：item、severity、lifecycle、evidence、resolution condition。
- `automation_status`：scheduler、latest/next Strategy Run、Broker Sync、ingestion jobs。
- `freshness_status`：逐來源契約、年齡、stale reason、對授權的影響。

所有讀模型都回傳 `as_of`、`received_at`、`quality` 與來源 identity。前端只能呈現後端已推導的安全結論，不得自行將未知值判定為正常。

## 分階段實作

### Phase 1：真的能每天使用

1. 建立真正的 URL routes 與完整文字導覽。
2. 移除假實際值；加入 Connecting、Stale、Unavailable、Planning Mode。
3. 實作 Strategy Account Binding 與 Read-only Broker Sync。
4. 建立 account、exposure、freshness read models。
5. 建立 Operator Status 與 Operator Queue。
6. 實作首頁、啟用清單、Telegram 與 Manual Halt。
7. 加入斷線、過期、未知持倉、Docker 重啟的 end-to-end 測試。

### Phase 2：建立自主運行證據

1. 排程自動 Strategy Run。
2. 持久化 Trade Intent 與 Risk Decision。
3. 串接 deterministic Paper Execution、T+2 與 reconciliation。
4. 建立策略、資料、風控、帳務詳細頁。
5. 對每條 Paper Evidence Chain 做 replay 與一致性驗證。

### Phase 3：研究與晉級

1. actual ETF history 與 after-cost benchmarks。
2. walk-forward、參數鄰域、雙倍成本與危機情境。
3. Promotion Gate 證據與 60 日連續運行報告。
4. Live Canary 仍需獨立設計與人工批准，不因 UI 完成自動開放。

## 第一版不做

- 正式下單或可被 feature flag 打開的 production submit。
- 在首頁直接修改策略參數。
- 把 20 萬規劃 NAV 顯示為券商餘額。
- 以 ETF 名義槓桿推定 Actual Exposure。
- 讓 Acknowledgement、Telegram 或管理員按鈕覆寫 Risk Gate。
- 多使用者、公開行情轉播或 Household Reserve 管理。

## 驗收標準

- 新使用者在 30 秒內判斷系統是否安全及是否需要處理。
- 每個顯示值都能追到來源、時間、品質與有效期限。
- Planning Mode 與 Observed Account State 不可能被視覺混淆。
- 斷線、資料過期、帳戶不一致或對帳失敗自動 fail closed。
- Telegram 能從狀態變化直接定位到 Operator Queue 證據。
- Shadow/Paper 可連續 60 個交易日自動留下完整 Paper Evidence Chain。
- 所有安全狀態均有可重複的觸發、恢復及 Docker 重啟測試。
- 沒有任何前端操作能建立 production broker order。
