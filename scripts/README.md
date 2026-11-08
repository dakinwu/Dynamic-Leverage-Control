# 命令入口

先在根目錄依README安裝環境。免帳號入口：

```powershell
python -m dynamic_leverage.cli evaluate-demo
python -m dynamic_leverage.cli show-recipes 1.2
python -m pytest
python scripts/check_public_files.py
```

- `run-local.ps1`：選配Docker launcher，可能讀取本機外部憑證，不作公開展示預設入口。
- `setup-secrets.sh`：使用者自行設定真實唯讀帳戶的選配工具；不需執行即可測試專案。
- `check_public_files.py`：掃描Git已追蹤及未忽略的候選檔案，只回報命中類別／位置，不列印金鑰內容；不等於完整機密鑑識。
- `collect_*`、`audit_*`、`freeze_*`、`run_*study*`及研究索引工具：保留原研究程式，部分依賴已被排除於公開副本的原文、資料及研究規格。不要在缺資料時移除指紋檢查、套用其他來源或聲稱已重現原績效。

公開研究界線見[研究說明](../docs/RESEARCH.md)。公開副本缺少私人來源是明確限制，不是程式自動下載大量歷史資料的授權。
