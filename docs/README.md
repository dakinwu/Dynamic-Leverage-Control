# 文件索引與使用路徑

本頁是專案文件的入口。先依照「使用者操作」閱讀；要了解系統應該長什麼樣，再閱讀「產品與營運規格」；要確認目前實際完成度，則以「差距盤點」為準。

## 我現在要怎麼操作？

### 第一次啟動

1. 閱讀根目錄 [README.md](../README.md) 的「免帳號展示」。
2. 若要同步 Shioaji 唯讀帳戶，依 README 的「設定 Shioaji 唯讀帳戶」執行 secrets wizard。
3. 開啟 `http://localhost:5173`，先確認總覽的啟用清單、資料新鮮度與帳戶同步狀態。

### 每日使用

1. 進入「總覽」，先看 Operator Status 與一句話結論。
2. 若狀態是 `Normal`，確認下次排程與資料新鮮度即可。
3. 若狀態是 `Attention`、`Action Required` 或 `Blocked`，開啟 Operator Queue 的最高優先項目。
4. 查看原因、影響、證據與建議下一步；必要時重新同步、手動重算或啟用 Manual Halt。
5. 若需要研究策略，進入「策略」頁切換 Shadow/Paper 候選，查看動態槓桿工作表與回測資料。
6. 曝險偏離目標時，先按「建立紙上調整預覽」，確認配置後才建立 Paper 證據；不會送出券商委託。
7. 交易日 09:05 與收盤後會嘗試唯讀同步帳戶；若要立即更新，可在「帳務」頁按手動同步。

### 發生問題時

- 資料過期或缺欄位：先看「資料」頁，再看 [DATA-PROCESSING.md](DATA-PROCESSING.md)。
- 風控阻擋或需要暫停：看「風控」頁，再看 [OPERATOR-CONSOLE.md](OPERATOR-CONSOLE.md) 的 Risk Gate 與 Manual Halt。
- 帳戶、NAV、T+2 或對帳問題：看「帳務」頁；帳戶邊界定義見 [CONTEXT.md](../CONTEXT.md)。
- 不確定目前是否已完成：看 [PRODUCT-GAP-AUDIT.md](PRODUCT-GAP-AUDIT.md)，不要只看目標規格。

## 文件分工

公開研究入口為[研究方法與限制](RESEARCH.md)。完整研究工作目錄只保留本機，不在公開副本中；既有研究腳本需要的原始擷取與索引不是展示依賴。開始本機新實驗前仍須查原工作紀錄，避免重掃同條件。

| 文件 | 唯一責任 | 何時閱讀 |
|---|---|---|
| [README.md](../README.md) | 安裝、啟動、日常操作、CLI、FAQ | 第一次使用或排錯 |
| [PRODUCT.md](PRODUCT.md) | 產品邊界、策略候選、計算慣例、安全上限與交付目標 | 修改產品行為前 |
| [OPERATOR-CONSOLE.md](OPERATOR-CONSOLE.md) | 分頁責任、操作流程、Queue、Freshness、風控 UX | 修改介面或操作流程前 |
| [DATA-PROCESSING.md](DATA-PROCESSING.md) | 官方資料抓取、解析、回補與失效處理 | 修改資料管線或 scheduler 前 |
| [PRODUCT-GAP-AUDIT.md](PRODUCT-GAP-AUDIT.md) | 目前已完成、部分完成與尚未完成的能力 | 判斷現況與排程下一階段 |
| [CONTEXT.md](../CONTEXT.md) | 專有名詞與邊界定義 | 看到名詞歧義時 |
| [RESEARCH.md](RESEARCH.md) | 公開研究方法、證據界線與重現限制 | 評估研究可信度時 |
| [OPERATIONS.md](OPERATIONS.md) | 選配唯讀帳戶、資料回補與排程 | 免帳號展示之後才閱讀 |
| [PUBLIC-REVIEW.md](PUBLIC-REVIEW.md) | 公開前實際檢查與未涵蓋範圍 | 對外提供檢視前 |
| `docs/adr/*` | 已採用且需保留的架構決策 | 需要改變核心設計時 |

## 文件權威順序

1. 使用方式：`README.md`
2. 產品與安全目標：`PRODUCT.md`、`OPERATOR-CONSOLE.md`
3. 實際完成度：`PRODUCT-GAP-AUDIT.md`
4. 資料管線細節：`DATA-PROCESSING.md`
5. 公開研究依據：`docs/RESEARCH.md`；完整本機原件不在公開副本
6. 架構決策：`docs/adr/*`

若規格與現況不同，這通常代表功能尚未完成；以差距盤點為準，不要自行把規格推論成已上線能力。
