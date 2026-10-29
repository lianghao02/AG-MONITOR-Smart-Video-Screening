# 協作規則

本專案共用程式、測試、文件與 CI；Codex 讀取 `.agents/AGENTS.md`，Antigravity 讀取 `.gemini/AGENTS.md`。

- 交接共用單一 `HANDOFF.md`；專案長期邊界見根目錄 `AGENTS.md`。
- 提交前執行 `scripts/qa.ps1`，並依 `README.md` 的測試方式驗收。
- 核心測試：`python_embed\python.exe -B -s tests\test_process_context.py`。
- `tools/` 為一次性研究腳本（追蹤器基準、遮蔽掃描），依賴 `input_videos/` 的實體影片，不屬於單元測試。
- 歷史實作紀錄封存於 `docs/archive/`，現行規格以 `README.md` 與 `CHANGELOG.md` 為準。
