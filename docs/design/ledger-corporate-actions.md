# 策略帳本：分割與現金配息（設計提案）

狀態：**已核准（第 10 節四項都採建議選項）並已實作**，決定記錄於 [ADR 0007](../adr/0007-book-corporate-actions-from-official-facts-and-broker-observation.md)。日期：2026-11-11。範圍：`strategy_ledger_state`／`strategy_ledger_events`，以及帳戶同步、紙上成交入帳。研究模擬器不在本次行為變更範圍（見第 7 節）。

## 1. 問題（已對照程式碼確認）

帳本目前只有 `BOOTSTRAP`、`MARK`、`FILL`、`RECONCILIATION_BREAK` 四種事件（`services/account_sync.py`、`services/ledger.py`）。

- **分割**：券商股數變成 `原股數 × 比例`，`mark_strategy_ledger` 比對到 `observed != expected`，回傳 `unrecorded-position-change`。沒有任何事件能修正帳本股數，所以會**永久**對帳失敗，實際曝險一直是未知。
- **現金配息**：帳本現金只會因 `FILL` 改變。除息日價格下跌，`current_nav = cash + Σ股數×價格` 跟著下降，配息卻從未入帳，因此每次除息都變成一筆假虧損，並累積進 `principal_loss_ratio`／`maximum_drawdown`，直接影響 40% 虧損上限（`CAPITAL_LOSS_LIMIT`）。
- 允許標的的現況：0050 會配息、已分割（1→4，2025-06-18）；00631L 不配息、已分割（1→22，2026-03-31）；00685L 不配息、已分割（1→24，2026-07-07）。三次分割都已有官方證據，存放在 `data/reference/corporate-actions-20260928/` 和 `docs/research/00685l-source-acceptance-20261018.md`。

## 2. 名詞與原則

- **公司行動（corporate action）**：一筆由官方來源確認的分割或現金配息事實。
- 帳本只依「**官方事實 + 券商觀測**」入帳，不從價格跳動或股數比例反推事件。缺少官方事實時一律 fail-closed（對帳失敗並開 Operator Queue），不猜測。
- 分割不改變財富：股數乘比例，價格除以比例，NAV 不變。
- 配息在**除息日**成為應收（receivable），在**發放日**才轉成現金；兩者之間的 NAV 必須包含應收款，否則除息日到發放日之間仍會出現假虧損。

## 3. 事件格式

新增三種帳本事件，沿用現有的 `strategy_ledger_events`（`event_key` 唯一、`event_hash`、`payload`）：

| `event_type` | 發生時點 | 帳本變化 |
|---|---|---|
| `SPLIT` | 券商觀測到分割後股數，且已在登記期間內 | `positions[s] ×= ratio`；`last_prices[s] /= ratio`；`position_revision += 1`；NAV 不變 |
| `DIVIDEND_ENTITLEMENT` | 除息日（以除息日開盤前持有股數計算） | `receivables[action_id] = 持有股數 × 每單位金額`；NAV 不變 |
| `DIVIDEND_PAYMENT` | 發放日 | `cash += 實收金額`；刪除該筆 receivable；實收和應收的差額記為費用 |

`payload` 共同欄位：

```json
{
  "action_id": "0050:DIVIDEND:2026-07-16",
  "symbol": "0050",
  "kind": "DIVIDEND",
  "facts": {"ex_date": "2026-07-16", "payment_date": "2026-08-07", "amount_per_unit": "1.000"},
  "record_units": "1100",
  "amount": "1100.00",
  "source": {"registry": "reference/corporate-actions.json@sha256:…", "snapshot_id": "…"},
  "state_after": { … }
}
```

`SPLIT` 的 `facts` 為 `{"ratio": "4", "last_trade_date": …, "halt_start": …, "halt_end": …, "resume_date": …}`。

狀態新增 `receivables: dict[action_id, Decimal]`（JSON 欄位，需要 migration 0007，預設 `{}`）。NAV 改成 `cash + Σ股數×價格 + Σreceivables`，虧損與回撤指標都用這個 NAV。

## 4. 冪等

- `action_id` 由官方事實決定：`{symbol}:SPLIT:{resume_date}`、`{symbol}:DIVIDEND:{ex_date}`。
- `event_key` 為 `CA:{action_id}:{SPLIT|ENTITLEMENT|PAYMENT}`，資料庫的唯一鍵保證同一階段只會入帳一次。
- 重送時沿用 Phase 2 成交的做法：比對 `facts` 的 hash，相同就回傳 `created=false`，不同就回 409。來源修正（例如官方更正配息金額）不得覆寫已入帳事件，必須新增 `CORRECTION` 事件，等之後確有需要再設計。
- 寫入沿用 Phase 2 的 compare-and-swap（`position_revision`、`updated_at`）與重試。
- **順序**：每次帳戶同步時，先把截至同步日已到期的公司行動依日期套用，再做 `MARK`。補登成交時，若 `occurred_at` 早於該標的最後一次已套用的分割，一律拒絕（409），避免把分割前的股數記進分割後的帳本。

## 5. account_sync 如何辨識

**分割**：

1. 登記檔有該標的的分割事件，且同步日 ≥ `halt_start`，並且
2. 券商股數 = 帳本股數 × `ratio`（必須完全相等）。

兩者都符合才寫入 `SPLIT` 再對帳。如果券商在停牌期間仍顯示舊股數，就照常 `MARK`；停牌期間的價格只用於估值，不代表可以成交（研究模擬器已有同樣原則）。

如果在 `resume_date` 之後 3 個交易日內，券商仍未顯示分割後股數，就判為對帳失敗，原因為 `split-not-reflected-by-broker`。

如果股數剛好是整數倍但**沒有**登記事件，也判為對帳失敗，原因為 `unregistered-quantity-ratio (×N)`，並開 Operator Queue 請人工登記。系統不會自動入帳。

**配息**：不依賴券商。券商帳戶的現金混合了外部持倉，不能用來推算策略應收。帳本依登記事實在除息日計算應收；持有股數 = 目前股數扣掉 `occurred_at ≥ 除息日` 的 `FILL`。到發放日才入現金。

## 6. 資料來源

| 事件 | 主要來源 | 交叉核對 | 取得方式 |
|---|---|---|---|
| 現金配息 | TWSE ETF 配息列表 `https://www.twse.com.tw/en/ETFortune-institute/dividendList`（已有嚴格解析器 `official_distributions.parse_distributions`，含除息日、發放日、金額） | TWSE 除權息計算結果 `TWT49U`、發行人公告 | scheduler 每日擷取三檔當年度與下一年度，存成不可變快照（`corporate_action.twse_distributions`）後解析 |
| 分割 | TWSE 新聞稿 PDF、TWSE ETFortune 發行人公告（已有 3 筆完整證據） | 帳戶同步的股數比例 | **人工審核的登記檔** `reference/corporate-actions.json`，格式沿用 `normalized-splits.json`，每筆附來源網址與已保存原檔的 SHA-256；載入時驗證 hash，作法同 Phase 1 的交易日曆 |

注意事項：

- **更正（2026-11-11，核准後發現）**：TWSE 有機器可讀的「ETF 分割（反分割）恢復買賣參考價」查詢 `https://www.twse.com.tw/rwd/zh/split/TWTCAU?startDate=…&endDate=…&response=json`（見 `docs/research/alternative-return-data-acceptance-20261018.md`）。不過它只列恢復買賣日、代號、類型、停止前收盤價與參考價，**沒有比例欄位**（只能用收盤價 ÷ 參考價近似）；00631L 那一列的類型欄是空白；而且只在恢復買賣時才列出，公開表單最早只能查到 2024 年。所以比例仍以人工審核登記檔為準。TWTCAU 適合當自動交叉核對：例如發現未登記的股數倍數時，查它是否已列出事件。這部分列為後續工作，本次不實作。分割兩年只有 3 次，人工審核的成本可以接受。
- 配息解析器遇到完全重複的列會整份拒絕（`docs/research/alternative-return-data-acceptance-20261018.md` 中 006204 的案例）。已驗收過的有 0050（2016–2026 年，21 筆，見 `docs/research/official-distributions-audit-20260928.json`）和 00685L（2017–2026 年，0 筆）；00631L 還沒用這個解析器做過零筆驗收，實作時要補上。萬一遇到重複列，擷取會失敗，並開 Operator Queue，不會跳過。
- 無配息的標的（00631L、00685L）同樣要定期擷取。官方表格正確回傳零列才代表「沒有配息」，請求失敗不能當成零列。

## 7. 是否整併各模組的分割與費用邏輯

| 模組 | 分割 | 配息 | 費用 |
|---|---|---|---|
| `services/nav_proxy_research.py` | 股數 × 比例，非整數時報錯 | 除息日成為應收，發放日入帳 | 費率 × 成本倍數，有最低手續費，**不取整** |
| `services/account_replay.py` | 同上，並同步調整掛單股數 | 同上 | `max(20, 金額×0.001425)`，**不取整** |
| `services/historical_study.py` | 總報酬指數乘以比例 | 除息日以收盤價**立即再投入** | 無 |
| `services/return_attribution.py` | `split × (收盤 + 配息) / 前收 − 1` | 同左 | 無 |
| `domain/paper.py`（已刪除，沒有執行中的使用者） | 無 | 無 | 手續費與稅**四捨五入到整數元**，最低 20 元 |
| `domain/backtest.py` | 無 | 無 | 單邊費率，沒有最低手續費與證交稅 |

**建議：部分整併。**

- 新增 `domain/corporate_actions.py`，提供純函式與資料型別：`CorporateAction`、`apply_split(units, ratio)`（非整數時報錯）、`dividend_entitlement(units, amount)`、`units_held_at(positions, fills, day)`，以及登記檔載入器。**實際帳本改用這個模組。**
- 原提議以 `domain/paper.FeeSchedule` 作為正式路徑的費用來源；實作時帳本沒有採用，`domain/paper.py` 也因沒有執行中的使用者而刪除。帳本目前直接記錄成交回報上的手續費與稅，不另行推算。
- **研究模組這次不改。** 它們的語意不完全一致：配息有應收和立即再投入兩種，費用有取整和不取整兩種。而且 `reference/rearm-results.json` 記錄的歸檔結果依賴這些程式在當時的實作，改了就會讓歸檔結果與程式不再對應。要整併的話，應另開一個研究版本，重跑並比較差異。

## 8. 風控與介面影響

- 分割：NAV、曝險不變；停牌期間 `fill_available=false` 的原則同樣適用於紙上意圖。
- 配息：除息日 NAV 不再出現假跌，所以 40% 上限不會被誤觸。應收款在帳務頁顯示為一行「應收配息」。
- Operator Queue 新增三種待辦：`unregistered-quantity-ratio`、`split-not-reflected-by-broker`、`distribution-capture-failed`。

## 9. 測試計畫（實作時先寫失敗測試）

1. 0050 帳本 1,000 股，登記 1→4，券商顯示 4,000 股：應寫入 `SPLIT`、對帳成功、NAV 不變。
2. 同一情境重跑同步：不重複入帳，`created=false`。
3. 券商顯示 4,000 股但沒有登記事件：對帳失敗，原因為 `unregistered-quantity-ratio (×4)`。
4. 停牌期間券商仍顯示舊股數：照常 `MARK`，不入帳。
5. 除息日當天價格下跌、配息 1 元：NAV 不變（含應收），`principal_loss_ratio` 不變。
6. 發放日：現金增加、應收歸零；重跑同步不重複入帳。
7. 除息日當天之後才補登的買進：不影響應收股數。
8. 補登成交的時間早於已套用的分割：回 409。
9. 同一個 `action_id` 但金額不同：回 409。
10. 以上情境在 PostgreSQL 上同樣通過（沿用 Phase 4 的 `DVL_TEST_POSTGRES_URL`）。

## 10. 需要你決定的事

1. **配息實收金額**：二代健保補充保費（單次給付達門檻時扣繳）與匯費會讓實收金額小於應收。選項：(a) 以應收金額入帳，差額由操作員確認發放時修正；(b) 依規則自動扣除（費率與門檻需要先找到官方來源才能寫進程式）；(c) 新增「確認配息實收」端點，跟補登成交一樣由本人輸入。**建議 (c)**，未確認前以應收金額入帳，並在畫面標示「待確認」。
2. **分割資料來源**：只用人工審核登記檔（建議），還是先另外研究是否有官方、機器可讀的資料來源？
3. **整併範圍**：只整併正式路徑（建議），還是連研究模組一起改並重跑歸檔研究？
4. **零碎股（例如反分割）**：先一律對帳失敗，請人工處理現金差額（建議）；或現在就設計 `CASH_IN_LIEU` 事件。

## 11. 實作完成後

依原任務要求，把 README「值得看哪些工程設計」表格第二列改回「實際帳本也處理分割與配息」，英文摘要同步更新；並新增 ADR 0007 記錄「公司行動以官方事實 + 券商觀測入帳」的決定。
