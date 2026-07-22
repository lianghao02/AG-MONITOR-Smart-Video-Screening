# 🚀 本機系統快取清理與記憶體優化工具 (System Optimizer Tool)

![Python Version](https://img.shields.io/badge/Python-3.8%2B-blue.svg)
![UI Framework](https://img.shields.io/badge/UI-CustomTkinter-blueviolet.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

> **參考 BleachBit 與 Optimizer 純淨防護理念** 打造之強健型 Windows 系統清理與記憶體優化工具。
> **100% 絕不變更任何系統設定**，純粹專注於過期暫存檔案清理、網頁快取清理、閒置處理程序關閉與實體 RAM 記憶體釋放。

---

## ✨ 核心功能與特色

1. **🧹 暫存快取清理 (Temp & Cache Clean)**
   - 支援掃描使用者 Temp 暫存區與 Python `pip` 快取目錄。
   - 提供動態目錄掃描深度選項（僅首層目錄、2 層、3 層、無限制）。

2. **🌐 網頁暫存快取安全清理 (Browser Cache)**
   - 支援掃描 Chrome 與 Edge 的靜態網頁暫存快取 (`Cache` & `Code Cache`)。
   - 🔒 **隱私防護承諾**：僅清理網頁圖片與樣式檔，**絕不刪除網頁瀏覽紀錄 (History)**，亦不影響帳號登入狀態與密碼。

3. **⚡ 閒置處理程序清理 (Process Cleaner)**
   - 自動掃描背景高資源佔用且閒置之 Python / Node.js 處理程序。
   - 提供滑桿動態設定記憶體判定門檻（預設 500 MB）。

4. **🛡️ 絕對安全防護 (Zero Settings Changed)**
   - **絕不變更電腦設定**：不修改登錄檔、不關閉系統服務、不更動網路設定。
   - **模擬模式 (DRY_RUN)**：預設啟用，先掃描預覽檔案與處理程序清單，經使用者二次確認後才執行。
   - **保護白名單**：自動跳過包含 `.git`、`.antigravity`、`.py`、`.html` 等關鍵程式碼檔案與系統核心處理程序 (`explorer.exe` 等)。

5. **⚙️ 記憶體即時釋放與狀態監控 (RAM Monitor & GC)**
   - 調用 Windows 原生 API (`GlobalMemoryStatusEx`) 實時監控系統 RAM 負載與可用量（每 3 秒自動刷新）。
   - 透過 Python 垃圾回收 (`gc.collect`) 與處理程序清理，實質提升系統可用記憶體。

6. **🎨 現代化深色介面 (Dark UI)**
   - 基於 `CustomTkinter` 打造深色主題，提供實體進度條與清晰的彩色執行日誌 Console。

---

## 📦 技術棧與相依套件

- **程式語言**：Python 3.8+
- **GUI 框架**：[CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)
- **標準庫**：`os`, `sys`, `shutil`, `subprocess`, `gc`, `ctypes`, `datetime`, `threading`, `tkinter`

---

## 🚀 快速開始

### 1. 安裝相依套件
```bash
pip install customtkinter
```

### 2. 啟動應用程式
```bash
python main.py
```

---

## 📜 授權協議

本專案採用 MIT 授權條款。
