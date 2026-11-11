# 命令入口

先在根目錄依README安裝環境（`python -m pip install -e ".[dev,research]"`）。腳本直接匯入已安裝的`dynamic_leverage`套件，不再修改`sys.path`；請在專案根目錄執行。免帳號入口：

```powershell
python -m dynamic_leverage.cli evaluate-demo
python -m dynamic_leverage.cli show-recipes 1.2
python -m pytest
python scripts/ops/check_public_files.py
```

## `ops/`：營運與維護

- `run-local.ps1`：選配Docker launcher，可能讀取本機外部憑證，不作公開展示預設入口。
- `setup-secrets.sh`：使用者自行設定真實唯讀帳戶的選配工具；不需執行即可測試專案。
- `Backup-DvlDatabase.ps1`：本機資料庫備份；若先前已註冊排程，搬移後需重新註冊。
- `check_public_files.py`：掃描Git已追蹤及未忽略的候選檔案，只回報命中類別／位置，不列印金鑰內容；不等於完整機密鑑識。
- `verify_*_runtime.py`：在本機API容器內執行的驗收檢查，只輸出去識別化結果。
- `capture_valuation_evidence.py`、`audit_valuation_observations.py`：在獨立驗收資料庫擷取／檢視官方估值觀測，不連券商。

## `research/`：研究程式

`collect_*`、`audit_*`、`freeze_*`、`run_*study*`及研究索引工具：保留原研究程式，部分依賴已被排除於公開副本的原文、資料及研究規格。不要在缺資料時移除指紋檢查、套用其他來源或聲稱已重現原績效。互相匯入的腳本（例如`run_risk_replay.py`使用`run_historical_study.py`）放在同一資料夾，以`python scripts/research/<name>.py`執行即可。

`src/dynamic_leverage/reference/rearm-results.json`記錄的`scripts/*.py`指紋，是原始研究執行當時（commit `72c1197`）的路徑與內容；之後的搬移只改了匯入方式，原始版本可從該commit取得。

公開研究界線見[研究說明](../docs/RESEARCH.md)。公開副本缺少私人來源是明確限制，不是程式自動下載大量歷史資料的授權。
