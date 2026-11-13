# 帳戶、排程與進階操作

本頁供選擇啟用外部資料／唯讀帳戶的操作者使用。免帳號展示先依[根目錄README](../README.md)。以下金額與成交JSON均為虛構教學範例，不能當作使用者實際帳務。需要Docker的指令先依README完成啟動。

## 設定 Shioaji 唯讀帳戶

你已有 `SJ_API_KEY` 與 `SJ_SECRET_KEY` 時，在 Git Bash 執行：

```bash
# 在專案根目錄執行
./scripts/ops/setup-secrets.sh
```

wizard 會依序處理：

1. 儲存 API key／secret（輸入不會顯示）。
2. 登入 Shioaji 唯讀列出股票帳戶。
3. 顯示遮罩帳號，例如 `0  9A9B-***9014  signed`。
4. `Account number to bind` 請填清單左側的索引，例如 `0`；不是手動輸入完整帳號。
5. CA 憑證目前可留白，因為正式下單已關閉。
6. Telegram 與 FinMind 都是選填。

完成後 secrets 會放在 Windows 的 LocalAppData 外部目錄，不會寫入 repository。重新啟動：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\ops\run-local.ps1 -Build
```

確認唯讀連線與合約：

```powershell
docker compose exec api dvl shioaji-check
```

帳戶同步只會觀察你綁定的帳戶。非策略標的會保留在帳務畫面，但平台不會賣出、接管或納入策略曝險。

第一次成功同步會以 `INITIAL_STRATEGY_NAV`（目前為 500,000 元）建立策略台帳，並把當下的 0050／00631L／00685L 持倉視為策略起始持倉。策略現金等於 500,000 元減去這些持倉的當時市值；券商帳戶的其他股票、整體現金與 T+2 待交割不會被當成策略損益。這表示績效觀察從台帳初始化時開始，不會倒推初始化以前的歷史獲利或虧損。

本人在券商完成實際成交後，須將成交回報登錄到台帳，再重新執行唯讀同步。`execution_id` 應使用券商成交編號或另一個不重複的識別值；重送相同值不會重複扣款：

```json
POST /api/v1/strategy-ledger/fills
{
  "execution_id": "broker-fill-001",
  "instrument": "0050",
  "side": "BUY",
  "quantity": "1000",
  "price": "55.00",
  "commission": "78",
  "tax": "0",
  "occurred_at": "2026-09-08T10:15:00+08:00",
  "note": "本人確認的券商成交"
}
```

若未登錄成交就出現策略持股數量變化，系統會標成 `reconciliation-break`（對帳中斷）並關閉曝險判定。可用 `GET /api/v1/strategy-ledger` 查看策略現金、NAV、本金虧損與回撤。

## 五個分頁怎麼用

### 總覽

先看三個數字：

- `Target Exposure`：目前選用策略計算出的目標曝險。
- `Strategy Exposure`：允許策略標的的名目曝險 ÷ 策略 NAV（初始投資資本預算為 500,000 元）；外部持倉不會稀釋策略曝險。策略開始產生損益後，必須使用當期策略淨值，不能繼續用初始預算當分母。
- `Observed NAV`：券商帳戶現金加上全部持倉市值，外部持倉仍會保留在這個帳戶真實值。
- `Account Exposure`：允許策略標的的名目曝險 ÷ Observed NAV，只作帳戶層級參考，不作策略再平衡分母。
- `Gap`：目標與實際的差距，只是監控資訊，不是下單指令。
- `本金虧損率`：目前策略 NAV 低於淨投入本金的比例。
- `目前回撤`：目前策略 NAV 從歷史高點下跌的比例；`歷史最大回撤` 則保留曾經發生過的最大值。

若看到 `planning`，表示尚未取得可信的券商帳戶快照；若看到 `DATA_UNSAFE`，先到「資料」處理缺口，不要把畫面上的舊數字當成新訊號。

總覽中的 Operator Queue 使用 `OPEN → ACKNOWLEDGED → RESOLVED` 生命週期；「已看」不代表問題已經解決。曝險偏離目標時，先按「建立紙上調整預覽」，確認比例後才保存策略／風控／意圖證據鏈。這裡的 Paper 是紙上交易模擬，不是券商委託。

### 策略

上方按鈕可以選擇並套用策略版本（只作用於 Shadow／Paper）：

- `constant-1.2-v1`：固定 1.2x 基準線。
- `constant-2.0-v1`：固定 2.0x 研究基準線，只能作用於 Shadow／Paper。
- `dvl-tw-v1.1-raw`／`dvl-tw-v1.1-winsorized`：研究用估值候選，日常只在策略頁比較。
- `dvl-tw-v2-shadow`：文章概念的研究版本，依 P/E percentile、EPS YoY、RV20、Taiwan VIX、回撤與資金成本計算候選目標。

v2 的候選上限是 3.2x，但現金帳戶可執行上限仍是 2.0x。畫面上的 diagnostics 會列出估值目標、波動上限、回撤上限及資金成本上限，方便逐項檢查。

若資料不足，策略會顯示 `DATA_UNSAFE` 與缺少欄位，不會用預設值補出訊號。

DVL V2 另外要求目前月份之前至少 120 個有效的 point-in-time 估值月點。資料頁會把「最新快照新鮮度」與「歷史完整度」分開顯示，例如 `46 / 120`；完整度未達標時，首頁顯示「尚未完成評估」，不會沿用前一次目標。

策略工作表的每列會列出估值區間、候選槓桿、0050／00631L／現金比例與資料新鮮度。Shadow 候選超過 2.0x 只供研究，不能變成現金帳戶的執行目標。

### 資料

這裡查看 scheduler 保存的官方 raw snapshots。每筆資料都有來源、觀測時間、接收時間與 checksum。資料處理流程會：

- 解析 TWSE TAIEX 收盤與日報酬。
- 累積至少 21 個不同交易日後計算 RV20。
- 解析 TAIFEX Taiwan VIX（頁面只提供下載連結，scheduler 會抓取官方 `getVixData` 檔案的 `Last 1 min AVG`）。
- 每日收盤嘗試抓取 TWSE 上市公司月報的「大盤／TAIEX 本益比」與 MOPS `t187ap14_L` 全體 EPS；解析失敗時保留上一筆可信快照並 fail-closed。
- 保存 00631L／00685L PCF 原文；可解析的實際曝險只作診斷，不用 ETF 市價直接乘 2。

### 風控

放行流程依序檢查資料新鮮度、帳戶對帳、曝險／交易上限，再確認委託層。任何一層失敗都會 fail-closed；目前最後一層固定顯示正式下單 `disabled`。

本金虧損率或歷史最大回撤任一觸及 40% 時，Risk Gate（下單建議前的風險閘門）只允許降低曝險；正常維持與增加曝險會被 `CAPITAL_LOSS_LIMIT` 阻擋。這是風險控制規則，不保證實際成交後一定不會穿越 40%。

## scheduler 與資料處理

scheduler 每 15 分鐘心跳一次，並使用交易日語意記錄「交易日 09:05 唯讀帳戶同步、09:15 Paper 評估、收盤後更新」的下次時間；不會把週末當成交易日。每次策略執行都會建立新的 `run_id`，即使快照內容沒有變化。

抓取來源：

- [TWSE OpenAPI](https://openapi.twse.com.tw/)
- [TAIFEX Taiwan VIX](https://www.taifex.com.tw/cht/7/vixMinNew)
- [元大 00631L PCF](https://www.yuantaetfs.com/tradeInfo/pcf/00631L)
- [群益 00685L PCF](https://www.capitalfund.com.tw/etf/product/detail/094/portfolio)

常見事件：

- `*_ingested`：快照已保存。
- `strategy_evaluated`：輸入完整，已產生 v2 Shadow 決策。
- `strategy_evaluation_skipped`：缺少必要資料，刻意不產生有效訊號。
- `*_failed`：單一來源失敗；其他來源仍會繼續，下一個週期重試。

查詢狀態 API：

```text
GET /api/v1/operations/queue
GET /api/v1/data/freshness
GET /api/v1/automation/status
GET /api/v1/paper/evidence/{run_id}
GET /api/v1/strategy-ledger
POST /api/v1/strategy-ledger/fills
```

Manual Halt 只會停止新的紙上交易模擬，資料同步與對帳仍會繼續：

```text
POST /api/v1/risk/manual-halt
POST /api/v1/risk/manual-halt/release
```

若 secrets 含 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID`，平台會在以下狀態變化時傳送去重通知：Scheduler 成功／降級、必要資料過期或恢復、唯讀帳戶同步失敗或恢復、Risk Gate 阻擋／放行、Manual Halt 啟用／解除，以及 Operator Queue 新增／ACK／RESOLVED／REOPENED。未設定時不會阻擋資料處理，也不會在訊息中包含完整帳號、持倉或金鑰。通知先與觸發它的狀態變更一起寫入`notification_outbox`，交易提交後才由API背景工作或scheduler送出；提交失敗時API回5xx且不送通知。

手動執行一次：

```powershell
docker compose exec scheduler dvl scheduler --once --interval-seconds 60
```

## 匯入估值資料（方案 A：官方自動抓取，CSV 作為核准後備）

Scheduler 每個交易日收盤會執行方案 A：抓取 [TWSE 上市公司月報](https://www.twse.com.tw/zh/trading/statistics/index04.html) 的明確大盤／TAIEX P/E 列，再抓取 [MOPS/TWSE `t187ap14_L` EPS](https://openapi.twse.com.tw/) 的全體 EPS 列，兩者以同一 `as_of` 組成不可變估值快照。系統**拒絕**只有個股資料的回應，不會用個股 P/E 平均冒充大盤數字。若 MOPS 暫時無法連線或沒有全體列，會改用同一官方快照的 `TAIEX 收盤指數 ÷ TWSE 大盤 P/E` 推導市場 EPS，並在證據鏈明確標示公式、來源與 MOPS 錯誤；不會靜默混用個股資料。

首次部署不要只等未來資料累積，可直接執行官方歷史回補：

```powershell
docker compose run --rm scheduler dvl backfill-official --months 121 --index-days 90
```

它會補目前月與之前 120 個月的估值，以及近 90 日 TAIEX。回補會先讀取資料庫、跳過已存在的估值月份，並在每次 TWSE 請求間節流；若官方歷史端點暫時限流，scheduler 會每 15 分鐘以小批次繼續補缺月，checksum 仍會避免重複資料。

TWSE 偶爾會調整月報下載路由，可在 `.env` 覆寫：

```text
TWSE_MARKET_VALUATION_URL=https://www.twse.com.tw/zh/trading/statistics/index04.html
MOPS_MARKET_EPS_URL=https://openapi.twse.com.tw/v1/opendata/t187ap14_L
```

若 TWSE 月報或 TAIEX 指數都無法取得，scheduler 會記錄 `taiex_valuation_failed`、保留上一筆資料，並阻擋需要新鮮估值的增加曝險。仍可在人工覆核後使用版本化 CSV 作為後備。複製範本：

```powershell
Copy-Item .\data\templates\valuation.csv .\data\private\valuation.csv
```

CSV 欄位：

```csv
as_of,pe_ttm,market_eps,source,source_version
2025-08-01,20.0,800.0,manual-reviewed,2025-08
2026-08-01,24.0,900.0,manual-reviewed,2026-08
```

至少準備相隔約一年的兩筆資料，才能計算 EPS YoY。匯入：

```powershell
docker compose exec api dvl ingest-valuation /app/data/private/valuation.csv
```

估值資料只可授權 Shadow/Paper 研究，不代表已通過正式交易 promotion gate。

## 策略工作表怎麼看

策略頁會顯示一張文章式的「估值 → 點位 → 槓桿」工作表。每列先用 `市場 EPS × TTM P/E` 算出研究用點位，再對照 P/E 區間的候選槓桿；右側的 v2 目標只有在 P/E percentile、EPS YoY、RV20、VIX 都有新鮮資料時才會填入。這張表是可稽核的研究假說，不是報酬保證，也不會直接下單。

表格下方的目前輸入卡會顯示策略實際採用的 TTM P/E、P/E percentile、EPS YoY、RV20/VIX。若顯示「等待資料」或策略狀態為 `DATA_UNSAFE`，先到「資料」頁確認快照，或依本文件匯入 point-in-time valuation CSV；不要把固定 1.2x 當成動態訊號。

## CLI 常用指令

```powershell
docker compose exec api dvl evaluate-demo
docker compose exec api dvl show-recipes 1.2
docker compose exec api dvl ingest-twse
docker compose exec api dvl shioaji-accounts
```

## 測試與品質檢查

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
pytest -q -p no:cacheprovider
ruff check src tests migrations
ruff format --check src tests migrations
```

前端：

```powershell
cd frontend
npm run typecheck
npm run build
```

## 常見問題

**`Could not discover Shioaji accounts`**

先啟動 Docker stack，再重新執行 wizard。若 API key 尚未完成 Shioaji 登入／簽署測試，帳戶清單不會出現。

**帳戶同步 503**

確認 `SHIOAJI_MODE=readonly`、secret file 存在、帳戶 binding 正確，並查看 `docker logs dynamic-leverage-control-api-1`。

**畫面又顯示「需先設定帳戶 ID」**

請確認 Docker Desktop 已重啟到最新 Compose 設定；Compose 未設定 `DVL_SECRET_FILE` 時只掛載空白範本 `config/empty-secrets.env`，不會自動讀取個人憑證；要使用 `%LOCALAPPDATA%\dynamic-leverage-2x\secrets\runtime.env`，請在 `.env` 設定 `DVL_SECRET_FILE` 指向它，或使用會自動設定的 secrets-aware launcher。指定的檔案不存在時 Compose 會直接報錯，不會建立同名資料夾。若仍看到舊狀態，從專案根目錄執行 `docker compose up -d --build`，或使用 launcher：`powershell -ExecutionPolicy Bypass -File .\scripts\ops\run-local.ps1 -Build`。

**策略顯示缺少 `rv20`**

這代表目前少於 21 個不同 TAIEX 交易日快照。這是安全阻擋，不是程式錯誤；不要用手動常數冒充 RV20。

**元大 PCF 偶爾 `RemoteProtocolError`**

發行商頁面可能暫時拒絕連線。scheduler 會保留既有快照、繼續其他來源並在下一輪重試。


## 研究資料界線

公開研究方法見[RESEARCH.md](RESEARCH.md)。完整來源擷取、帳務CSV、私人研究紀錄與歷史輸出只留本機，不隨公開副本提供；相關舊研究腳本不能僅靠公開檔案重現。

## 分割與配息（策略台帳）

- **配息**：scheduler 每次執行都會擷取 TWSE ETF 配息表（0050、00631L、00685L）。除息日記為應收配息，發放日暫以應收金額轉為現金，並在 Operator Queue 提醒到帳務頁輸入銀行實際入帳金額。擷取失敗或無法解析時會開待辦，不會當作沒有配息。
- **分割**：只有登記在 `src/dynamic_leverage/reference/corporate-actions.json` 的分割會入帳，而且要等券商顯示的股數剛好等於「帳本股數 × 比例」。新增分割時，先保存官方新聞稿與發行人公告原檔，把比例與日期寫進登記檔，再重新計算 `corporate-actions-provenance.json` 的 SHA-256（以 LF 換行計算），然後重新同步帳戶。未登記的整數倍股數只會造成對帳失敗並開待辦。
- 補登成交的時間若早於已入帳的分割或除息，會被拒絕（409），因為它會改變已入帳事件的股數。
