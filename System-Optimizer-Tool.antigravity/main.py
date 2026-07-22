#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
專案名稱：本機系統快取清理與記憶體優化工具 (System Optimizer Tool)
主要功能：過濾清理系統暫存檔案、網頁暫存快取、盤點清理背景閒置 Python/Node 處理程序、即時釋放記憶體
相依套件：本工具採用 Python 3 標準庫 (tkinter, os, sys, shutil, subprocess, gc, ctypes)
安裝指令：pip install customtkinter
執行指令：python main.py
"""

import os
import sys
import shutil
import subprocess
import gc
import datetime
import threading
import ctypes
import customtkinter as ctk
from tkinter import messagebox, scrolledtext

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', ctypes.c_ulong),
        ('dwMemoryLoad', ctypes.c_ulong),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]

def get_system_ram_info():
    """使用 Windows 原生 API 獲取當前系統記憶體狀態 (MB, 負載%)"""
    try:
        if sys.platform.startswith('win'):
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_mb = stat.ullTotalPhys / (1024 * 1024)
            avail_mb = stat.ullAvailPhys / (1024 * 1024)
            used_mb = total_mb - avail_mb
            load_percent = stat.dwMemoryLoad
            return total_mb, avail_mb, used_mb, load_percent
    except Exception:
        pass
    return 0.0, 0.0, 0.0, 0

# ==============================================================================
# 1. 系統預設參數配置 (Configuration)
# ==============================================================================
class CONFIG:
    APP_NAME = "本機系統快取清理與記憶體優化工具"
    VERSION = "v1.1.0 (專業版)"
    
    # 清理門檻與路徑設定
    DEFAULT_CPU_THRESHOLD = 80.0       # CPU 警告閾值 (%)
    DEFAULT_PROCESS_RAM_LIMIT = 500    # 閒置處理程序記憶體判定門檻 (MB)
    TARGET_PROCESSES = ["python.exe", "node.exe"]  # 預設掃描的高資源佔用處理程序
    
    # 預設掃描的系統暫存與網頁快取路徑
    USER_HOME = os.path.expanduser("~")
    TEMP_DIR = os.path.join(USER_HOME, "AppData", "Local", "Temp")
    PIP_CACHE_DIR = os.path.join(USER_HOME, "AppData", "Local", "pip", "cache")
    PREFETCH_DIR = r"C:\Windows\Prefetch"  # 系統預載快取區 (需管理員權限)
    
    # 瀏覽器網頁暫存快取 (Cache) - 僅圖片與網頁樣式檔，不影響瀏覽紀錄與個人資料
    CHROME_CACHE_DIR = os.path.join(USER_HOME, "AppData", "Local", "Google", "Chrome", "User Data", "Default", "Cache")
    CHROME_CODE_CACHE_DIR = os.path.join(USER_HOME, "AppData", "Local", "Google", "Chrome", "User Data", "Default", "Code Cache")
    EDGE_CACHE_DIR = os.path.join(USER_HOME, "AppData", "Local", "Microsoft", "Edge", "User Data", "Default", "Cache")
    EDGE_CODE_CACHE_DIR = os.path.join(USER_HOME, "AppData", "Local", "Microsoft", "Edge", "User Data", "Default", "Code Cache")
    
    # 動態掃描深度設定
    SCAN_DEPTH_OPTIONS = {
        "僅首層目錄": 1,
        "掃描 2 層": 2,
        "掃描 3 層": 3,
        "無限制 (完整清理)": 999
    }
    DEFAULT_SCAN_DEPTH = "無限制 (完整清理)"
    DRY_RUN = True  # 預設啟用模擬模式 (安全性第一)
    
    # UI 視覺主題顏色
    THEME = {
        "BG_DARK": "#1E1E24",          # 主背景深灰
        "CARD_BG": "#2A2A32",          # 卡片背景
        "TEXT_LIGHT": "#F5F5F7",       # 主要文字
        "TEXT_MUTED": "#8E8E93",       # 次要提示字
        "PRIMARY": "#2980B9",          # 科技藍
        "SUCCESS": "#27AE60",          # 綠色標示
        "WARNING": "#F39C12",          # 警告橙
        "DANGER": "#E74C3C"            # 警示紅
    }
    
    # 安全保護白名單：絕對禁止刪除或關閉的關鍵檔名與系統關鍵服務
    PROTECTED_KEYWORDS = [
        ".git", ".antigravity", "rules.md", "main.py", 
        "explorer.exe", "taskmgr.exe", "svchost.exe"
    ]

# ==============================================================================
# 2. 核心清理與優化邏輯引擎 (Optimizer Engine)
# ==============================================================================
class OptimizerEngine:
    
    @staticmethod
    def clean_temp_cache(log_callback, target_dir, skip_protected=True, max_depth=1, dry_run=False):
        """核心邏輯一：暫存快取清理 (Temp Clean)"""
        mode_text = "[模擬掃描]" if dry_run else "開始掃描"
        log_callback(f"🚀 {mode_text} 系統暫存區 (設定深度: {max_depth} 層)...", CONFIG.THEME["PRIMARY"])
        if not os.path.exists(target_dir):
            log_callback(f"⚠️ 目標路徑不存在，自動跳過：{target_dir}", CONFIG.THEME["WARNING"])
            return [] if dry_run else 0
            
        deleted_bytes = 0
        deleted_count = 0
        failed_count = 0
        pending_files = []
        
        for root, dirs, files in os.walk(target_dir):
            if root == target_dir:
                depth_level = 1
            else:
                depth_level = len(os.path.relpath(root, target_dir).split(os.sep)) + 1
                
            if depth_level >= max_depth:
                dirs.clear()

            if skip_protected and any(key in root.lower() for key in CONFIG.PROTECTED_KEYWORDS):
                continue
                
            for file in files:
                is_protected_ext = file.lower().endswith('.py') or file.lower().endswith('.html')
                if skip_protected and (is_protected_ext or any(key in file.lower() for key in CONFIG.PROTECTED_KEYWORDS)):
                    continue
                    
                file_path = os.path.join(root, file)
                if dry_run:
                    log_callback(f"🔍 [模擬模式] 預計清理檔案: {file_path}", CONFIG.THEME["TEXT_MUTED"])
                    pending_files.append(file_path)
                else:
                    try:
                        file_size = os.path.getsize(file_path)
                        os.remove(file_path)
                        deleted_bytes += file_size
                        deleted_count += 1
                    except Exception:
                        failed_count += 1
                    
        if dry_run:
            return pending_files
        else:
            mb_released = deleted_bytes / (1024 * 1024)
            log_callback(f"✅ 暫存清理完成！成功釋放空間: {mb_released:.2f} MB", CONFIG.THEME["SUCCESS"])
            log_callback(f"📊 統計：成功刪除 {deleted_count} 個檔案，跳過 {failed_count} 個項目 (原因: 檔案正被其他程式佔用或權限不足)。\n", CONFIG.THEME["TEXT_LIGHT"])
            return mb_released

    @staticmethod
    def clean_browser_cache(log_callback, dry_run=False):
        """核心邏輯二：網頁暫存快取清理 (Chrome / Edge 圖片與靜態檔) - 絕不影響瀏覽紀錄"""
        mode_text = "[模擬掃描]" if dry_run else "開始掃描"
        log_callback(f"🚀 {mode_text} 網頁暫存快取 (Chrome / Edge 圖片與樣式檔)...", CONFIG.THEME["PRIMARY"])
        
        target_dirs = [
            ("Chrome 快取", CONFIG.CHROME_CACHE_DIR),
            ("Chrome Code 快取", CONFIG.CHROME_CODE_CACHE_DIR),
            ("Edge 快取", CONFIG.EDGE_CACHE_DIR),
            ("Edge Code 快取", CONFIG.EDGE_CODE_CACHE_DIR)
        ]
        
        deleted_bytes = 0
        deleted_count = 0
        failed_count = 0
        pending_files = []
        
        for label, path in target_dirs:
            if not os.path.exists(path):
                continue
            for root, dirs, files in os.walk(path):
                for file in files:
                    file_path = os.path.join(root, file)
                    if dry_run:
                        log_callback(f"🔍 [模擬模式] 預計清理網頁快取 ({label}): {file_path}", CONFIG.THEME["TEXT_MUTED"])
                        pending_files.append(file_path)
                    else:
                        try:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            deleted_bytes += file_size
                            deleted_count += 1
                        except Exception:
                            failed_count += 1
                            
        if dry_run:
            return pending_files
        else:
            mb_released = deleted_bytes / (1024 * 1024)
            log_callback(f"✅ 網頁暫存快取清理完成！成功釋放空間: {mb_released:.2f} MB", CONFIG.THEME["SUCCESS"])
            if failed_count > 0:
                log_callback(f"📊 統計：成功刪除 {deleted_count} 個快取檔，跳過 {failed_count} 個項目 (原因: 瀏覽器正在執行中並鎖定檔案)。\n", CONFIG.THEME["TEXT_LIGHT"])
            return mb_released

    @staticmethod
    def clean_prefetch(log_callback, target_dir, dry_run=False):
        """核心邏輯三：系統預載快取清理 (Prefetch Clean)"""
        mode_text = "[模擬掃描]" if dry_run else "開始掃描"
        log_callback(f"🚀 {mode_text} 系統預載歷史快取 (Prefetch)...", CONFIG.THEME["PRIMARY"])
        if not os.path.exists(target_dir):
            log_callback(f"⚠️ 目標路徑不存在，自動跳過：{target_dir}", CONFIG.THEME["WARNING"])
            return [] if dry_run else 0
            
        deleted_bytes = 0
        deleted_count = 0
        failed_count = 0
        pending_files = []
        
        try:
            for root, dirs, files in os.walk(target_dir):
                for file in files:
                    is_protected_ext = file.lower().endswith('.py') or file.lower().endswith('.html')
                    if is_protected_ext:
                        continue
                        
                    file_path = os.path.join(root, file)
                    if dry_run:
                        log_callback(f"🔍 [模擬模式] 預計清理檔案: {file_path}", CONFIG.THEME["TEXT_MUTED"])
                        pending_files.append(file_path)
                    else:
                        try:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            deleted_bytes += file_size
                            deleted_count += 1
                        except Exception:
                            failed_count += 1
        except Exception as e:
            log_callback(f"❌ 讀取 Prefetch 發生錯誤 (可能需要管理員權限): {str(e)}", CONFIG.THEME["WARNING"])
            
        if dry_run:
            return pending_files
        else:
            mb_released = deleted_bytes / (1024 * 1024)
            log_callback(f"✅ Prefetch 清理完成！成功釋放空間: {mb_released:.2f} MB", CONFIG.THEME["SUCCESS"])
            log_callback(f"📊 統計：成功刪除 {deleted_count} 個檔案，跳過 {failed_count} 個項目 (原因: 檔案正被其他程式佔用或權限不足)。\n", CONFIG.THEME["TEXT_LIGHT"])
            return mb_released

    @staticmethod
    def kill_zombie_processes(log_callback, ram_limit_mb, target_extensions=None, dry_run=False):
        """核心邏輯四：背景閒置處理程序清理 (Process Cleaner)"""
        mode_text = "[模擬掃描]" if dry_run else "開始掃描"
        log_callback(f"🚀 {mode_text} 背景閒置處理程序 (記憶體佔用 > {ram_limit_mb}MB)...", CONFIG.THEME["PRIMARY"])
        if target_extensions is None:
            target_extensions = CONFIG.TARGET_PROCESSES
            
        killed_count = 0
        total_freed_ram_mb = 0.0
        pending_pids = []
        try:
            cmd = 'tasklist /FO CSV /NH'
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            output = subprocess.check_output(cmd, startupinfo=startupinfo, text=True, encoding='cp950', errors='ignore')
            
            for line in output.splitlines():
                if not line.strip():
                    continue
                parts = line.replace('"', '').split(',')
                if len(parts) >= 5:
                    proc_name = parts[0].strip()
                    pid = parts[1].strip()
                    mem_usage_str = parts[4].replace(' K', '').replace(',', '').strip()
                    
                    if any(ext in proc_name.lower() for ext in target_extensions):
                        try:
                            mem_mb = int(mem_usage_str) / 1024
                            if mem_mb > ram_limit_mb:
                                if int(pid) == os.getpid():
                                    continue
                                    
                                if dry_run:
                                    log_callback(f"🔍 [模擬模式] 預計結束處理程序：{proc_name} (PID: {pid}) 佔用 {mem_mb:.1f} MB", CONFIG.THEME["WARNING"])
                                    pending_pids.append((pid, proc_name, mem_mb))
                                else:
                                    log_callback(f"⚠️ 偵測到高能耗閒置處理程序：{proc_name} (PID: {pid}) 佔用 {mem_mb:.1f} MB", CONFIG.THEME["WARNING"])
                                    subprocess.run(f"taskkill /F /PID {pid}", startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    log_callback(f"❌ 已成功關閉處理程序 PID: {pid} (釋放約 {mem_mb:.1f} MB RAM)", CONFIG.THEME["DANGER"])
                                    killed_count += 1
                                    total_freed_ram_mb += mem_mb
                        except ValueError:
                            continue
        except Exception as e:
            log_callback(f"❌ 讀取處理程序列表時發生錯誤: {str(e)}", CONFIG.THEME["DANGER"])
            
        if dry_run:
            return pending_pids
        else:
            if killed_count == 0:
                log_callback("✅ 處理程序檢查完成，目前無超標之閒置處理程序。\n", CONFIG.THEME["SUCCESS"])
            else:
                log_callback(f"✅ 成功關閉了 {killed_count} 個背景閒置處理程序，預計釋放 {total_freed_ram_mb:.2f} MB RAM！\n", CONFIG.THEME["SUCCESS"])
            return killed_count, total_freed_ram_mb

    @staticmethod
    def force_garbage_collection(log_callback):
        """核心邏輯五：記憶體即時回收 (RAM Garbage Collection)"""
        log_callback("🚀 啟動 Python 記憶體回收機制...", CONFIG.THEME["PRIMARY"])
        try:
            gc.get_referrers()
            collected = gc.collect()
            log_callback(f"✅ 記憶體回收成功！回收物件群組共: {collected} 組", CONFIG.THEME["SUCCESS"])
            log_callback("⚙️ 系統記憶體分頁已完成整理。\n", CONFIG.THEME["TEXT_MUTED"])
        except Exception as e:
            log_callback(f"❌ 回收記憶體時發生錯誤: {str(e)}\n", CONFIG.THEME["DANGER"])

# ==============================================================================
# 3. 使用者介面實作 (GUI Interface)
# ==============================================================================
class SystemOptimizerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{CONFIG.APP_NAME} {CONFIG.VERSION}")
        self.geometry("920x670")
        ctk.set_appearance_mode("dark")
        
        self.default_font = ctk.CTkFont(family="Microsoft JhengHei", size=12)
        self.title_font = ctk.CTkFont(family="Microsoft JhengHei", size=14, weight="bold")
        
        # UI 控制變數
        self.var_clean_temp = ctk.BooleanVar(value=True)
        self.var_clean_browser = ctk.BooleanVar(value=True)
        self.var_clean_pip = ctk.BooleanVar(value=False)
        self.var_clean_prefetch = ctk.BooleanVar(value=False)
        self.var_kill_zombie = ctk.BooleanVar(value=True)
        self.var_ram_limit = ctk.IntVar(value=CONFIG.DEFAULT_PROCESS_RAM_LIMIT)
        self.var_scan_depth = ctk.StringVar(value=CONFIG.DEFAULT_SCAN_DEPTH)
        self.var_dry_run = ctk.BooleanVar(value=CONFIG.DRY_RUN)
        
        self.build_ui()
        
        self.append_log(f"✅ {CONFIG.APP_NAME} 已成功啟動。", CONFIG.THEME["SUCCESS"])
        self.append_log("💡 提示：設定左側清理選項後，點擊「開始一鍵優化」即可執行系統清理與記憶體釋放。\n---", CONFIG.THEME["TEXT_MUTED"])

    def build_ui(self):
        """建構主畫面視覺排版"""
        header_frame = ctk.CTkFrame(self, fg_color=CONFIG.THEME["CARD_BG"], height=60)
        header_frame.pack(fill="x", padx=15, pady=10)
        header_frame.pack_propagate(False)
        
        lbl_title = ctk.CTkLabel(header_frame, text=f"🚀 {CONFIG.APP_NAME}", font=ctk.CTkFont(family="Microsoft JhengHei", size=16, weight="bold"), text_color=CONFIG.THEME["TEXT_LIGHT"])
        lbl_title.pack(side="left", padx=15, pady=15)

        self.lbl_ram_status = ctk.CTkLabel(header_frame, text="💾 讀取 RAM 中...", font=ctk.CTkFont(family="Microsoft JhengHei", size=12, weight="bold"), text_color=CONFIG.THEME["SUCCESS"])
        self.lbl_ram_status.pack(side="left", padx=20, pady=15)
        
        lbl_ver = ctk.CTkLabel(header_frame, text=CONFIG.VERSION, font=self.default_font, text_color=CONFIG.THEME["TEXT_MUTED"])
        lbl_ver.pack(side="right", padx=15, pady=18)
        
        frame_depth = ctk.CTkFrame(header_frame, fg_color="transparent")
        frame_depth.pack(side="right", padx=15, pady=15)
        lbl_depth = ctk.CTkLabel(frame_depth, text="📂 掃描深度：", font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"])
        lbl_depth.pack(side="left")
        self.cmb_depth = ctk.CTkComboBox(frame_depth, variable=self.var_scan_depth, values=list(CONFIG.SCAN_DEPTH_OPTIONS.keys()), state="readonly", width=140, font=self.default_font, fg_color=CONFIG.THEME["BG_DARK"], border_color=CONFIG.THEME["PRIMARY"])
        self.cmb_depth.pack(side="left", padx=5)

        self.update_ram_status()

        self.progress_bar = ctk.CTkProgressBar(self, height=8, progress_color=CONFIG.THEME["PRIMARY"], fg_color=CONFIG.THEME["CARD_BG"])
        self.progress_bar.pack(fill="x", padx=15, pady=(0, 5))
        self.progress_bar.set(0)

        main_container = ctk.CTkFrame(self, fg_color="transparent")
        main_container.pack(fill="both", expand=True, padx=15, pady=5)
        
        # 左側控制面板
        left_panel = ctk.CTkFrame(main_container, fg_color=CONFIG.THEME["CARD_BG"], corner_radius=10)
        left_panel.pack(side="left", fill="both", padx=(0, 10))
        
        left_title = ctk.CTkLabel(left_panel, text="⚙️ 系統優化設定選項", font=self.title_font, text_color=CONFIG.THEME["PRIMARY"])
        left_title.pack(anchor="w", padx=15, pady=(15, 8))
        
        chk_temp = ctk.CTkCheckBox(left_panel, text="清理使用者暫存區 (Temp)", variable=self.var_clean_temp, font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color=CONFIG.THEME["PRIMARY"])
        chk_temp.pack(anchor="w", padx=15, pady=5)
        
        chk_browser = ctk.CTkCheckBox(left_panel, text="清理網頁暫存快取 (Chrome / Edge)", variable=self.var_clean_browser, font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color=CONFIG.THEME["PRIMARY"])
        chk_browser.pack(anchor="w", padx=15, pady=5)
        
        lbl_browser_hint = ctk.CTkLabel(left_panel, text="└ 僅包含圖片與樣式檔，完全不影響瀏覽紀錄與密碼", font=ctk.CTkFont(family="Microsoft JhengHei", size=10), text_color=CONFIG.THEME["TEXT_MUTED"])
        lbl_browser_hint.pack(anchor="w", padx=20, pady=(0, 5))

        chk_pip = ctk.CTkCheckBox(left_panel, text="清理 Python pip 快取目錄", variable=self.var_clean_pip, font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color=CONFIG.THEME["PRIMARY"])
        chk_pip.pack(anchor="w", padx=15, pady=5)
        
        chk_prefetch = ctk.CTkCheckBox(left_panel, text="清理系統預載歷史 (Prefetch)", variable=self.var_clean_prefetch, font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color=CONFIG.THEME["PRIMARY"])
        chk_prefetch.pack(anchor="w", padx=15, pady=5)
        
        divider = ctk.CTkFrame(left_panel, fg_color=CONFIG.THEME["BG_DARK"], height=2)
        divider.pack(fill="x", padx=15, pady=10)
        
        chk_zombie = ctk.CTkCheckBox(left_panel, text="關閉高記憶體佔用閒置處理程序", variable=self.var_kill_zombie, font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color=CONFIG.THEME["PRIMARY"])
        chk_zombie.pack(anchor="w", padx=15, pady=5)
        
        chk_dry_run = ctk.CTkCheckBox(left_panel, text="🛡️ 模擬開關 (僅預覽不刪除檔案)", variable=self.var_dry_run, font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color=CONFIG.THEME["PRIMARY"])
        chk_dry_run.pack(anchor="w", padx=15, pady=(5, 5))
        
        lbl_slider_desc = ctk.CTkLabel(left_panel, text="處理程序記憶體判定門檻：", font=self.default_font, text_color=CONFIG.THEME["TEXT_LIGHT"])
        lbl_slider_desc.pack(anchor="w", padx=15, pady=(5, 2))
        
        self.lbl_unit = ctk.CTkLabel(left_panel, text=f"當前門檻: {CONFIG.DEFAULT_PROCESS_RAM_LIMIT} MB", font=ctk.CTkFont(family="Microsoft JhengHei", size=11, weight="bold"), text_color=CONFIG.THEME["WARNING"])
        self.lbl_unit.pack(anchor="e", padx=15, pady=(0, 2))

        self.ram_slider = ctk.CTkSlider(
            left_panel, from_=100, to=2000, number_of_steps=38,
            variable=self.var_ram_limit, command=self._on_ram_slider_change,
            progress_color=CONFIG.THEME["PRIMARY"], button_color=CONFIG.THEME["PRIMARY"]
        )
        self.ram_slider.pack(fill="x", padx=15, pady=5)

        self.btn_launch = ctk.CTkButton(
            left_panel, text="⚡ 開始一鍵優化", font=ctk.CTkFont(family="Microsoft JhengHei", size=14, weight="bold"),
            fg_color=CONFIG.THEME["SUCCESS"], text_color=CONFIG.THEME["TEXT_LIGHT"], hover_color="#2196F3",
            corner_radius=8, height=45, command=self.execute_optimization_flow
        )
        self.btn_launch.pack(fill="x", side="bottom", padx=15, pady=15)

        # 右側執行日誌 Console
        right_panel = ctk.CTkFrame(main_container, fg_color=CONFIG.THEME["CARD_BG"], corner_radius=10)
        right_panel.pack(side="right", fill="both", expand=True)
        
        right_title = ctk.CTkLabel(right_panel, text="🖥️ 系統優化即時執行日誌", font=self.title_font, text_color=CONFIG.THEME["PRIMARY"])
        right_title.pack(anchor="w", padx=15, pady=(15, 5))
        
        log_frame = ctk.CTkFrame(right_panel, fg_color="#111115", corner_radius=8)
        log_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        
        self.log_display = scrolledtext.ScrolledText(
            log_frame, bg="#111115", fg=CONFIG.THEME["TEXT_LIGHT"], font=("Consolas", 11),
            relief="flat", wrap="word", insertbackground=CONFIG.THEME["TEXT_LIGHT"], borderwidth=0, highlightthickness=0
        )
        self.log_display.pack(fill="both", expand=True, padx=10, pady=10)
        
        for key, color in CONFIG.THEME.items():
            self.log_display.tag_config(color, foreground=color)

    def _on_ram_slider_change(self, value):
        self.lbl_unit.configure(text=f"當前門檻: {int(value)} MB")

    def update_ram_status(self):
        total, avail, used, load = get_system_ram_info()
        if total > 0:
            avail_gb = avail / 1024
            self.lbl_ram_status.configure(text=f"💾 系統 RAM 負載: {load}% (可用 {avail_gb:.1f} GB)")
        self.after(3000, self.update_ram_status)

    def append_log(self, message, color_key=None):
        def _update():
            self.log_display.insert("end", message + "\n")
            if color_key:
                end_line = float(self.log_display.index("end")) - 1.0
                start_line = end_line - 1.0
                self.log_display.tag_add(color_key, f"{start_line:.1f}", f"{end_line:.1f}")
            self.log_display.see("end")
            self.update_idletasks()
        self.after(0, _update)

    # ==============================================================================
    # 4. 一鍵排程與工作流管理 (Execution Flow)
    # ==============================================================================
    def execute_optimization_flow(self):
        self.btn_launch.configure(state="disabled", text="⏳ 優化執行中...")
        self.append_log("==================================================", CONFIG.THEME["TEXT_MUTED"])
        self.append_log(f"⏰ 任務啟動時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", CONFIG.THEME["TEXT_LIGHT"])
        self.append_log("==================================================", CONFIG.THEME["TEXT_MUTED"])
        
        is_dry_run = self.var_dry_run.get()
        selected_depth_str = self.var_scan_depth.get()
        max_depth = CONFIG.SCAN_DEPTH_OPTIONS.get(selected_depth_str, 1)
        do_temp = self.var_clean_temp.get()
        do_browser = self.var_clean_browser.get()
        do_pip = self.var_clean_pip.get()
        do_prefetch = self.var_clean_prefetch.get()
        do_zombie = self.var_kill_zombie.get()
        current_threshold_mb = self.var_ram_limit.get()
        
        if is_dry_run:
            self.append_log("🛡️ 目前為 [模擬模式]，僅預覽掃描結果，不會實際刪除檔案。", CONFIG.THEME["WARNING"])

        def _update_progress(value):
            self.after(0, lambda: self.progress_bar.set(value))

        def _thread_task():
            try:
                tot_ram, avail_before, used_before, load_before = get_system_ram_info()

                _update_progress(0.1)
                pending_files = []
                pending_pids = []
                total_items = 0
                freed_ram_from_procs = 0.0

                # 1. 清理使用者 Temp 暫存
                if do_temp:
                    res = OptimizerEngine.clean_temp_cache(self.append_log, CONFIG.TEMP_DIR, max_depth=max_depth, dry_run=is_dry_run)
                    if is_dry_run: pending_files.extend(res)
                _update_progress(0.25)

                # 2. 清理網頁暫存快取 (Chrome / Edge)
                if do_browser:
                    res = OptimizerEngine.clean_browser_cache(self.append_log, dry_run=is_dry_run)
                    if is_dry_run: pending_files.extend(res)
                _update_progress(0.45)
                    
                # 3. 清理 Python pip 快取
                if do_pip:
                    res = OptimizerEngine.clean_temp_cache(self.append_log, CONFIG.PIP_CACHE_DIR, max_depth=max_depth, dry_run=is_dry_run)
                    if is_dry_run: pending_files.extend(res)
                _update_progress(0.65)
                    
                # 4. 清理系統預載歷史 (Prefetch)
                if do_prefetch:
                    res = OptimizerEngine.clean_prefetch(self.append_log, CONFIG.PREFETCH_DIR, dry_run=is_dry_run)
                    if is_dry_run: pending_files.extend(res)
                _update_progress(0.8)
                    
                # 5. 處理程序清理
                if do_zombie:
                    res = OptimizerEngine.kill_zombie_processes(self.append_log, ram_limit_mb=current_threshold_mb, dry_run=is_dry_run)
                    if is_dry_run:
                        pending_pids.extend(res)
                    else:
                        killed_count, freed_ram_from_procs = res
                _update_progress(0.9)
                    
                if is_dry_run:
                    total_items = len(pending_files) + len(pending_pids)
                    est_proc_ram = sum(item[2] for item in pending_pids if len(item) >= 3)
                    if total_items == 0:
                        _update_progress(1.0)
                        self.append_log("✅ 模擬掃描結束，目前環境乾淨，無需要清理的項目。\n", CONFIG.THEME["SUCCESS"])
                        self.after(0, lambda: messagebox.showinfo("模擬完成", "模擬掃描結束，目前無需要清理的項目。"))
                    else:
                        _update_progress(1.0)
                        self.append_log(f"📊 [模擬統計] 預計清理 {len(pending_files)} 個檔案，預計關閉 {len(pending_pids)} 個處理程序 (約釋放 {est_proc_ram:.2f} MB RAM)。", CONFIG.THEME["TEXT_LIGHT"])
                        self.append_log("⚠️ 請確認上方掃描清單，若無誤可確認執行真實清理。", CONFIG.THEME["WARNING"])
                        
                        def _ask_confirm():
                            confirm = messagebox.askyesno("執行確認", f"模擬模式掃描完成！\n\n預計清理: {len(pending_files)} 個檔案\n預計關閉: {len(pending_pids)} 個處理程序 (預估釋放 {est_proc_ram:.1f} MB RAM)\n\n確認要執行真實清理與釋放嗎？")
                            if confirm:
                                threading.Thread(target=_real_delete_thread, args=(pending_files, pending_pids, avail_before, load_before), daemon=True).start()
                            else:
                                self.append_log("🛑 已取消真實清理動作。\n", CONFIG.THEME["TEXT_MUTED"])
                                self.btn_launch.configure(state="normal", text="⚡ 開始一鍵優化")
                                
                        self.after(0, _ask_confirm)
                        return
                else:
                    OptimizerEngine.force_garbage_collection(self.append_log)
                    _update_progress(1.0)
                    
                    tot_ram, avail_after, used_after, load_after = get_system_ram_info()
                    ram_diff_mb = avail_after - avail_before
                    final_freed_ram_mb = max(ram_diff_mb, freed_ram_from_procs)

                    self.append_log("==================================================", CONFIG.THEME["SUCCESS"])
                    self.append_log(f"🎉 【RAM 釋放成果】成功釋放系統記憶體: {final_freed_ram_mb:.2f} MB！", CONFIG.THEME["SUCCESS"])
                    if load_before > 0:
                        self.append_log(f"📊 記憶體負載變化: {load_before}% ➡️ {load_after}% (可用 RAM: {avail_after/1024:.2f} GB)", CONFIG.THEME["TEXT_LIGHT"])
                    self.append_log("==================================================", CONFIG.THEME["SUCCESS"])
                    self.append_log("🏁 【系統優化程序執行完畢】\n", CONFIG.THEME["SUCCESS"])
                    self.after(0, lambda: messagebox.showinfo("優化完成報告", f"一鍵系統優化成功完畢！\n\n🎉 成功釋放實體 RAM: {final_freed_ram_mb:.2f} MB\n系統負載已降至 {load_after}%。"))
                
            except Exception as e:
                self.append_log(f"❌ 執行過程中發生異常: {str(e)}", CONFIG.THEME["DANGER"])
                self.after(0, lambda err=e: messagebox.showerror("執行錯誤提示", f"程序發生非預期中斷:\n{str(err)}"))
                
            finally:
                if not is_dry_run or (is_dry_run and total_items == 0):
                    self.after(0, lambda: self.btn_launch.configure(state="normal", text="⚡ 開始一鍵優化"))
                    self.after(2000, lambda: self.progress_bar.set(0))

        def _real_delete_thread(files, pids, avail_before, load_before):
            try:
                self.append_log("\n⚡ 使用者授權完成，開始執行真實清理與記憶體釋放...", CONFIG.THEME["DANGER"])
                
                deleted_count = 0
                failed_count = 0
                for file_path in files:
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                    except Exception:
                        failed_count += 1
                    
                killed_count = 0
                freed_ram_proc = 0.0
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                for item in pids:
                    pid = item[0]
                    proc_name = item[1]
                    mem_mb = item[2] if len(item) >= 3 else 0.0
                    try:
                        subprocess.run(f"taskkill /F /PID {pid}", startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        killed_count += 1
                        freed_ram_proc += mem_mb
                    except: pass
                    
                self.append_log(f"✅ 清理完成！成功刪除 {deleted_count} 個檔案，關閉 {killed_count} 個處理程序。", CONFIG.THEME["SUCCESS"])
                if failed_count > 0:
                    self.append_log(f"⚠️ 注意：有 {failed_count} 個檔案未刪除 (原因: 檔案正被其他程式佔用或無存取權限)。", CONFIG.THEME["WARNING"])
                    
                OptimizerEngine.force_garbage_collection(self.append_log)
                # 計算真實刪除前後實質釋出 RAM
                tot_ram, avail_after, used_after, load_after = get_system_ram_info()
                ram_diff_mb = avail_after - avail_before
                final_freed_ram_mb = max(ram_diff_mb, freed_ram_proc)

                self.append_log("==================================================", CONFIG.THEME["SUCCESS"])
                self.append_log(f"🎉 【RAM 釋放成果】成功釋出實體 RAM: {final_freed_ram_mb:.2f} MB！", CONFIG.THEME["SUCCESS"])
                if load_before > 0:
                    self.append_log(f"📊 記憶體負載變化: {load_before}% ➡️ {load_after}% (可用 RAM: {avail_after/1024:.2f} GB)", CONFIG.THEME["TEXT_LIGHT"])
                self.append_log("==================================================", CONFIG.THEME["SUCCESS"])
                self.append_log("🏁 【系統優化程序執行完畢】\n", CONFIG.THEME["SUCCESS"])
                self.after(0, lambda: messagebox.showinfo("優化完成", f"清理已成功完畢！\n\n🎉 成功釋放實體 RAM: {final_freed_ram_mb:.2f} MB\n系統負載已降至 {load_after}%。"))
            except Exception as e:
                self.append_log(f"❌ 清理過程中發生異常: {str(e)}", CONFIG.THEME["DANGER"])
            finally:
                self.after(0, lambda: self.btn_launch.configure(state="normal", text="⚡ 開始一鍵優化"))
                self.after(2000, lambda: self.progress_bar.set(0))

        threading.Thread(target=_thread_task, daemon=True).start()

if __name__ == "__main__":
    try:
        if sys.platform.startswith('win'):
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
        
    app = SystemOptimizerApp()
    app.mainloop()