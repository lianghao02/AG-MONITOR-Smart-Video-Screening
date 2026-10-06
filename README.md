# AG-MONITOR 智慧影像快篩系統

技術識別名稱：`AG-MONITOR-Smart-Video-Screening`

目前正式版本：`v4.0.1`

## 專案概念與開發原因

AG-MONITOR 把大量監視器影像中的候選活動整理成可人工檢視的截圖與追蹤結果。開發動機是逐支轉檔、逐段播放的成本高，且監視器裸流格式常讓一般播放器或剪輯軟體難以直接使用。

設計採 Python 影音與模型生態系，將解碼、AI 推論與影像寫入分工，提供 CPU 可攜環境，降低現場部署負擔。AI 負責縮小檢視範圍；事件判讀仍由使用者回看原片確認。

**典型流程**：加入影片 → 選擇模型與快篩設定 → 執行分析 → 檢查截圖 → 回看原始影像。

## 完整可攜版

一般使用者可從 [GitHub Releases](https://github.com/lianghao02/AG-MONITOR-Smart-Video-Screening/releases/latest) 下載 `AG-MONITOR-Smart-Video-Screening-v4.0.1-win-x64-portable.zip`。完整解壓後執行 `AG-MONITOR.exe`，不需另行安裝 Python、pip、CUDA 或模型。

可攜版內含 CPU 通用 Runtime、YOLOv8n 相容回退，以及 YOLO11n／11s／12n／12s 四個模型。使用者截圖與執行紀錄放在外層 `data/`，不會與程式檔案混在一起。

## 技術架構現況（2026-08-24）

本專案主力為 **Python 3.13**，以 PyAV、OpenCV、Ultralytics YOLO 與 LAP 組成智慧影像快篩與 AI 追蹤管線。因模型、影音解碼與演算法調校高度依賴 Python 生態系，現階段不進行 C#、Rust 或 Web 重寫；未來僅在量測出明確瓶頸時評估將個別 CPU 密集元件抽換為原生核心。

## 下載、依賴與啟動

- **系統**：Windows 10/11；主要開發版本為 Python 3.13。
- **核心套件**：PyAV、Eel、OpenCV、Ultralytics YOLO、lap；完整固定版本見 `requirements.txt`，可攜版補充套件見 `portable-requirements.txt`。
- **推薦啟動**：下載並解壓完整專案後雙擊 `RUN.bat`。若沒有 Python，`setup_and_run.ps1` 會建立專案內的 `python_embed` 並安裝依賴。
- **手動安裝（選用開發環境）**：`py -3.13 -m venv .venv`，再以 `.venv\Scripts\python.exe -s -m pip install -r requirements.txt` 安裝，並以 `.venv\Scripts\python.exe -B -s main.py` 啟動。此流程與 RUN.bat 的 embedded 環境分開維護。
- **網路需求**：原始碼首次建立環境、安裝套件或取得 YOLO 模型時需要網路；GitHub Release 完整可攜版可離線啟動。
- **打包／移機**：保留完整專案與已建好的 `python_embed`；不要只複製 `main.py`。模型與原始影片不應提交至 GitHub。

## 專案簡介
專為大量監視器影片調閱設計，通吃 `.h265`、`.dav`、`.264`、`.avi` 等非標準監視器裸流，結合 AI 追蹤、空景快轉、靜止車輛過濾與全景精華截圖。

### ✨ 核心強項與獨家演算法
- **零拷貝極速解碼**：專為非標準監控影音流 (Raw Stream) 打造，跳脫傳統辦案轉檔曠日費時的痛點。
- **智慧空景快轉 (Time-Jump)**：無人畫面自動幻燈片躍遷；一旦偵測到目標，無縫切換回流暢實時追蹤。
- **台灣常見車種全覆蓋**：精準辨識人(Person)、單車(Bicycle)、一般汽車(Car)、機車(Moto)、公車客運(Bus)、大貨車(Truck)。
- **實務級抗干擾追蹤 (Anti-Jitter)**：搭載「絕對位移判定演算法」與「機車騎士智慧融合邊界框」，降低路面標線抖動誤報與人車分離雙重截圖。
- **全景精華截圖**：新移動目標進場時以完整監視器畫面保存，同波人車合併為一張，並可選擇是否烙印 AI 彩色標註框。
- **全速 Headless 快篩**：批次處理時可停止逐幀預覽傳輸，讓 PyAV 解碼、YOLO 推論與非同步影像寫入各自運作。

## 快速開始
**一鍵啟動（推薦）**
直接雙擊專案目錄下的 `RUN.bat` 即可全自動啟動！
*若環境中缺少 Python，系統會自動尋找可攜執行環境。*

**手動啟動指令**
```bash
python_embed\python.exe -B -s main.py
```

`RUN.bat` 會使用專案自己的 `python_embed`。僅建置／檢查環境、不開啟介面可執行：

```bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_and_run.ps1 -NoLaunch
```

## 快篩輸出

自動分析不會在開始前掃描整支影片計算 SHA-256，也不產生截圖 JSONL 清冊。截圖直接輸出至 `captures/`，檔名格式為 `{影片名稱}_{時分秒}_ID{首個目標ID}_{類型}.jpg`。安全批次重新命名仍保留獨立雜湊交易，避免檔案搬移時發生內容錯置。

## 自動化測試

將測試影片放入被 Git 忽略的 `input_videos/`，再以可攜核心執行：

```bat
python_embed\python.exe -B -s tests\test_forensic_core.py -v
python_embed\python.exe -B -s tests\test_process_context.py -v
python_embed\python.exe -B -s tests\test_real_videos.py -v
```

## 技術棧
- **PyAV** (零拷貝全記憶體極速解碼，解決壞軌容錯)
- **Ultralytics YOLOv8 & ByteTrack** (強健的多目標辨識與軌跡追蹤)
- **OpenCV**（ROI、動態引導、影像標註與 JPEG 編碼）
- **Eel** (輕量化 WebSocket 前後端通訊與現代化 UI 渲染)

後續功能規劃與實作紀錄請參閱 [`docs/FUTURE_ROADMAP.md`](docs/FUTURE_ROADMAP.md) 與 [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)。

## 授權與公開聲明

本專案以 [GNU Affero General Public License v3.0](LICENSE) 公開。發行包包含多項第三方開放原始碼元件與 Ultralytics YOLO 模型；個別著作權與授權資訊請參閱 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

本工具僅供影像快速篩選與人工研判輔助，不保證偵測結果零漏失或零誤報，也不取代原始影片、依法定程序保存的資料或承辦人專業判斷。

## 快篩安全與驗證補充

- 截圖採零覆寫命名，保留原始解析度與監視器角落 OSD；預設不修改畫面內容。
- 正常停止會排空 Writer Queue；強制停止會立即捨棄尚未寫入的截圖事件。
- Base64 影像輸入設有型別、格式及容量限制；寫檔失敗時不會回報成功。
- 預設埠號被占用時會依序嘗試備援埠號。
- Python 3.13 為主要開發與驗證版本。
- AI 偵測結果僅供人工快篩與研判，不能取代原始影片或承辦人判斷。

## 環境修復後的操作邊界

主要入口仍為 RUN.bat，使用專案 embedded 3.13 並明確停用使用者套件。`setup_and_run.ps1 -CheckOnly` 僅檢查；`-NoLaunch` 是建置及驗證。手動執行來源請使用 `python_embed\python.exe -B -s main.py`，套件操作使用相同直譯器的 `-s -m pip`。若自行建立 .venv，先啟用後再使用其 `python.exe -s`。既有損壞 runtime 不會自動刪除；來源依賴已鎖定，正式發布仍依 requirements-release.txt。舊 dist 本輪保留，不代表已套用新入口隔離。

## 已知 Bug、限制與疑難排解

以下區分已確認問題、功能限制及待驗證項目；歷史修正不代表舊發行包已自動更新，也不代表本次文件更新重新完成所有功能測試。

| 狀態 | 情境 | 處理方式 |
|---|---|---|
| 功能限制 | 夜間、遮擋、低解析度或特殊車型可能漏抓、誤抓。 | 抽查未命中的片段並回看原片；不能把無候選結果當成無事件。 |
| 輸入限制 | 裸流毀損、時間資訊缺失或解碼器不支援。 | 保留來源，記錄格式與錯誤；解碼失敗與沒有偵測目標應分開判斷。 |
| 操作限制 | 強制停止與正常停止的寫入行為不同。 | 正常停止會處理待寫入佇列；強制停止可能捨棄尚未寫出的事件，檢查現有輸出後再續作。 |

已修復紀錄包括重複啟動、啟動失敗後介面狀態、中文檔名與追蹤器重設等問題，詳見 [CHANGELOG.md](CHANGELOG.md)。可攜版、來源程式與使用者自行建立的環境應分別驗證；舊執行檔不會自動包含來源修正。

### 問題回報

請提供使用版本／啟動方式、作業系統與相關環境、重現步驟、預期及實際結果，以及去識別的錯誤訊息或最小樣本。先保留現場與來源資料；不要附真實案件、完整帳號、密碼、Token 或 API Key。版本修正以對應原始碼與發行包為準。
