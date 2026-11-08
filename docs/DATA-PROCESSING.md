# 官方資料處理階段（MVP）

## scheduler 會做什麼

每個週期會把 TWSE 日行情、TAIEX 指數、TAIFEX Taiwan VIX，以及元大 00631L／群益 00685L PCF 以 append-only raw snapshot 保存。相同來源與內容以 SHA-256 去重；任一來源失敗不會阻塞其他來源。

接著 `strategy_pipeline` 只讀取這些不可變快照：

- TAIEX：解析收盤點位、日報酬與歷史高點回撤。
- RV20：需要至少 21 個不同交易日收盤，使用 20 個簡單收盤報酬的年化樣本標準差。
- Taiwan VIX：先讀取 TAIFEX 月頁的最新 `getVixData?filesname=YYYYMMDD` 下載連結，再解析官方 Big5 文字檔的 `Last 1 min AVG`；只有拿到合理數值才接受。
- 估值：方案 A 每個交易日收盤抓取 TWSE 月報的大盤／TAIEX P/E、MOPS `t187ap14_L` 全體 EPS 與 TWSE TAIEX 收盤指數，合併成 `valuation.taiex.manual` 的 point-in-time 快照。MOPS 沒有全體列或暫時不可達時，使用 `TAIEX 收盤指數 ÷ TWSE 大盤 P/E` 的透明 fallback；個股欄位永不平均成市場 EPS。TWSE 月報或指數都失敗時才拒絕寫入並保留上一筆。CSV 仍是人工覆核後備。P/E percentile 只使用截至當月前的歷史點；EPS YoY 必須找到約一年前的已公布 EPS。
- PCF：先保存原文；可解析的曝險只做診斷，不拿 ETF 市價乘 2 代替實際曝險。

完整輸入齊全時，scheduler 會評估 `dvl-tw-v2-shadow` 並保存輸入 snapshot IDs、每個 cap 與理由。缺資料時只輸出 `strategy_evaluation_skipped`，策略目標明確為 NULL，不會沿用前一次目標、不會補預設值，也不會產生下單指令。候選目標可到 3.2x，但超過 2.0x 永遠維持 Shadow/Paper。

DVL V2 的歷史完整度門檻是目前月份以前 120 個有效 point-in-time 估值月點；回補需包含目前月，因此預設抓取 121 個月。最新快照即使是 FRESH，也不代表歷史資料已足夠。交易日 09:05 與收盤後 scheduler 會在設定檔具備唯讀帳戶綁定時嘗試同步 Shioaji，失敗會留下獨立的 `account_sync_failed` 事件。

## 目前預期看到的事件

在剛啟動、尚未累積 21 個 TAIEX 交易日、官方估值端點暫時無法解析或尚未匯入 CSV 時，scheduler log 會出現：

```json
{"event":"strategy_evaluation_skipped","missing":["pe_percentile","eps_yoy","rv20"]}
```

這是刻意的 fail-closed 狀態，不是錯誤。累積資料後，事件會變成 `strategy_evaluated`；相同輸入快照再次執行則只輸出 `strategy_evaluation_unchanged`。

## 匯入估值資料

官方方案 A 失敗時，才使用人工審核、版本化 CSV。複製 `data/templates/valuation.csv` 到 gitignored 的 `data/private/valuation.csv`，至少提供跨約一年的兩筆 point-in-time 資料，再執行：

```powershell
docker compose exec api dvl ingest-valuation /app/data/private/valuation.csv
```

自動與 CSV 兩條路徑都只授權 Shadow/Paper 研究，不可授權正式交易；每筆快照會保留來源 URL、兩個 checksum、as_of 與解析方法。
