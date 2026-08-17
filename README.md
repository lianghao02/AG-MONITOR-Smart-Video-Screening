# AG-MONITOR 科技偵查・智慧雙軌鑑識工作站 v3.1.0

## 下載、依賴與啟動

- **系統**：Windows 10/11；主要開發版本為 Python 3.13。
- **核心套件**：PyAV、Eel、OpenCV、Ultralytics YOLO、lap；完整固定版本見 `requirements.txt`，可攜版補充套件見 `portable-requirements.txt`。
- **推薦啟動**：下載並解壓完整專案後雙擊 `RUN.bat`。若沒有 Python，`setup_and_run.ps1` 會建立專案內的 `python_embed` 並安裝依賴。
- **手動安裝**：`py -3.13 -m venv .venv`，啟用後執行 `python -m pip install -r requirements.txt`，再執行 `python main.py`。
- **網路需求**：首次建立環境、安裝套件或下載 YOLO／Real-ESRGAN 模型時需要網路；已備妥 `python_embed`、套件與模型後可離線啟動。
- **打包／移機**：保留完整專案與已建好的 `python_embed`；不要只複製 `main.py`。模型與證物資料不應提交至 GitHub。

## 專案簡介
專為台灣警務鑑識實戰設計，通吃 `.h265`, `.dav`, `.264`, `.avi` 等極端監視器裸流的無損戰術播放器；並結合雙模態 AI 蒐證追蹤，與針對「文字車牌 / 人像五官」自適應降噪超解析的數位照片修復工作站。

### ✨ 核心強項與獨家演算法
- **零拷貝極速解碼**：專為非標準監控影音流 (Raw Stream) 打造，跳脫傳統辦案轉檔曠日費時的痛點。
- **智慧空景快轉 (Time-Jump)**：無人畫面自動幻燈片躍遷；一旦偵測到目標，無縫切換回流暢實時追蹤。
- **台灣常見車種全覆蓋**：精準辨識人(Person)、單車(Bicycle)、一般汽車(Car)、機車(Moto)、公車客運(Bus)、大貨車(Truck)。
- **鑑識級抗干擾追蹤 (Anti-Jitter)**：搭載「絕對位移判定演算法」與「機車騎士智慧融合邊界框」，完美消除路面標線抖動誤報與人車分離雙重截圖的困擾。
- **全自動蒐證日誌 (Audit Log)**：每次執行全自動分析，系統將於背景自動寫入帶有精確時間戳記的 `系統鑑識紀錄.txt`，清楚還原所有目標出現軌跡與檔案錯誤歷程。

## 快速開始
**一鍵雙軌啟動 (推薦)**
直接雙擊專案目錄下的 `RUN.bat` 即可全自動啟動！
*若環境中缺乏 Python，系統將自動尋找隨身碟內的便攜核心進行「綠色盲開」。*

**手動啟動指令**
```bash
python main.py
```

`RUN.bat` 會使用專案自己的 `python_embed`。僅建置／檢查環境、不開啟介面可執行：

```bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_and_run.ps1 -NoLaunch
```

## 鑑識可追溯性

每支影片開始分析前，系統會將原始路徑、檔案大小、修改時間與 SHA-256 寫入本次鑑識紀錄。若無法完成雜湊，該影片不會進入分析，避免產生無法對應原始證物的截圖。

## 自動化測試

將測試影片放入被 Git 忽略的 `input_videos/`，再以可攜核心執行：

```bat
python_embed\python.exe -B tests\test_forensic_core.py -v
python_embed\python.exe -B tests\test_process_context.py -v
python_embed\python.exe -B tests\test_real_videos.py -v
```

## 技術棧
- **PyAV** (零拷貝全記憶體極速解碼，解決壞軌容錯)
- **Ultralytics YOLOv8 & ByteTrack** (強健的多目標辨識與軌跡追蹤)
- **OpenCV Contrib** (Bilateral Filter 降噪與 ESPCN 影像超解析引擎)
- **Eel** (輕量化 WebSocket 前後端通訊與現代化 UI 渲染)

後續功能規劃與實作紀錄請參閱 [`docs/FUTURE_ROADMAP.md`](docs/FUTURE_ROADMAP.md) 與 [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)。

## v3.1.0 安全與驗證補充

- 截圖採零覆寫命名，並將容量、SHA-256、時間碼與目標寫入 `鑑識截圖清冊.jsonl`。
- Base64 影像輸入設有型別、格式及容量限制；寫檔失敗時不會回報成功。
- 預設埠號被占用時會依序嘗試備援埠號。
- Python 3.13 為主要開發與驗證版本。
- AI 偵測結果僅供人工研判，不能取代原始證物、鑑識程序或承辦人判斷。
