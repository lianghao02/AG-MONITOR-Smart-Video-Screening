# HANDOFF

## 核心元資料 (Metadata)
- **Repository**：lianghao02/AG-MONITOR-Smart-Video-Screening
- **Branch**：main
- **Commit SHA**：c5602407995cfcbd77bf1f55426c0bef267b3b1b（本輪提交前基準；最新提交以 Git 記錄為準）
- **Skill Version**：v1.0.0
- **Task Type**：HANDOFF
- **Local Path Hint**：01_AG-MONITOR-Smart-Video-Screening

---

## 目前狀態
開發環境修復已驗證；本輪不包含正式發布。

## 本輪目標
依已授權計畫修復 Python 環境，保留既有功能與使用者資料。

## 基準與已確認事實 (Baseline & Confirmed Facts)
上述 SHA 為修復前已存在的 HEAD。既有功能成果承接原版本，不重做或撤銷；詳細跨專案基準位於控制中心 docs/python-environment-repair/baseline.json。

## 已完成 (Completed)
2026-10-06 GitHub 同步交接：使用者已授權提交與推送前輪成果；本輪只提交已核對範圍。最新 Commit SHA、遠端同步與 CI 結果統一見控制中心 `docs/github-sync/RESULTS.md`，不將提交本身的 SHA 寫入同一份提交。 本輪補正 PowerShell 5.1 中文腳本編碼：僅增加 UTF-8 BOM，原內容位元組不變；29 個相關腳本在 5.1／7 語法檢查均通過，環境 CheckOnly 亦通過。

2026-10-05 README 文件更新：補齊專案概念、開發原因、典型流程、已知 Bug／限制及回報方式，並依實際入口校正必要操作說明。本次沒有修改產品程式、環境或個人資料，未 Commit／Push；前輪成果與既有待辦繼承。文件檢核與逐案索引由控制中心 docs/readme-refresh/RESULTS.md 彙整，不代表本次重新驗收全部功能。

2026-10-05 目錄整理補充：保留現行配置、模型、captures、embedded 與現行發行，只清除入口及 tests 位元碼快取。CheckOnly/pip check 通過；保護與繼承修改見中央 docs/project-layout/verification.json，未 Commit/Push。

保留 embedded 3.13.0；啟動及未來建置驗證加入 -s，取消 3.11／退役專案 DLL 回退。requirements.txt 鎖定目前來源環境，portable-requirements 改為單一引用，正式發布清單保留。

## 異動檔案 (Changed Files)
AGENTS.md、setup_and_run.ps1、scripts/build_portable_release.py、requirements.txt、README.md、portable-requirements.txt。

## 刻意未修改 (Do Not Do / Deliberately Omitted)
未變更全域 Python／PATH／全域套件、既有發布包、業務演算法或原始資料；未 Commit／Push。07 原有兩個檔案修改保留且已核對雜湊。

## 尚未完成 (Remaining Work)
- **P1 (阻斷/必須)**：無已確認的現行開發環境阻斷。
- **P2 (重要/當次)**：本輪必要修復與驗證完成。
- **P3 (改善建議/暫緩)**：舊發布包不會因來源修正自動更新；下一次發布另驗證乾淨電腦與隔離入口。全域套件及共用 cv2 去重另案處理。

## 驗證結果 (Validation)
### 已執行測試與結果
44 項核心／工作程序測試通過，略過 2 項實體影片測試；CheckOnly、NoLaunch 與 pip check 通過。
### 尚未驗證項目
Win10／其他使用者／無全域 Python 電腦、完整原生介面互動、重新打包及正式更新切換，本輪未宣稱通過。
### 已知風險 (Known Risks)
完整明細與回復方式見控制中心 docs/python-environment-repair/RESULTS.md；不可將新 .venv 的驗證視為舊 Portable 包已修復。

## Git 狀態
- Commit：上述 SHA 為提交前基準；最新 SHA 見 `git log -1` 與中央同步報告。
- Push：實際推送及遠端核對結果見中央 `docs/github-sync/RESULTS.md`。
- Working Tree：最終狀態見中央同步報告；不含被忽略的環境、成品與使用者資料。
- Branch：main。

## 下一步建議動作 (Next Recommended Action)
正常使用既有入口；若未要求發布，停止擴大修改。日後提交須先核對工作範圍，07 既有修改不得混入本輪。正式發布前再完成發布門檻。

## 發布狀態 (Release Status)
本輪沒有建立新發布版；既有版本保留。
