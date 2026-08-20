import os
os.environ["OPENCV_FFMPEG_LOG_LEVEL"] = "-1"
os.environ["PYAV_LOGGING"] = "off"
os.environ["YOLO_VERBOSE"] = "False"
os.environ["YOLO_OFFLINE"] = "True"
# 將 Ultralytics 執行設定留在專案的忽略目錄內，避免可攜模式
# 依賴或污染目前 Windows 使用者的 AppData 設定。
os.environ.setdefault(
    "YOLO_CONFIG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", ".ultralytics"),
)
os.makedirs(os.environ["YOLO_CONFIG_DIR"], exist_ok=True)
import cv2
import numpy as np
import time
import base64
import csv
import hashlib
import json
import math
import re
import shutil
import urllib.request
import zipfile
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import threading
from threading import Thread
from PIL import Image, ImageDraw, ImageFont
import eel
try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    # Python 官方 embeddable 套件不包含 Tcl/Tk；可攜模式改走
    # Windows Forms 對話框，避免程式在啟動階段直接中止。
    tk = None
    filedialog = None
import av
import traceback
import gc
from ultralytics import YOLO

# ===== DEBUG 診斷日誌 (寫入檔案，因為 eel 會攔截 stdout) =====
_DEBUG_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
def dlog(msg):
    try:
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}\n")
            _f.flush()
    except Exception:
        pass

# 啟動時清空舊的日誌
try:
    open(_DEBUG_LOG_PATH, "w", encoding="utf-8").close()
except Exception:
    pass
dlog("=== main.py 啟動 ===")

def safe_base64_decode(base64_str, max_bytes=50 * 1024 * 1024):
    """安全解析 Base64 數據，防禦過長字串 OOM、data-URI 前綴與格式毀損。"""
    if not isinstance(base64_str, str):
        return None, "傳入資料非字串格式"
    if "," in base64_str:
        base64_str = base64_str.split(",", 1)[1]
    base64_str = base64_str.strip()
    if len(base64_str) > max_bytes:
        return None, f"Base64 資料超出安全容量限制 ({len(base64_str)} > {max_bytes} bytes)"
    missing_padding = len(base64_str) % 4
    if missing_padding:
        base64_str += "=" * (4 - missing_padding)
    try:
        return base64.b64decode(base64_str), None
    except Exception as err:
        return None, f"Base64 解碼失敗: {err}"

class CONFIG:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CAPTURES_DIR = os.path.join(BASE_DIR, "captures")
    APP_TITLE = "AG-MONITOR 科技偵查戰術播放器"
    
    SMART_SKIP_SEC = 3.0   
    MOTION_THRESH = 25     
    MOTION_MIN_AREA = 500  
    HASH_CHUNK_SIZE = 4 * 1024 * 1024
    DECODER_SLOW_WARN_SEC = 10.0
    DECODER_DEADLOCK_SEC = 30.0
    DECODER_MAX_CONSECUTIVE_ERRORS = 100
    BYTETRACK_OCCLUSION_GRACE_SEC = 1.5
    BOTSORT_REID_OCCLUSION_GRACE_SEC = 6.0
    TRACK_REIDENTIFY_GRACE_SEC = 3.0
    MOTION_CONFIRMATION_FRAMES = 2
    TRACKER_CONFIGS = {
        "bytetrack": os.path.join(BASE_DIR, "trackers", "ag_bytetrack.yaml"),
        "botsort_reid": os.path.join(BASE_DIR, "trackers", "ag_botsort_reid.yaml"),
    }
    TRACKER_LABELS = {
        "bytetrack": "ByteTrack（穩定基準）",
        "botsort_reid": "BoT-SORT ReID（實驗性）",
    }

    TARGET_CLASSES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# Global State
video_queue = []
roi_points = []
scale_info = None 
is_processing = False
stop_requested = False
skip_video_path = None
model = None
current_model_name = None
list_lock = threading.Lock()
global_live_settings = {}
current_report_path = None

def write_report(msg):
    global current_report_path
    if not current_report_path: return
    try:
        with open(current_report_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def calculate_sha256(file_path, chunk_size=CONFIG.HASH_CHUNK_SIZE):
    """以串流方式計算證物雜湊，避免大型監視器影片占滿記憶體。"""
    digest = hashlib.sha256()
    with open(file_path, "rb") as file_obj:
        while chunk := file_obj.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()


def build_evidence_metadata(file_path):
    """建立可寫入鑑識紀錄的原始證物識別資料。"""
    absolute_path = os.path.abspath(file_path)
    stat = os.stat(absolute_path)
    return {
        "path": absolute_path,
        "name": os.path.basename(absolute_path),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "sha256": calculate_sha256(absolute_path),
    }


def format_enabled_classes(class_settings):
    enabled = [
        CONFIG.TARGET_CLASSES[class_id]
        for class_id in CONFIG.TARGET_CLASSES
        if class_settings.get(str(class_id), True)
    ]
    return ", ".join(enabled) if enabled else "無"


def resolve_tracker_config(settings):
    """只允許使用專案內固定追蹤設定，拒絕任意路徑注入。"""
    tracker_mode = settings.get("trackerMode", "bytetrack")
    if tracker_mode not in CONFIG.TRACKER_CONFIGS:
        raise ValueError(f"不支援的追蹤核心: {tracker_mode}")
    tracker_path = CONFIG.TRACKER_CONFIGS[tracker_mode]
    if not os.path.isfile(tracker_path):
        raise FileNotFoundError(f"追蹤設定不存在: {tracker_path}")
    return tracker_mode, tracker_path


def tracker_occlusion_grace_seconds(tracker_mode):
    """ReID 必須維持足夠長的連續追蹤，才能跨越三秒以上遮蔽。"""
    if tracker_mode == "botsort_reid":
        return CONFIG.BOTSORT_REID_OCCLUSION_GRACE_SEC
    return CONFIG.BYTETRACK_OCCLUSION_GRACE_SEC


def resolve_live_processing_settings(live_settings, current_settings):
    """合併分析中的即時設定；未變更欄位沿用目前值。"""
    resolved = current_settings.copy()
    for key in (
        "confThresh", "fastMode", "skipSec", "classes", "captureMode",
        "filterStationary", "inferenceSize", "riderAssist",
    ):
        if key in live_settings:
            resolved[key] = live_settings[key]
    resolved["skipSec"] = float(resolved["skipSec"])
    return resolved


def record_motion_observation(state, centroid, box_size):
    """以目標尺寸自適應的門檻確認移動，排除停放車輛的邊界框抖動。"""
    if state["is_moving"]:
        return True

    start_cx, start_cy = state["start_centroid"]
    start_w, start_h = state["start_box_size"]
    width, height = box_size
    displacement = math.hypot(centroid[0] - start_cx, centroid[1] - start_cy)
    reference_size = max(1, start_w, start_h)
    movement_threshold = max(6.0, min(32.0, reference_size * 0.08))
    size_threshold = max(6.0, min(32.0, reference_size * 0.10))
    size_change = max(abs(width - start_w), abs(height - start_h))

    if displacement >= movement_threshold or size_change >= size_threshold:
        state["motion_confirmations"] += 1
    else:
        state["motion_confirmations"] = 0

    if state["motion_confirmations"] >= CONFIG.MOTION_CONFIRMATION_FRAMES:
        state["is_moving"] = True
    return state["is_moving"]


def should_trigger_decoder_deadlock(phase, elapsed, timeout=CONFIG.DECODER_DEADLOCK_SEC):
    """只有解碼器本身長時間無回應才視為死鎖；等待佇列消費不算。"""
    return phase == "decoding" and elapsed >= timeout


# Player State
engine_mode = 'auto' # 'auto' or 'manual'
player_state = {
    'playing': False,
    'reverse': False,
    'speed': 1.0,
    'seek_req': None, # 0.0 ~ 100.0 percent
    'step_req': 0,    # frames to step
    'manual_capture_req': False,
    'current_frame': None,
    'current_timecode': "",
    'annotated_frame': None
}
player_lock = threading.Lock()
real_roi_poly = None


def _run_windows_dialog(script):
    """在沒有 tkinter 的可攜環境中呼叫 Windows 原生選檔對話框。"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=0x08000000,
        check=False,
    )
    if completed.returncode != 0:
        dlog(f"[DIALOG] Windows Forms 對話框失敗: {completed.stderr.strip()}")
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _select_video_files():
    if filedialog is not None:
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        try:
            return list(filedialog.askopenfilenames(
                title="選擇視訊檔案",
                filetypes=[("視訊檔案", "*.mp4 *.avi *.mkv *.mov *.m4v *.h264 *.h265 *.264 *.265 *.dav *.flv *.ts *.wmv")],
            ))
        finally:
            root.destroy()

    script = r'''
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '選擇視訊檔案'
$dialog.Multiselect = $true
$dialog.Filter = '視訊檔案|*.mp4;*.avi;*.mkv;*.mov;*.m4v;*.h264;*.h265;*.264;*.265;*.dav;*.flv;*.ts;*.wmv|所有檔案|*.*'
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $dialog.FileNames | ForEach-Object { [Console]::Out.WriteLine($_) }
}
'''
    return _run_windows_dialog(script)


def _select_video_folder():
    if filedialog is not None:
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        try:
            return filedialog.askdirectory(title="選擇包含影片的資料夾 (將自動掃描所有子資料夾)")
        finally:
            root.destroy()

    script = r'''
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '選擇包含影片的資料夾（將自動掃描所有子資料夾）'
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.WriteLine($dialog.SelectedPath)
}
'''
    paths = _run_windows_dialog(script)
    return paths[0] if paths else ""

@eel.expose
def add_videos_dialog():
    global video_queue
    files = _select_video_files()
    
    added_paths = []
    if files:
        with list_lock:
            for f in files:
                if f not in video_queue:
                    video_queue.append(f)
                    added_paths.append(f)
            if added_paths and len(video_queue) == len(added_paths):
                # load preview for the first
                Thread(target=load_preview_frame, args=(video_queue[0],), daemon=True).start()
    return added_paths

@eel.expose
def add_folder_dialog():
    global video_queue
    folder_path = _select_video_folder()
    
    if not folder_path:
        return []

    valid_extensions = {".mp4", ".avi", ".mkv", ".mov", ".m4v", ".h264", ".h265", ".264", ".265", ".dav", ".flv", ".ts", ".wmv",
                        ".mp4]", ".avi]", ".mkv]", ".mov]", ".m4v]", ".h264]", ".h265]", ".264]", ".265]", ".dav]", ".flv]", ".ts]", ".wmv]"}
    added_paths = []
    
    with list_lock:
        for root_dir, dirs, files in os.walk(folder_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in valid_extensions:
                    full_path = os.path.join(root_dir, file)
                    full_path = full_path.replace("\\", "/")
                    if full_path not in video_queue:
                        video_queue.append(full_path)
                        added_paths.append(full_path)
                        
        if added_paths and len(video_queue) == len(added_paths):
            Thread(target=load_preview_frame, args=(video_queue[0],), daemon=True).start()
            
    return added_paths

@eel.expose
def add_dropped_paths(paths):
    """處理從桌面或檔案總管拖曳進入的檔案或資料夾路徑。"""
    global video_queue
    if not paths or not isinstance(paths, list):
        return []
        
    valid_extensions = {
        ".mp4", ".avi", ".mkv", ".mov", ".m4v", ".h264", ".h265", ".264", ".265", ".dav", ".flv", ".ts", ".wmv",
        ".mp4]", ".avi]", ".mkv]", ".mov]", ".m4v]", ".h264]", ".h265]", ".264]", ".265]", ".dav]", ".flv]", ".ts]", ".wmv]"
    }
    
    added_paths = []
    with list_lock:
        for raw_path in paths:
            if not isinstance(raw_path, str):
                continue
            normalized_path = os.path.normpath(raw_path).replace("\\", "/")
            if os.path.isfile(raw_path):
                ext = os.path.splitext(raw_path)[1].lower()
                if ext in valid_extensions and normalized_path not in video_queue:
                    video_queue.append(normalized_path)
                    added_paths.append(normalized_path)
            elif os.path.isdir(raw_path):
                for root_dir, _dirs, files in os.walk(raw_path):
                    for file in files:
                        ext = os.path.splitext(file)[1].lower()
                        if ext in valid_extensions:
                            full_path = os.path.normpath(os.path.join(root_dir, file)).replace("\\", "/")
                            if full_path not in video_queue:
                                video_queue.append(full_path)
                                added_paths.append(full_path)
                                
        if added_paths and len(video_queue) == len(added_paths):
            Thread(target=load_preview_frame, args=(video_queue[0],), daemon=True).start()
            
    return added_paths

@eel.expose
def resolve_and_add_dropped_files(file_metadata_list):
    """
    當前端受限於瀏覽器安全沙盒無法獲取 fullpath 時，
    透過前端提供的檔名 (name) 與大小 (size) 在專案目錄、常用監視器目錄中極速反查真實絕對路徑。
    """
    global video_queue
    if not file_metadata_list or not isinstance(file_metadata_list, list):
        return {"success": False, "added_paths": [], "unresolved_names": []}

    valid_extensions = {
        ".mp4", ".avi", ".mkv", ".mov", ".m4v", ".h264", ".h265", ".264", ".265", ".dav", ".flv", ".ts", ".wmv",
        ".mp4]", ".avi]", ".mkv]", ".mov]", ".m4v]", ".h264]", ".h265]", ".264]", ".265]", ".dav]", ".flv]", ".ts]", ".wmv]"
    }

    # 優先搜尋候選目錄（專案目錄及其子目錄、桌面、下載、常見目錄）
    search_dirs = [
        os.path.join(CONFIG.BASE_DIR, "input_videos"),
        CONFIG.BASE_DIR,
        os.path.join(CONFIG.BASE_DIR, "videos"),
        os.path.join(CONFIG.BASE_DIR, "test_videos"),
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Downloads"),
    ]
    
    # 建立目前可搜尋目錄下的快速檔案索引 (檔名 -> list of full_paths)
    candidate_map = {}
    for s_dir in search_dirs:
        if os.path.exists(s_dir) and os.path.isdir(s_dir):
            for root, _dirs, files in os.walk(s_dir):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in valid_extensions:
                        full_p = os.path.normpath(os.path.join(root, f)).replace("\\", "/")
                        candidate_map.setdefault(f.lower(), []).append(full_p)

    added_paths = []
    unresolved_names = []

    with list_lock:
        for item in file_metadata_list:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            size = item.get("size", 0)
            if not name:
                continue

            name_lower = name.lower()
            matched_path = None

            if name_lower in candidate_map:
                candidates = candidate_map[name_lower]
                if len(candidates) == 1:
                    matched_path = candidates[0]
                else:
                    for c_path in candidates:
                        try:
                            if os.path.getsize(c_path) == size:
                                matched_path = c_path
                                break
                        except OSError:
                            pass
                    if not matched_path:
                        matched_path = candidates[0]

            if matched_path:
                if matched_path not in video_queue:
                    video_queue.append(matched_path)
                    added_paths.append(matched_path)
            else:
                unresolved_names.append(name)

        if added_paths and len(video_queue) == len(added_paths):
            Thread(target=load_preview_frame, args=(video_queue[0],), daemon=True).start()

    return {
        "success": len(added_paths) > 0,
        "added_paths": added_paths,
        "unresolved_names": unresolved_names
    }

@eel.expose
def clear_roi():
    global roi_points
    roi_points = []
    return True

@eel.expose
def play_specific_video(path):
    global skip_video_path
    skip_video_path = path

@eel.expose
def clear_queue():
    global real_roi_poly, video_queue
    with list_lock:
        video_queue.clear()
        real_roi_poly = None

@eel.expose
def open_capture_folder():
    try:
        folder_path = os.path.abspath(CONFIG.CAPTURES_DIR)
        os.makedirs(folder_path, exist_ok=True)
        os.startfile(folder_path)
        eel.appendLog("已開啟截圖資料夾", "info")
    except Exception as e:
        eel.appendLog(f"開啟資料夾失敗: {str(e)}", "error")


def _build_rename_target(path, keep_old_name):
    dir_name = os.path.dirname(path)
    base_name = os.path.basename(path)
    if base_name.endswith("]"):
        name_no_ext, broken_ext = os.path.splitext(base_name)
        if broken_ext.startswith(".") and broken_ext.endswith("]"):
            return os.path.join(dir_name, name_no_ext + "]" + broken_ext[:-1])
    start_time = parse_start_time(base_name)
    if not start_time:
        return path
    time_str = start_time.strftime("%Y%m%d_%H%M%S")
    if keep_old_name and base_name.startswith(f"{time_str}_["):
        return path
    if not keep_old_name and base_name.startswith(time_str) and "_[" not in base_name:
        return path
    name_no_ext, ext = os.path.splitext(base_name)
    if keep_old_name:
        if re.match(r'^20\d{6}_\d{6}_\[', name_no_ext) and name_no_ext.endswith("]"):
            name_no_ext = name_no_ext[17:-1]
        new_name = f"{time_str}_[{name_no_ext}]{ext}"
    else:
        new_name = f"{time_str}{ext}"
    return os.path.join(dir_name, new_name)


def _reserve_unique_rename_path(candidate, source, reserved_paths):
    source_key = os.path.normcase(os.path.abspath(source))
    stem, ext = os.path.splitext(candidate)
    counter = 0
    while True:
        current = candidate if counter == 0 else f"{stem}_{counter}{ext}"
        current_key = os.path.normcase(os.path.abspath(current))
        exists_elsewhere = os.path.exists(current) and current_key != source_key
        if not exists_elsewhere and current_key not in reserved_paths:
            reserved_paths.add(current_key)
            return current
        counter += 1


def _write_rename_manifest(manifest, manifest_dir):
    os.makedirs(manifest_dir, exist_ok=True)
    if "manifest_path" not in manifest:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        manifest["manifest_path"] = os.path.join(manifest_dir, f"rename_{stamp}.json")
        manifest["csv_path"] = os.path.join(manifest_dir, f"rename_{stamp}.csv")
    json_temp = manifest["manifest_path"] + ".tmp"
    with open(json_temp, "w", encoding="utf-8", newline="\n") as file_obj:
        json.dump(manifest, file_obj, ensure_ascii=False, indent=2)
    os.replace(json_temp, manifest["manifest_path"])
    csv_temp = manifest["csv_path"] + ".tmp"
    fields = ("status", "original_path", "new_path", "size", "sha256_before", "sha256_after", "error")
    with open(csv_temp, "w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest["items"])
    os.replace(csv_temp, manifest["csv_path"])


def plan_video_renames(paths, keep_old_name=True):
    reserved_paths = set()
    items = []
    for raw_path in paths:
        source = os.path.abspath(raw_path)
        if not os.path.isfile(source):
            raise FileNotFoundError(f"找不到待重新命名檔案: {source}")
        candidate = _build_rename_target(source, keep_old_name)
        if os.path.normcase(candidate) == os.path.normcase(source):
            reserved_paths.add(os.path.normcase(source))
            items.append({"status": "skipped", "original_path": source, "new_path": source,
                          "size": os.path.getsize(source), "sha256_before": "", "sha256_after": "",
                          "error": "檔名已符合格式或無法解析時間"})
            continue
        destination = _reserve_unique_rename_path(candidate, source, reserved_paths)
        metadata = build_evidence_metadata(source)
        items.append({"status": "planned", "original_path": source, "new_path": destination,
                      "size": metadata["size"], "sha256_before": metadata["sha256"],
                      "sha256_after": "", "error": ""})
    return items


def execute_rename_transaction(paths, keep_old_name=True, manifest_dir=None, rename_func=None, progress_callback=None):
    rename_func = rename_func or os.rename
    manifest_dir = manifest_dir or os.path.join(CONFIG.CAPTURES_DIR, "rename_manifests")
    manifest = {"version": 1, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "keep_old_name": bool(keep_old_name), "status": "planning", "items": []}
    try:
        manifest["items"] = plan_video_renames(paths, keep_old_name)
        manifest["status"] = "planned"
        _write_rename_manifest(manifest, manifest_dir)
    except Exception as error:
        manifest["status"], manifest["error"] = "planning_failed", str(error)
        _write_rename_manifest(manifest, manifest_dir)
        return {"success": False, "count": 0, "new_paths": list(paths), "msg": str(error),
                "manifest_path": manifest["manifest_path"], "csv_path": manifest["csv_path"]}

    completed = []
    try:
        actionable = [item for item in manifest["items"] if item["status"] == "planned"]
        for index, item in enumerate(actionable, 1):
            rename_func(item["original_path"], item["new_path"])
            completed.append(item)
            item["sha256_after"] = calculate_sha256(item["new_path"])
            if item["sha256_after"] != item["sha256_before"]:
                raise RuntimeError(f"重新命名後 SHA-256 不一致: {item['new_path']}")
            item["status"] = "renamed"
            _write_rename_manifest(manifest, manifest_dir)
            if progress_callback:
                progress_callback(index, len(actionable))
    except Exception as error:
        rollback_errors = []
        for item in reversed(completed):
            try:
                if os.path.exists(item["original_path"]):
                    raise FileExistsError(f"回復目的已存在: {item['original_path']}")
                rename_func(item["new_path"], item["original_path"])
                if calculate_sha256(item["original_path"]) != item["sha256_before"]:
                    raise RuntimeError("回復後 SHA-256 不一致")
                item["status"] = "rolled_back"
            except Exception as rollback_error:
                item["status"], item["error"] = "rollback_failed", str(rollback_error)
                rollback_errors.append(str(rollback_error))
        manifest["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        manifest["error"] = str(error)
        _write_rename_manifest(manifest, manifest_dir)
        message = f"重新命名失敗，已回復: {error}"
        if rollback_errors:
            message = f"重新命名與回復均失敗，請依對照表人工處理: {'; '.join(rollback_errors)}"
        return {"success": False, "count": 0, "new_paths": list(paths), "msg": message,
                "manifest_path": manifest["manifest_path"], "csv_path": manifest["csv_path"]}

    manifest["status"] = "completed"
    _write_rename_manifest(manifest, manifest_dir)
    path_map = {item["original_path"]: item["new_path"] for item in manifest["items"]}
    new_paths = [path_map.get(os.path.abspath(path), os.path.abspath(path)) for path in paths]
    return {"success": True, "count": len(completed), "new_paths": new_paths,
            "manifest_path": manifest["manifest_path"], "csv_path": manifest["csv_path"]}


@eel.expose
def batch_rename_videos(keep_old_name=True):
    global video_queue
    with list_lock:
        if is_processing:
            return {"success": False, "msg": "分析中無法重新命名"}
        total = len(video_queue)
        result = execute_rename_transaction(
            list(video_queue),
            keep_old_name=keep_old_name,
            progress_callback=lambda current, action_total: eel.updateRenameProgress(current, action_total)(),
        )
        eel.updateRenameProgress(total, total)()
        if result["success"]:
            video_queue = result["new_paths"]
        return result

@eel.expose
def set_roi_points(pts):
    global roi_points, real_roi_poly
    roi_points = pts
    real_roi_poly = get_real_roi_polygon()

@eel.expose
def request_stop():
    global stop_requested
    stop_requested = True
    eel.updateStatus("狀態: 正在要求安全終止...", "warn")

@eel.expose
def update_live_setting(key, value):
    global global_live_settings
    old_value = global_live_settings.get(key)
    global_live_settings[key] = value
    if is_processing and old_value != value:
        if key == "classes":
            display_value = format_enabled_classes(value)
        else:
            display_value = value
        write_report(f"⚙️ 執行中設定變更: {key} = {display_value}")

@eel.expose
def set_engine_mode(mode):
    global engine_mode, stop_requested
    if is_processing:
        return
    engine_mode = mode
    eel.appendLog(f"已切換至: {'全自動 AI 蒐證' if mode == 'auto' else '即時人眼點視'}", "info")

# --- Player API ---
@eel.expose
def play_pause():
    with player_lock:
        player_state['playing'] = not player_state['playing']
        eel.updatePlayState(player_state['playing'], player_state['reverse'])

@eel.expose
def toggle_reverse():
    with player_lock:
        player_state['reverse'] = not player_state['reverse']
        eel.updatePlayState(player_state['playing'], player_state['reverse'])

@eel.expose
def set_speed(s):
    with player_lock:
        player_state['speed'] = float(s)

@eel.expose
def seek_frame(percent):
    with player_lock:
        player_state['seek_req'] = float(percent)

@eel.expose
def step_frame(steps):
    with player_lock:
        player_state['step_req'] = int(steps)
        player_state['playing'] = False
        eel.updatePlayState(player_state['playing'], player_state['reverse'])

@eel.expose
def manual_capture():
    with player_lock:
        player_state['manual_capture_req'] = True

@eel.expose
def start_processing(settings):
    global is_processing, stop_requested, global_live_settings, skip_video_path
    if not video_queue:
        eel.updateStatus("狀態: 清單為空，無法開始", "danger")
        eel.processingFinished()
        return
    is_processing = True
    stop_requested = False
    skip_video_path = None
    global_live_settings = settings.copy()
    
    with player_lock:
        player_state['playing'] = True if engine_mode == 'manual' else False
        player_state['reverse'] = False
        player_state['speed'] = 1.0
        player_state['seek_req'] = None
        player_state['step_req'] = 0
        player_state['manual_capture_req'] = False
    
    if engine_mode == 'manual':
        eel.updatePlayState(player_state['playing'], player_state['reverse'])
        
    Thread(target=batch_processing_worker, args=(settings,), daemon=True).start()

def load_preview_frame(video_path):
    global scale_info
    fh = None
    container = None
    try:
        ext = os.path.splitext(video_path)[1].lower()
        fmt = None
        if ext in ['.265', '.h265']:
            fmt = 'hevc'
        elif ext in ['.264', '.h264', '.dav']:
            fmt = 'h264'
        elif ext in ['.ts']:
            fmt = 'mpegts'
        
        fh = open(video_path, 'rb')
        try:
            container = av.open(fh, format=fmt, metadata_errors='ignore')
        except Exception:
            # If PyAV throws UnicodeDecodeError or any other error, fallback to probing
            container = av.open(video_path, format=fmt, metadata_errors='ignore')
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            img = frame.to_ndarray(format='bgr24')
            
            canvas_w = 800
            canvas_h = 600
            
            frame_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img_h, img_w, _ = frame_rgb.shape
            scale = min(canvas_w / img_w, canvas_h / img_h)
            new_w, new_h = int(img_w * scale), int(img_h * scale)
            
            pad_x = (canvas_w - new_w) // 2
            pad_y = (canvas_h - new_h) // 2
            scale_info = (scale, pad_x, pad_y, img_w, img_h)
            
            img_resized = cv2.resize(frame_rgb, (new_w, new_h))
            
            canvas_img = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
            canvas_img[pad_y:pad_y+new_h, pad_x:pad_x+new_w] = img_resized
            
            _, buffer = cv2.imencode('.jpg', cv2.cvtColor(canvas_img, cv2.COLOR_RGB2BGR))
            b64_str = base64.b64encode(buffer).decode('utf-8')
            
            info_obj = {
                "scale": scale, "pad_x": pad_x, "pad_y": pad_y, 
                "canvas_w": canvas_w, "canvas_h": canvas_h
            }
            eel.setPreviewImage(b64_str, info_obj)()
            break
        container.close()
    except Exception as e:
        print(f"Failed to load preview: {e}")
    finally:
        if container:
            try:
                container.close()
            except Exception:
                pass
        if fh:
            fh.close()

def get_real_roi_polygon():
    if not scale_info or not roi_points:
        return None
    scale, pad_x, pad_y, img_w, img_h = scale_info
    real_pts = []
    for pt in roi_points:
        rx = max(0, min(int((pt[0] - pad_x) / scale), img_w - 1))
        ry = max(0, min(int((pt[1] - pad_y) / scale), img_h - 1))
        real_pts.append([rx, ry])
    return np.array(real_pts, dtype=np.int32)


def _configure_worker_context(process_engine_mode, report_path):
    """設定 Windows spawn 子程序無法從主程序繼承的必要狀態。"""
    global engine_mode, current_report_path
    engine_mode = process_engine_mode
    current_report_path = report_path


def process_wrapper(
    video_path,
    video_name,
    settings,
    batch_output_dir,
    ui_queue,
    shared_state,
    model_name,
    process_engine_mode,
    report_path,
):
    import sys
    import threading
    import time
    from ultralytics import YOLO
    
    global eel, stop_requested, skip_video_path, player_state, global_live_settings
    global roi_points, scale_info, model
    
    class MockEel:
        def __getattr__(self, name):
            def wrapper(*args, **kwargs):
                ui_queue.put((name, args, kwargs))
                return lambda: None
            return wrapper
            
    eel = MockEel()
    
    stop_requested = False
    skip_video_path = None
    player_state = shared_state.get('player_state', {})
    global_live_settings = shared_state.get('live_settings', {})
    roi_points = shared_state.get('roi_points', [])
    scale_info = shared_state.get('scale_info', None)
    # Windows multiprocessing 使用 spawn，子程序不會繼承主程序的全域狀態。
    # 模式與鑑識紀錄路徑必須明確傳入，否則人工點視會退回 auto，
    # 子程序產生的截圖與錯誤也無法寫入本次鑑識紀錄。
    _configure_worker_context(process_engine_mode, report_path)
    
    sub_sync_running = True
    def sub_sync_thread():
        global stop_requested, skip_video_path, player_state, global_live_settings, roi_points, scale_info
        while sub_sync_running:
            stop_requested = shared_state.get('stop_requested', False)
            skip_video_path = shared_state.get('skip_video_path', None)
            player_state = shared_state.get('player_state', {})
            global_live_settings = shared_state.get('live_settings', {})
            roi_points = shared_state.get('roi_points', [])
            scale_info = shared_state.get('scale_info', None)
            time.sleep(0.05)
            
    threading.Thread(target=sub_sync_thread, daemon=True).start()
    
    eel.updateStatus(f"狀態: 正在載入 {model_name}大腦...", "ok")()
    model = YOLO(model_name)
    
    try:
        process_single_video(video_path, video_name, settings, batch_output_dir)
        sys.exit(0)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        eel.appendLog(f"影片 {video_name} 發生致命崩潰，已觸發看門狗安全略過: {str(e)}", "danger")()
        print(err)
        sys.exit(1)
    finally:
        sub_sync_running = False


def batch_processing_worker(settings):
    global is_processing, model, current_model_name, skip_video_path
    import multiprocessing
    import queue
    import traceback
    import gc
    
    try:
        model_name = settings.get("aiModel", "yolov8n.pt")

        with list_lock:
            q = list(video_queue)
        total_v = len(q)

        single_folder = settings.get("singleFolder", False)
        batch_output_dir = None
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        global current_report_path

        if single_folder:
            batch_output_dir = os.path.join(CONFIG.CAPTURES_DIR, f"Batch_{timestamp_str}")
            os.makedirs(batch_output_dir, exist_ok=True)
            current_report_path = os.path.join(batch_output_dir, "系統鑑識紀錄.txt")
        else:
            current_report_path = os.path.join(CONFIG.CAPTURES_DIR, f"系統鑑識紀錄_{timestamp_str}.txt")
            
        write_report("=== AG-MONITOR 科技偵查戰術分析紀錄 ===")
        write_report(f"AI 核心模型: {model_name}")
        write_report(f"執行模式: {engine_mode}")
        tracker_mode, tracker_config = resolve_tracker_config(settings)
        write_report(f"追蹤核心: {CONFIG.TRACKER_LABELS[tracker_mode]}")
        write_report(f"追蹤設定: {tracker_config}")
        write_report(f"遮蔽追蹤保活: {tracker_occlusion_grace_seconds(tracker_mode)} 秒")
        write_report(f"信心門檻: {settings.get('confThresh', 0.40)}")
        write_report(f"啟用類別: {format_enabled_classes(settings.get('classes', {}))}")
        write_report(f"蒐證模式: {settings.get('captureMode', '')}")
        write_report(f"極速背景處理: {'開啟' if settings.get('fastMode', True) else '關閉'}")
        write_report(f"靜止物件過濾: {'開啟' if settings.get('filterStationary', True) else '關閉'}")
        write_report(f"空景跳躍間隔: {settings.get('skipSec', 0.20)} 秒")
        write_report(f"批次集中資料夾: {'開啟' if single_folder else '關閉'}")
        write_report("=========================================\n")

        manager = multiprocessing.Manager()
        ui_queue = manager.Queue()
        shared_state = manager.dict({
            'stop_requested': False,
            'skip_video_path': None,
            'player_state': player_state,
            'live_settings': global_live_settings,
            'roi_points': roi_points,
            'scale_info': scale_info
        })
        
        sync_running = True
        def state_sync_thread():
            while sync_running:
                try:
                    shared_state['stop_requested'] = stop_requested
                    shared_state['skip_video_path'] = skip_video_path
                    shared_state['player_state'] = player_state
                    shared_state['live_settings'] = global_live_settings
                    shared_state['roi_points'] = roi_points
                    shared_state['scale_info'] = scale_info
                except (BrokenPipeError, EOFError, ConnectionResetError, FileNotFoundError, OSError):
                    break
                except Exception:
                    break
                time.sleep(0.05)
                
        threading.Thread(target=state_sync_thread, daemon=True).start()
        
        def ui_listener_thread():
            while sync_running:
                try:
                    msg = ui_queue.get(timeout=0.1)
                    if msg == "STOP": break
                    name, args, kwargs = msg
                    func = getattr(eel, name, None)
                    if func:
                        func(*args, **kwargs)()
                except queue.Empty:
                    pass
                except Exception:
                    pass
                    
        threading.Thread(target=ui_listener_thread, daemon=True).start()

        current_idx = 0
        while current_idx < total_v:
            if stop_requested:
                break
            
            video_path = q[current_idx]
            # Reset skip path for next video
            global skip_video_path
            skip_video_path = None
            shared_state['skip_video_path'] = None
            
            v_name = os.path.basename(video_path)
            
            eel.updateStatus(f"狀態: 正在分析 ({current_idx + 1}/{total_v}) {v_name}", "ok")
            eel.appendLog(f"開始載入影片: {v_name}", "info")
            write_report(f"▶ 開始分析影片 ({current_idx + 1}/{total_v}): {v_name}")
            try:
                evidence = build_evidence_metadata(video_path)
                write_report(f"  原始路徑: {evidence['path']}")
                write_report(f"  檔案大小: {evidence['size']} bytes")
                write_report(f"  檔案修改時間: {evidence['modified']}")
                write_report(f"  SHA-256: {evidence['sha256']}")
            except Exception as hash_error:
                write_report(f"  ❌ 無法建立原始證物雜湊: {hash_error}")
                eel.appendLog(f"{v_name} 無法建立 SHA-256，已停止以避免產生不可追溯證物", "danger")
                write_report(f"⛔ 分析狀態: 雜湊失敗，未進入分析\n")
                current_idx += 1
                continue
            
            p = multiprocessing.Process(
                target=process_wrapper, 
                args=(
                    video_path,
                    v_name,
                    settings,
                    batch_output_dir,
                    ui_queue,
                    shared_state,
                    model_name,
                    engine_mode,
                    current_report_path,
                )
            )
            p.start()
            
            # Watchdog loop: wait for process to finish or crash, while staying responsive to stop requests
            while p.is_alive():
                if stop_requested:
                    try:
                        shared_state['stop_requested'] = True
                    except Exception:
                        pass
                p.join(timeout=0.5)
            
            requested_path = None
            try:
                requested_path = shared_state.get('skip_video_path', None)
            except Exception:
                pass
            if p.exitcode != 0:
                write_report(f"❌ 分析狀態: 致命錯誤 (離開代碼: {p.exitcode}，看門狗已介入)\n")
            elif stop_requested:
                write_report("⏹️ 分析狀態: 使用者中止\n")
            elif requested_path is not None:
                write_report(f"⏭️ 分析狀態: 使用者要求跳轉至 {os.path.basename(requested_path)}\n")
            else:
                write_report(f"✅ 分析狀態: 完成 {v_name}\n")
            gc.collect()
            
            # Check if user requested to skip to a specific video during this process
            skip_path = requested_path
            if skip_path is not None:
                try:
                    current_idx = q.index(skip_path)
                except ValueError:
                    current_idx += 1
            else:
                current_idx += 1

        if stop_requested:
            eel.updateStatus("狀態: 已由使用者手動中止", "danger")
            eel.appendLog("任務被中斷", "warn")
            write_report("=== 批次任務狀態: 使用者中止 ===")
        else:
            eel.updateProgress(100, "")
            eel.updateStatus("狀態: 全部完成！", "ok")
            eel.appendLog("所有佇列影片處理完成", "success")
            write_report("=== 批次任務狀態: 全部完成 ===")

    except Exception as e:
        err_msg = traceback.format_exc()
        eel.updateStatus("系統崩潰", "danger")
        eel.appendLog(f"系統崩潰: {str(e)}", "error")
        write_report(f"=== 批次任務狀態: 系統崩潰 ({str(e)}) ===")
        print(err_msg)
    finally:
        sync_running = False
        is_processing = False
        eel.processingFinished()

def parse_start_time(filename):
    # Channel export: CH07-20260326-173728-184505 (YYYYMMDD-Start-End)
    match_channel_export = re.search(r'(?:^|[_-])CH\d+[_-](20\d{6})[_-](\d{6})[_-]\d{6}(?=\.|[_-]|$)', filename, re.IGNORECASE)
    if match_channel_export:
        try:
            return datetime.strptime(
                f"{match_channel_export.group(1)}_{match_channel_export.group(2)}",
                "%Y%m%d_%H%M%S",
            )
        except ValueError:
            pass

    # Dashcam/Special Format: P260625_015237_015747 (YYMMDD_StartTime_EndTime)
    match_yymmdd = re.search(r'[A-Za-z]?(\d{2})(\d{4})_(\d{6})_\d{6}', filename)
    if match_yymmdd:
        try:
            yy = int(match_yymmdd.group(1))
            year = 2000 + yy if yy < 50 else 1900 + yy
            return datetime.strptime(f"{year}{match_yymmdd.group(2)}_{match_yymmdd.group(3)}", "%Y%m%d_%H%M%S")
        except: pass

    match_15 = re.search(r'(20\d{6}_\d{6})', filename)
    if match_15:
        try: return datetime.strptime(match_15.group(1), "%Y%m%d_%H%M%S")
        except: pass

    match_h_m = re.search(r'(20\d{6}_\d{2}h\d{2}m)', filename)
    if match_h_m:
        try:
            time_str = match_h_m.group(1).replace('h', '').replace('m', '') + '00'
            return datetime.strptime(time_str, "%Y%m%d_%H%M%S")
        except: pass
        
    match_14 = re.search(r'(20\d{12})', filename)
    if match_14:
        try: return datetime.strptime(match_14.group(1), "%Y%m%d%H%M%S")
        except: pass

    match_roc_us = re.search(r'([01]\d{6}_\d{6})', filename)
    if match_roc_us:
        try:
            roc_str = match_roc_us.group(1)
            year = int(roc_str[:3]) + 1911
            return datetime.strptime(f"{year}{roc_str[3:]}", "%Y%m%d_%H%M%S")
        except: pass

    match_roc13 = re.search(r'([01]\d{12})', filename)
    if match_roc13:
        try:
            roc_str = match_roc13.group(1)
            year = int(roc_str[:3]) + 1911
            return datetime.strptime(f"{year}{roc_str[3:]}", "%Y%m%d%H%M%S")
        except: pass

    match_unix = re.search(r'(1\d{9}|2[0-4]\d{8})', filename)
    if match_unix:
        try: return datetime.fromtimestamp(int(match_unix.group(1)))
        except: pass

    return None

def format_timecode(milliseconds, start_time=None):
    if start_time:
        dt = start_time + timedelta(milliseconds=milliseconds)
        ms = int(milliseconds % 1000)
        return dt.strftime("%Y/%m/%d %H:%M:%S") + f".{ms // 100:01d}"
    
    seconds = int(milliseconds / 1000)
    ms = int(milliseconds % 1000)
    mins = seconds // 60
    secs = seconds % 60
    hrs = mins // 60
    mins = mins % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}.{ms // 100:01d}"

def process_single_video(video_path, video_name, settings, batch_output_dir=None):
    global stop_requested, real_roi_poly, skip_video_path
    
    clean_v_name = os.path.splitext(video_name)[0]
    clean_v_name = "".join([c for c in clean_v_name if c.isalnum() or c in (".", "_", "-", "[", "]")]).rstrip()
    if batch_output_dir:
        output_dir = batch_output_dir
    else:
        output_dir = os.path.join(CONFIG.CAPTURES_DIR, clean_v_name)
        os.makedirs(output_dir, exist_ok=True)
    real_roi_poly = get_real_roi_polygon()
    start_time_dt = parse_start_time(video_name)
    tracker_mode, tracker_config = resolve_tracker_config(settings)

    fh = None
    container = None
    try:
        ext = os.path.splitext(video_path)[1].lower()
        fmt = None
        if ext in ['.265', '.h265']:
            fmt = 'hevc'
        elif ext in ['.264', '.h264', '.dav']:
            fmt = 'h264'
        elif ext in ['.ts']:
            fmt = 'mpegts'

        fh = open(video_path, 'rb')
        try:
            container = av.open(fh, format=fmt, metadata_errors='ignore')
        except Exception:
            container = av.open(video_path, format=fmt, metadata_errors='ignore')
        stream = container.streams.video[0]
        stream.thread_type = "NONE"
        
        fps = float(stream.average_rate) if stream.average_rate else 30.0
        if fps <= 0: fps = 30.0
        occlusion_grace_frames = max(1, int(fps * tracker_occlusion_grace_seconds(tracker_mode)))
        reidentify_grace_msec = int(CONFIG.TRACK_REIDENTIFY_GRACE_SEC * 1000)
        
        total_frames = stream.frames
        if total_frames <= 0:
            total_frames = int(float(stream.duration * stream.time_base) * fps) if stream.duration else 1000

        global scale_info
        img_w, img_h = stream.width or 800, stream.height or 600
        canvas_w, canvas_h = 800, 600
        scale = min(canvas_w / img_w, canvas_h / img_h)
        new_w, new_h = int(img_w * scale), int(img_h * scale)
        scale_info = (scale, (canvas_w - new_w) // 2, (canvas_h - new_h) // 2, img_w, img_h)

        conf_thresh, class_vars = settings['confThresh'], settings['classes']
        capture_mode, fast_mode = settings.get('captureMode', ''), settings.get('fastMode', True)
        filter_stationary = settings.get('filterStationary', True)
        skip_sec = float(settings.get('skipSec', 0.20))
        inference_size = int(settings.get('inferenceSize', 960))
        rider_assist = bool(settings.get('riderAssist', True))
        static_skip_step = max(1, int(fps * skip_sec))
        
        track_states, id_alias_map = {}, {}
        target_frame_idx, decoded_frame_idx, is_dynamic_mode = 0, -1, False
        dynamic_lock_until, no_target_frames = 0, 0
        
        is_raw_stream = (stream.duration is None)
        stream.codec_context.skip_frame = 'DEFAULT'
        frame_iter = container.decode(stream)
        current_av_frame = None
        raw_skip_counter = 0       # 用來控制 Raw 流靜態模式的 YOLO 執行頻率
        last_progress_update = 0   # 上次更新進度條的時間

        import queue
        frame_queue = queue.Queue(maxsize=15)
        command_queue = queue.Queue()
        decode_thread_running = True
        deadlock_detected = False
        decoder_health = {"phase": "idle", "since": time.monotonic(), "slow_warned": False}
        decoder_health_lock = threading.Lock()

        def set_decoder_phase(phase, made_progress=False):
            with decoder_health_lock:
                decoder_health["phase"] = phase
                if made_progress or phase != "decoding":
                    decoder_health["since"] = time.monotonic()
                    decoder_health["slow_warned"] = False
        
        def watchdog_worker():
            nonlocal deadlock_detected
            while decode_thread_running:
                time.sleep(1)
                with decoder_health_lock:
                    phase = decoder_health["phase"]
                    elapsed = time.monotonic() - decoder_health["since"]
                    slow_warned = decoder_health["slow_warned"]
                if phase == "decoding" and elapsed >= CONFIG.DECODER_SLOW_WARN_SEC and not slow_warned:
                    with decoder_health_lock:
                        decoder_health["slow_warned"] = True
                    dlog(f"[WATCHDOG] 解碼耗時偏長但尚未熔斷: {elapsed:.1f}s")
                    write_report(f"⚠️ 解碼器回應偏慢: {video_name}，已等待 {elapsed:.1f} 秒")
                if should_trigger_decoder_deadlock(phase, elapsed):
                    deadlock_detected = True
                    dlog(f"[WATCHDOG] 🚨 解碼階段 {elapsed:.1f}s 無進展，觸發熔斷")
                    write_report(f"🚨 影片讀取失敗 (解碼階段 {elapsed:.1f} 秒無進展): {video_name}")
                    eel.appendLog(f"🚨 {clean_v_name} 解碼超過 {CONFIG.DECODER_DEADLOCK_SEC:.0f} 秒無回應，看門狗已熔斷", "danger")
                    break

        watchdog_thread = threading.Thread(target=watchdog_worker, daemon=True)
        watchdog_thread.start()

        def decoding_worker():
            nonlocal container, stream
            frame_iter = container.decode(stream)
            local_decoded_idx = -1
            consecutive_errors = 0
            
            while decode_thread_running:
                # 混沌測試 (Chaos Monkey)
                if os.path.exists("sim_crash.txt") and local_decoded_idx == 30:
                    dlog("[CHAOS MONKEY] 🐒 觸發混沌測試！故意睡眠 10 秒製造死鎖...")
                    time.sleep(10)

                try:
                    cmd = command_queue.get_nowait()
                    if cmd['action'] == 'seek':
                        target_idx = cmd['target']
                        pts = int(target_idx / fps / float(stream.time_base))
                        try:
                            container.seek(pts, stream=stream, backward=True)
                            frame_iter = container.decode(stream)
                            local_decoded_idx = -1
                        except Exception as seek_err:
                            dlog(f"[DECODER] Seek 失敗 (pts={pts}, target_idx={target_idx}): {seek_err}")
                            try:
                                frame_iter = container.decode(stream)
                            except Exception:
                                pass
                        # Flush existing queue items
                        while not frame_queue.empty():
                            try:
                                frame_queue.get_nowait()
                            except queue.Empty:
                                break
                except queue.Empty:
                    pass

                try:
                    set_decoder_phase("decoding")
                    f = next(frame_iter)
                    set_decoder_phase("decoded", made_progress=True)
                    consecutive_errors = 0
                    if local_decoded_idx == -1:
                        # 剛完成 seek，必須依賴 PTS 來建立新的基準點
                        if f.pts:
                            local_decoded_idx = int(float(f.pts * stream.time_base) * fps)
                        else:
                            # 萬一連 seek 後都沒有 PTS，只能硬塞 target_idx 作為基準
                            local_decoded_idx = target_idx if 'target_idx' in locals() else 0
                    else:
                        # 循序解碼中，嚴格加 1，無視壞掉的 PTS 避免跳躍或倒退
                        local_decoded_idx += 1
                        
                    bgr_frame = f.to_ndarray(format='bgr24')
                    
                    while decode_thread_running:
                        try:
                            set_decoder_phase("queue_wait")
                            frame_queue.put({'idx': local_decoded_idx, 'frame': bgr_frame}, timeout=0.05)
                            set_decoder_phase("idle", made_progress=True)
                            break
                        except queue.Full:
                            continue
                except StopIteration:
                    set_decoder_phase("finished", made_progress=True)
                    frame_queue.put({'idx': -1, 'frame': None})
                    break
                except Exception as e:
                    set_decoder_phase("decode_error", made_progress=True)
                    consecutive_errors += 1
                    if consecutive_errors >= CONFIG.DECODER_MAX_CONSECUTIVE_ERRORS:
                        dlog(f"[DECODER] 連續解碼錯誤達上限: {e}")
                        frame_queue.put({'idx': -2, 'frame': None, 'error': str(e)})
                        break
                    time.sleep(0.01)

        decoding_thread = threading.Thread(target=decoding_worker, daemon=True)
        decoding_thread.start()

        last_received_idx = -1

        def get_frame(target_idx):
            nonlocal last_received_idx
            
            # 若倒退或是跳躍過大，發送 seek 指令
            if target_idx < last_received_idx or (target_idx - last_received_idx) > 30:
                command_queue.put({'action': 'seek', 'target': target_idx})
                
            while decode_thread_running and not deadlock_detected:
                try:
                    item = frame_queue.get(timeout=0.1)
                    if item['idx'] == -1:
                        return None
                    if item['idx'] == -2:
                        raise RuntimeError(f"解碼器連續錯誤達安全上限: {item.get('error', 'unknown')}")
                    last_received_idx = item['idx']
                    if last_received_idx >= target_idx:
                        return item['frame']
                except queue.Empty:
                    if deadlock_detected:
                        raise RuntimeError("Watchdog triggered deadlock interruption")
                    if not decode_thread_running:
                        return None
                    continue
            
            if deadlock_detected:
                raise RuntimeError("Watchdog triggered deadlock interruption")
            return None

        last_ui_update, last_pushed_idx = time.time(), -1
        
        while True:
            if stop_requested or skip_video_path is not None:
                break
            if deadlock_detected:
                raise RuntimeError("Watchdog triggered deadlock interruption")
            
            with player_lock:
                req_seek = player_state['seek_req']
                req_step = player_state['step_req']
                is_play = player_state['playing']
                is_rev = player_state['reverse']
                p_speed = player_state['speed']
                req_capture = player_state['manual_capture_req']
                player_state['seek_req'] = None
                player_state['step_req'] = 0
                player_state['manual_capture_req'] = False

            if engine_mode == 'manual':
                if stream.codec_context.skip_frame != 'DEFAULT':
                    stream.codec_context.skip_frame = 'DEFAULT'
                target_idx = target_frame_idx
                
                if req_seek is not None:
                    target_idx = int((req_seek / 100.0) * total_frames)
                elif req_step != 0:
                    target_idx = max(0, target_idx + req_step)
                elif is_play:
                    if is_rev:
                        target_idx = max(0, target_idx - int(1 * p_speed))
                    else:
                        target_idx = target_idx + int(1 * p_speed)

                if target_idx != decoded_frame_idx or current_av_frame is None:
                    frame = get_frame(target_idx)
                    if frame is None and is_play:
                        break
                else:
                    frame = current_av_frame.to_ndarray(format='bgr24') if current_av_frame else None

                target_frame_idx = target_idx

                if frame is not None:
                    milliseconds = (target_idx / fps) * 1000
                    time_code_str = format_timecode(milliseconds, start_time_dt)
                    
                    annotated = frame.copy()
                    if real_roi_poly is not None:
                        # 雙層高對比輪廓 (黑底 4px + 螢光綠 2px)
                        cv2.polylines(annotated, [real_roi_poly], True, (0, 0, 0), 4)
                        cv2.polylines(annotated, [real_roi_poly], True, (0, 255, 128), 2)
                    
                    if req_capture:
                        save_legal_screenshot(annotated, output_dir, time_code_str, ["Manual Capture"], clean_v_name)
                        eel.appendLog(f"[{time_code_str}] 📸 手動快門擷取成功", "success")

                    # Draw OSD Timecode
                    frame_h, frame_w = annotated.shape[:2]
                    osd_text = f"AG-MONITOR | {time_code_str}"
                    (tw, th), _ = cv2.getTextSize(osd_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                    cv2.rectangle(annotated, (5, frame_h - th - 15), (5 + tw + 10, frame_h - 5), (0, 0, 0), -1)
                    cv2.putText(annotated, osd_text, (10, frame_h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

                    now = time.time()
                    if target_idx != last_pushed_idx or (now - last_ui_update > 0.03 and is_play):
                        push_frame_to_ui(annotated)
                        eel.updateProgress(min(100, (target_idx / total_frames) * 100), time_code_str)
                        last_ui_update = now
                        last_pushed_idx = target_idx

                if is_play:
                    time.sleep(1.0 / (fps * p_speed) if p_speed < 8 else 0.01)
                else:
                    time.sleep(0.01)


            else:
                live_values = resolve_live_processing_settings(global_live_settings, {
                    'confThresh': conf_thresh,
                    'fastMode': fast_mode,
                    'skipSec': skip_sec,
                    'classes': class_vars,
                    'captureMode': capture_mode,
                    'filterStationary': filter_stationary,
                    'inferenceSize': inference_size,
                    'riderAssist': rider_assist,
                })
                conf_thresh = live_values['confThresh']
                fast_mode = live_values['fastMode']
                skip_sec = live_values['skipSec']
                class_vars = live_values['classes']
                capture_mode = live_values['captureMode']
                filter_stationary = live_values['filterStationary']
                inference_size = int(live_values.get('inferenceSize', 960))
                rider_assist = bool(live_values.get('riderAssist', True))
                static_skip_step = max(1, int(fps * skip_sec))
                
                # 依據動態狀態切換解碼模式
                if not is_dynamic_mode:
                    if is_raw_stream:
                        # Raw 流：必須逐幀解碼，但由 raw_skip_counter 控制只每 N 幀才距行 YOLO
                        frame = get_frame(target_frame_idx)
                        if frame is None:
                            dlog(f"[DEBUG-LOOP] get_frame returned None at frame {target_frame_idx}, breaking")
                            break
                        raw_skip_counter += 1
                        if raw_skip_counter < static_skip_step:
                            # 這幀跳過 YOLO，只更新進度條並進到下一幀
                            target_frame_idx += 1
                            _now = time.time()
                            if _now - last_ui_update > 0.5:
                                push_frame_to_ui(frame, [], None, time_code_str)
                                last_ui_update = _now
                            if _now - last_progress_update > 0.2:
                                ms = (target_frame_idx / fps) * 1000
                                t_str = format_timecode(ms, start_time_dt)
                                eel.updateProgress(min(100, (target_frame_idx / total_frames) * 100), t_str)
                                last_progress_update = _now
                            continue
                        else:
                            raw_skip_counter = 0  # 重置計數器，這幀執行 YOLO
                    else:
                        stream.codec_context.skip_frame = 'DEFAULT'  # 依賴 get_frame 內部的 seek，關閉 NONKEY 避免跳躍過大
                        frame = get_frame(target_frame_idx)
                        if frame is None:
                            dlog(f"[DEBUG-LOOP] static get_frame returned None at frame {target_frame_idx}, breaking")
                            break

                else:
                    if not is_raw_stream:
                        stream.codec_context.skip_frame = 'DEFAULT'  # 動態追蹤：逐幀完整解碼
                    frame = get_frame(target_frame_idx)
                    if frame is None:
                        dlog(f"[DEBUG-LOOP] dynamic get_frame returned None at frame {target_frame_idx}, breaking")
                        break

                milliseconds = (target_frame_idx / fps) * 1000
                time_code_str = format_timecode(milliseconds, start_time_dt)
                
                now = time.time()
                if now - last_progress_update > 0.1:
                    eel.updateProgress(min(100, (target_frame_idx / total_frames) * 100), time_code_str)
                    last_progress_update = now

                # ---------------- YOLO Detection (高解析推論 + 視角/小目標特化) ----------------
                track_kwargs = {'conf': conf_thresh, 'verbose': False}
                if inference_size and inference_size != 640:
                    track_kwargs['imgsz'] = inference_size

                if is_dynamic_mode:
                    results = model.track(
                        frame,
                        persist=True,
                        tracker=tracker_config,
                        **track_kwargs,
                    )[0]
                else:
                    results = model.predict(frame, **track_kwargs)[0]
                    
                boxes = results.boxes
                annotated_frame = frame.copy()
                valid_targets = []

                if boxes is not None:
                    moto_or_bike_enabled = class_vars.get("3", True) or class_vars.get("1", True)
                    for box in boxes:
                        conf = float(box.conf[0])
                        if conf < conf_thresh:
                            continue
                        cls_id = int(box.cls[0])

                        # 智慧正面/多角度騎士關聯補償：
                        # 若為 Person (0) 但使用者勾選了機車/單車，且長寬比呈騎乘特徵 (h/w >= 1.1)
                        is_likely_rider = False
                        if rider_assist and cls_id == 0 and moto_or_bike_enabled and not class_vars.get("0", False):
                            xyxy_tmp = box.xyxy[0].cpu().numpy()
                            w_tmp = max(1, xyxy_tmp[2] - xyxy_tmp[0])
                            h_tmp = max(1, xyxy_tmp[3] - xyxy_tmp[1])
                            aspect_ratio = h_tmp / w_tmp
                            if 1.1 <= aspect_ratio <= 3.2:
                                is_likely_rider = True

                        if cls_id not in CONFIG.TARGET_CLASSES:
                            continue
                        if not class_vars.get(str(cls_id), True) and not is_likely_rider:
                            continue

                        raw_tid = int(box.id[0]) if box.id is not None else 0
                        tid = id_alias_map.get(raw_tid, raw_tid) if raw_tid != 0 else 0
                        xyxy = box.xyxy[0].cpu().numpy()
                        x1, y1, x2, y2 = map(int, xyxy)

                        inside_roi = True
                        if real_roi_poly is not None:
                            pts_to_test = [
                                ((x1 + x2) / 2.0, float(y2)),
                                ((x1 + x2) / 2.0, (y1 + y2) / 2.0),
                                (float(x1), float(y2)),
                                (float(x2), float(y2)),
                                ((x1 + x2) / 2.0, float(y1)),
                            ]
                            inside_roi = any(cv2.pointPolygonTest(real_roi_poly, pt, False) >= 0 for pt in pts_to_test)
                            if not inside_roi and len(real_roi_poly) > 0:
                                for rpt in real_roi_poly:
                                    rx, ry = rpt[0], rpt[1]
                                    if x1 <= rx <= x2 and y1 <= ry <= y2:
                                        inside_roi = True
                                        break

                        if inside_roi:
                            target_cls = 3 if (is_likely_rider and class_vars.get("3", True)) else cls_id
                            valid_targets.append({'tid': tid, 'raw_tid': raw_tid, 'conf': conf, 'cls_id': target_cls, 'xyxy': (x1, y1, x2, y2)})

                # ---------------- Filter Overlapping Targets (人車合一與精準去重) ----------------
                drop_indices = set()
                for i in range(len(valid_targets)):
                    if i in drop_indices: continue
                    t1 = valid_targets[i]
                    x1_1, y1_1, x2_1, y2_1 = t1['xyxy']
                    area1 = max(1, (x2_1 - x1_1) * (y2_1 - y1_1))
                    
                    for j in range(i + 1, len(valid_targets)):
                        if j in drop_indices: continue
                        t2 = valid_targets[j]
                        x1_2, y1_2, x2_2, y2_2 = t2['xyxy']
                        area2 = max(1, (x2_2 - x1_2) * (y2_2 - y1_2))
                        
                        ix1, iy1 = max(x1_1, x1_2), max(y1_1, y1_2)
                        ix2, iy2 = min(x2_1, x2_2), min(y2_1, y2_2)
                        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
                        inter_area = iw * ih
                        if inter_area > 0:
                            is_rider = (t1['cls_id'] == 0 and t2['cls_id'] in [1, 2, 3, 5, 7]) or (t1['cls_id'] in [1, 2, 3, 5, 7] and t2['cls_id'] == 0)
                            overlap_ratio = inter_area / min(area1, area2)
                            if is_rider:
                                if overlap_ratio > 0.15:
                                    if t1['cls_id'] != 0:
                                        drop_indices.add(j)
                                        t1['xyxy'] = (min(x1_1, x1_2), min(y1_1, y1_2), max(x2_1, x2_2), max(y2_1, y2_2))
                                    else:
                                        drop_indices.add(i)
                                        t2['xyxy'] = (min(x1_1, x1_2), min(y1_1, y1_2), max(x2_1, x2_2), max(y2_1, y2_2))
                                        break
                            else:
                                union_area = area1 + area2 - inter_area
                                iou = inter_area / union_area if union_area > 0 else 0
                                if iou > 0.88:
                                    if t1['conf'] >= t2['conf']:
                                        drop_indices.add(j)
                                    else:
                                        drop_indices.add(i)
                                        break
                                
                final_targets = []
                for i, t in enumerate(valid_targets):
                    if i not in drop_indices:
                        final_targets.append(t)
                
                valid_targets = final_targets

                # ---------------- Motion & Skip Logic ----------------
                motion_detected = len(valid_targets) > 0

                if motion_detected:
                    no_target_frames = 0
                else:
                    no_target_frames += 1

                if is_dynamic_mode:
                    if no_target_frames < occlusion_grace_frames:
                        motion_detected = True

                if is_dynamic_mode and target_frame_idx < dynamic_lock_until:
                    motion_detected = True

                if not is_dynamic_mode:
                    if motion_detected:
                        dynamic_lock_until = target_frame_idx
                        old_target = target_frame_idx
                        
                        # 確保給予追蹤器至少 0.5 秒的「起跑準備時間 (Run-up time)」，
                        # 否則像 0.2s 這種極短的跳躍，會導致 ByteTrack 還沒確立目標就撞到 lock_until，
                        # 進而引發不斷退出又被 predict 抓回來的「搞1秒重複」跳回迴圈。
                        run_up_frames = max(int(fps * 0.5), static_skip_step)
                        target_frame_idx = max(0, target_frame_idx - run_up_frames)
                        
                        is_dynamic_mode = True
                        
                        # 重置 YOLO 追蹤器
                        if hasattr(model, 'predictor'):
                            model.predictor = None
                            
                        # 前端除錯提示：讓使用者確切看到「退回了多少幀 (時間)」以作為暖機
                        eel.appendLog(f"[{time_code_str}] 靜態掃描發現目標！觸發時光倒流防護，退回 {run_up_frames} 幀以供 Tracker 暖機鎖定。", "warning")
                        
                        if is_raw_stream:
                            # Raw 流：絕對不能 seek，直接保留現有迭代器，重置 target 計數器即可
                            target_frame_idx = old_target
                            dlog(f"[DEBUG-SKIP] Raw stream: skipping seek, staying at frame {target_frame_idx}")
                        else:
                            # 依賴 get_frame 內部的 `if target_idx < last_received_idx:` 自動觸發唯一一次的 seek，
                            # 這裡不要重複 put seek，否則會產生多個過期的 seek command 造成時序錯亂回彈。
                            pass
                        continue
                    else:
                        _run_grace_period_gc(
                            milliseconds, track_states, capture_mode, output_dir, clean_v_name,
                            filter_stationary, reidentify_grace_msec,
                        )
                        if is_raw_stream:
                            target_frame_idx += 1  # Raw 流靜態模式：每幀前進 1
                        else:
                            target_frame_idx += static_skip_step
                        # 靜態空景模式：每 0.5 秒仍推送一幀到 UI，確保預覽畫面不凍結
                        _now = time.time()
                        if _now - last_ui_update > 0.5:
                            # 畫上 OSD 否則實時畫面沒時間碼
                            frame_h, frame_w = annotated_frame.shape[:2]
                            osd_text = f"AG-MONITOR | {time_code_str}"
                            (tw, th), _ = cv2.getTextSize(osd_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                            cv2.rectangle(annotated_frame, (5, frame_h - th - 15), (5 + tw + 10, frame_h - 5), (0, 0, 0), -1)
                            cv2.putText(annotated_frame, osd_text, (10, frame_h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                            push_frame_to_ui(frame, [], real_roi_poly, time_code_str)
                            last_ui_update = _now
                            
                            # 前端除錯提示：讓使用者確認 Timecode 是否有在穩定前進，沒有卡死
                            eel.appendLog(f"[{time_code_str}] ⏩ 空景閃現推進中... (設定間距: {skip_sec}s)", "info")
                            
                            # Backend debug (隱藏於 UI，寫入背景日誌檔)
                            debug_msg = f"[DEBUG] target={target_frame_idx}, skip={static_skip_step}, fps={fps}, last_idx={last_received_idx}"
                            dlog(debug_msg)
                        continue
                else:
                    if not motion_detected and target_frame_idx >= dynamic_lock_until:
                        is_dynamic_mode = False
                        raw_skip_counter = 0  # 重新進入靜態模式，重置跳過計數器
                        if is_raw_stream:
                            target_frame_idx += 1
                        else:
                            target_frame_idx += static_skip_step
                        _run_grace_period_gc(
                            milliseconds, track_states, capture_mode, output_dir, clean_v_name,
                            filter_stationary, reidentify_grace_msec,
                        )
                        
                        # 退出動態模式：推送最後一幀讓使用者看到
                        _now = time.time()
                        if _now - last_ui_update > 0.5:
                            frame_h, frame_w = annotated_frame.shape[:2]
                            osd_text = f"AG-MONITOR | {time_code_str}"
                            (tw, th), _ = cv2.getTextSize(osd_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                            cv2.rectangle(annotated_frame, (5, frame_h - th - 15), (5 + tw + 10, frame_h - 5), (0, 0, 0), -1)
                            cv2.putText(annotated_frame, osd_text, (10, frame_h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                            push_frame_to_ui(frame, [], real_roi_poly, time_code_str)
                            last_ui_update = _now
                        continue


                # ---------------- Track States Management ----------------
                for target in valid_targets:
                    raw_tid = target['raw_tid']
                    tid = target['tid']
                    
                    if raw_tid in id_alias_map:
                        tid = id_alias_map[raw_tid]
                        
                    conf = target['conf']
                    cls_name = CONFIG.TARGET_CLASSES[target['cls_id']]
                    summary_str = f"ID:{tid} {cls_name}"
                    x1, y1, x2, y2 = target['xyxy']
                    w, h = x2 - x1, y2 - y1
                    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                    
                    if tid not in track_states:
                        matched_old_tid = None
                        for old_tid, old_state in track_states.items():
                            if old_state['class_name'] != cls_name:
                                continue
                            if old_state['last_seen_msec'] == milliseconds:
                                continue
                            
                            time_diff = milliseconds - old_state['last_seen_msec']
                            if 0 < time_diff <= reidentify_grace_msec:
                                old_cx, old_cy = old_state.get('last_centroid', (cx, cy))
                                dist = math.hypot(cx - old_cx, cy - old_cy)
                                last_w, last_h = old_state.get('last_box_size', (w, h))
                                radius = max(80, min(200, max(last_w, last_h) * 1.2))
                                if dist <= radius:
                                    matched_old_tid = old_tid
                                    break
                        
                        if matched_old_tid is not None:
                            id_alias_map[raw_tid] = matched_old_tid
                            tid = matched_old_tid
                            summary_str = f"ID:{tid} {cls_name}"
                        else:
                            track_states[tid] = {
                                'class_name': cls_name,
                                'best_conf': conf,
                                'start_frame': annotated_frame.copy(),
                                'start_timecode': time_code_str,
                                'start_target_info': target.copy(),
                                'best_frame': annotated_frame.copy(),
                                'best_timecode': time_code_str,
                                'best_summary': [f"{summary_str}({conf:.2f} Peak)"],
                                'best_target_info': target.copy(),
                                'last_frame': annotated_frame.copy(),
                                'last_timecode': time_code_str,
                                'last_target_info': target.copy(),
                                'last_seen_msec': milliseconds,
                                'last_continuous_capture_msec': milliseconds,
                                'last_centroid': (cx, cy),
                                'start_box_size': (w, h),
                                'last_box_size': (w, h),
                                'start_centroid': (cx, cy),
                                'is_moving': False,
                                'motion_confirmations': 0,
                                'entry_captured': False
                            }
                            
                    if tid in track_states:
                        state = track_states[tid]
                        state['last_seen_msec'] = milliseconds
                        state['last_frame'] = annotated_frame.copy()
                        state['last_timecode'] = time_code_str
                        state['last_target_info'] = target.copy()
                        state['last_centroid'] = (cx, cy)
                        state['last_box_size'] = (w, h)
                        
                        if conf > state['best_conf']:
                            state['best_conf'] = conf
                            state['best_frame'] = annotated_frame.copy()
                            state['best_timecode'] = time_code_str
                            state['best_summary'] = [f"{summary_str}({conf:.2f} Peak)"]
                            state['best_target_info'] = target.copy()
                            
                        if not state['is_moving'] and record_motion_observation(state, (cx, cy), (w, h)):
                            dlog(f"[DEBUG-MOVE] ID:{tid} {state['class_name']} 已確認移動")
                            eel.appendLog(f"[{time_code_str}] ID:{tid} {state['class_name']} 已確認移動，開始蒐證", "info")
                        state['prev_centroid'] = (cx, cy)
                                
                        if state['is_moving'] and not state['entry_captured']:
                            state['entry_captured'] = True
                            dlog(f"[DEBUG-CAPTURE] 準備截圖! mode={capture_mode} output_dir={output_dir}")
                            if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "事件起訖模式"]:
                                save_legal_screenshot(state['start_frame'], output_dir, state['start_timecode'], [f"ID:{tid} {state['class_name']}(Entry)"], clean_v_name, state.get('start_target_info'))
                                eel.appendLog(f"[{state['start_timecode']}] 擷取 ID:{tid} {state['class_name']}(Entry)", "success")
                            elif capture_mode == "持續追蹤模式 (預設)":
                                save_legal_screenshot(state['start_frame'], output_dir, state['start_timecode'], [f"ID:{tid} {state['class_name']}(Track-Entry)"], clean_v_name, state.get('start_target_info'))
                                eel.appendLog(f"[{state['start_timecode']}] 擷取 ID:{tid} {state['class_name']}(Track-Entry)", "success")
                    
                    if capture_mode == "持續追蹤模式 (預設)":
                        state = track_states[tid]
                        if state['is_moving']:
                            if (milliseconds - state['last_continuous_capture_msec']) >= 3000:
                                state['last_continuous_capture_msec'] = milliseconds
                                save_legal_screenshot(annotated_frame, output_dir, time_code_str, [f"{summary_str}(Track)"], clean_v_name, target)
                                eel.appendLog(f"[{time_code_str}] 擷取 {summary_str}(Track)", "success")

                if engine_mode == 'auto' and is_dynamic_mode:
                    current_targets_str = ", ".join([f"ID:{t['tid']} {CONFIG.TARGET_CLASSES[t['cls_id']]}" for t in valid_targets])
                    if current_targets_str:
                        eel.updateStatus(f"狀態: 正在分析 (發現目標: {current_targets_str})", "ok")
                    else:
                        eel.updateStatus(f"狀態: 正在分析 (追蹤中...)", "ok")

                # Draw OSD Timecode
                frame_h, frame_w = annotated_frame.shape[:2]
                osd_text = f"AG-MONITOR | {time_code_str}"
                (tw, th), _ = cv2.getTextSize(osd_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(annotated_frame, (5, frame_h - th - 15), (5 + tw + 10, frame_h - 5), (0, 0, 0), -1)
                cv2.putText(annotated_frame, osd_text, (10, frame_h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

                if not fast_mode:
                    push_frame_to_ui(frame, valid_targets, real_roi_poly, time_code_str)
                    
                # 每幀都執行 GC，確保已移動物件在消失後立即儲存截圖
                _run_grace_period_gc(
                    milliseconds, track_states, capture_mode, output_dir, clean_v_name,
                    filter_stationary, reidentify_grace_msec,
                )

                target_frame_idx += 1 

        if engine_mode == 'auto':
            _flush_all_track_states(track_states, capture_mode, output_dir, clean_v_name, filter_stationary)
        decode_thread_running = False
        set_decoder_phase("stopped", made_progress=True)
        container.close()

    except Exception as e:
        err_msg = traceback.format_exc()
        write_report(f"  ❌ 影片解碼異常 ({video_name}): {str(e)}")
        eel.appendLog(f"[{video_name}] 解碼毀損診斷: {str(e)}", "error")
        eel.appendLog("處置建議: 可能是編碼異常或檔案殘缺，請重新提取原始檔案。", "warn")
        print(f"Exception for {video_name}:\n{err_msg}")
        raise
    finally:
        if 'decode_thread_running' in locals():
            decode_thread_running = False
        if container:
            try:
                container.close()
            except Exception:
                pass
        if fh:
            fh.close()

def push_frame_to_ui(frame, valid_targets=[], roi_poly=None, time_code_str=""):
    canvas_w, canvas_h = 800, 600
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img_h, img_w, _ = frame_rgb.shape
    scale = min(canvas_w / img_w, canvas_h / img_h)
    new_w, new_h = int(img_w * scale), int(img_h * scale)
    pad_x = (canvas_w - new_w) // 2
    pad_y = (canvas_h - new_h) // 2
    img_resized = cv2.resize(frame_rgb, (new_w, new_h))
    canvas_img = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    canvas_img[pad_y:pad_y+new_h, pad_x:pad_x+new_w] = img_resized
    _, buffer = cv2.imencode('.jpg', cv2.cvtColor(canvas_img, cv2.COLOR_RGB2BGR))
    b64_str = base64.b64encode(buffer).decode('utf-8')
    info_obj = {"scale": scale, "pad_x": pad_x, "pad_y": pad_y, "canvas_w": canvas_w, "canvas_h": canvas_h}
    
    json_boxes = []
    for t in valid_targets:
        x1, y1, x2, y2 = t['xyxy']
        json_boxes.append({
            'tid': t['tid'],
            'cls_name': CONFIG.TARGET_CLASSES[t['cls_id']],
            'conf': float(t['conf']),
            'x1': float(x1 * scale + pad_x),
            'y1': float(y1 * scale + pad_y),
            'x2': float(x2 * scale + pad_x),
            'y2': float(y2 * scale + pad_y)
        })
        
    roi_pts = []
    if roi_poly is not None:
        for pt in roi_poly:
            flat_pt = pt.flatten() if hasattr(pt, 'flatten') else np.array(pt).flatten()
            roi_pts.append({
                'x': float(flat_pt[0] * scale + pad_x),
                'y': float(flat_pt[1] * scale + pad_y)
            })

    eel.setPreviewImage(b64_str, info_obj, json_boxes, roi_pts, time_code_str)()

def _run_grace_period_gc(
    curr_msec, track_states, capture_mode, output_dir, prefix_name,
    filter_stationary=True, reidentify_grace_msec=1500,
):
    expired_ids = []
    for tid, state in track_states.items():
        if (curr_msec - state['last_seen_msec']) > reidentify_grace_msec:
            expired_ids.append(tid)
    for tid in expired_ids:
        state = track_states[tid]
        if filter_stationary and not state['is_moving']:
            pass
        else:
            if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "單次最清晰模式 (推薦)"]:
                if state['best_frame'] is not None:
                    capture_path = save_legal_screenshot(state['best_frame'], output_dir, state['best_timecode'], state['best_summary'], prefix_name, state.get('best_target_info'))
                    if capture_path:
                        eel.appendLog(f"[{state['best_timecode']}] 擷取 {state['best_summary'][0]}", "success")
                    else:
                        eel.appendLog(f"[{state['best_timecode']}] 寫入 {state['best_summary'][0]} 失敗", "error")
            elif capture_mode == "事件起訖模式":
                if state['last_frame'] is not None:
                    capture_path = save_legal_screenshot(state['last_frame'], output_dir, state['last_timecode'], [f"ID:{tid} {state['class_name']}(Exit)"], prefix_name, state.get('last_target_info'))
                    if capture_path:
                        eel.appendLog(f"[{state['last_timecode']}] 擷取 ID:{tid} {state['class_name']}(Exit)", "success")
                    else:
                        eel.appendLog(f"[{state['last_timecode']}] 寫入 ID:{tid} {state['class_name']}(Exit) 失敗", "error")
        del track_states[tid]

def _flush_all_track_states(track_states, capture_mode, output_dir, prefix_name, filter_stationary=True):
    for tid, state in track_states.items():
        if filter_stationary and not state['is_moving']:
            continue
        if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "單次最清晰模式 (推薦)"]:
            if state['best_frame'] is not None:
                capture_path = save_legal_screenshot(state['best_frame'], output_dir, state['best_timecode'], state['best_summary'], prefix_name, state.get('best_target_info'))
                if capture_path:
                    eel.appendLog(f"[{state['best_timecode']}] 擷取 {state['best_summary'][0]}", "success")
                else:
                    eel.appendLog(f"[{state['best_timecode']}] 寫入 {state['best_summary'][0]} 失敗", "error")
        elif capture_mode == "事件起訖模式":
            if state['last_frame'] is not None:
                capture_path = save_legal_screenshot(state['last_frame'], output_dir, state['last_timecode'], [f"ID:{tid} {state['class_name']}(Exit)"], prefix_name, state.get('last_target_info'))
                if capture_path:
                    eel.appendLog(f"[{state['last_timecode']}] 擷取 ID:{tid} {state['class_name']}(Exit)", "success")
                else:
                    eel.appendLog(f"[{state['last_timecode']}] 寫入 ID:{tid} {state['class_name']}(Exit) 失敗", "error")
    track_states.clear()

CAPTURES_DIR = CONFIG.CAPTURES_DIR
CAPTURE_MANIFEST_FILENAME = "鑑識截圖清冊.jsonl"


def _append_capture_manifest(output_dir, capture_path, time_code, objects_list, source_prefix):
    """追加一筆可機讀的截圖鑑識紀錄，並立即刷入磁碟。"""
    record = {
        "version": 1,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "path": os.path.abspath(capture_path),
        "filename": os.path.basename(capture_path),
        "size": os.path.getsize(capture_path),
        "sha256": calculate_sha256(capture_path),
        "time_code": time_code,
        "targets": list(objects_list),
        "source_prefix": source_prefix,
        "report_path": os.path.abspath(current_report_path) if current_report_path else None,
    }
    manifest_dir = os.path.dirname(current_report_path) if current_report_path else output_dir
    manifest_path = os.path.join(manifest_dir, CAPTURE_MANIFEST_FILENAME)
    with open(manifest_path, "a", encoding="utf-8", newline="\n") as manifest_file:
        manifest_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest_file.flush()
        os.fsync(manifest_file.fileno())
    return record


def save_legal_screenshot(frame, output_dir, time_code, objects_list, prefix_name="evidence", target_info=None):
    dlog(f"[DEBUG-SAVE] save_legal_screenshot called: output_dir={output_dir}, time_code={time_code}")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        dlog(f"[DEBUG-SAVE] makedirs failed: {e}")
        return

    MIN_WIDTH = 1280
    if frame.shape[1] < MIN_WIDTH:
        scale = MIN_WIDTH / frame.shape[1]
        new_w = int(frame.shape[1] * scale)
        new_h = int(frame.shape[0] * scale)
        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    else:
        scale = 1.0
        
    if target_info is not None:
        x1, y1, x2, y2 = target_info['xyxy']
        x1, y1, x2, y2 = int(x1 * scale), int(y1 * scale), int(x2 * scale), int(y2 * scale)
        cls_id = target_info['cls_id']
        tid = target_info['tid']
        conf = target_info['conf']
        
        frame_w = frame.shape[1]
        if frame_w < 1280:
            thick, font_thick, font_scale = 1, 1, 0.4
        elif frame_w < 2000:
            thick, font_thick, font_scale = 2, 1, 0.6
        else:
            thick, font_thick, font_scale = 3, 2, 0.8
            
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), thick)
        cv2.putText(frame, f"ID:{tid} {CONFIG.TARGET_CLASSES[cls_id]} {conf:.2f}",
            (x1, max(15, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 255), font_thick)

    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(frame_rgb)
    draw = ImageDraw.Draw(pil_img)

    try:
        font = ImageFont.truetype("msjh.ttc", 16)
        small_font = ImageFont.truetype("msjh.ttc", 12)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", 16)
            small_font = ImageFont.truetype("arial.ttf", 12)
        except Exception:
            font = ImageFont.load_default()
            small_font = ImageFont.load_default()

    w, h = pil_img.size
    watermark_text = f"AG-MONITOR | Timecode: {time_code}"
    detail_text = f"Target: {', '.join(objects_list)}"

    try:
        text_bbox = draw.textbbox((0, 0), watermark_text, font=font)
        tw = text_bbox[2] - text_bbox[0]
        det_bbox = draw.textbbox((0, 0), detail_text, font=small_font)
        dw = det_bbox[2] - det_bbox[0]
    except AttributeError:
        tw, _ = draw.textsize(watermark_text, font=font)
        dw, _ = draw.textsize(detail_text, font=small_font)
    
    box_w = max(tw, dw) + 30
    box_h = 45
    
    bx1 = 15
    by1 = h - box_h - 15
    bx2 = bx1 + box_w
    by2 = by1 + box_h
    
    overlay = Image.new('RGBA', pil_img.size, (255, 255, 255, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    
    # 120 is roughly 47% opacity for the background
    overlay_draw.rectangle([(bx1, by1), (bx2, by2)], fill=(0, 0, 0, 120))
    overlay_draw.text((bx1 + 15, by1 + 5), watermark_text, fill=(255, 255, 0, 255), font=font)
    overlay_draw.text((bx1 + 15, by1 + 25), detail_text, fill=(255, 255, 255, 255), font=small_font)

    pil_img = pil_img.convert("RGBA")
    pil_img = Image.alpha_composite(pil_img, overlay)
    pil_img = pil_img.convert("RGB")
    if " " in time_code:
        parts = time_code.split(".")
        main_time = parts[0]
        safe_time_str = main_time.replace("/", "").replace(":", "").replace(" ", "_")
        if len(parts) > 1:
            safe_time_str += f"_{parts[1]}"
    else:
        safe_time_str = time_code.replace(":", "").replace(".", "_")
        
    filename = f"{prefix_name}_{safe_time_str}.jpg"
    final_path = os.path.join(output_dir, filename)
    dlog(f"[DEBUG-SAVE] Saving to: {final_path}")

    final_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    try:
        is_success, buffer = cv2.imencode('.jpg', final_bgr)
        if is_success:
            stem, extension = os.path.splitext(final_path)
            collision_index = 0
            while True:
                actual_path = final_path if collision_index == 0 else f"{stem}_{collision_index}{extension}"
                try:
                    with open(actual_path, 'xb') as f:
                        f.write(buffer.tobytes() if hasattr(buffer, "tobytes") else buffer)
                    break
                except FileExistsError:
                    collision_index += 1
            manifest_record = _append_capture_manifest(output_dir, actual_path, time_code, objects_list, prefix_name)
            dlog(f"[DEBUG-SAVE] ✅ File written OK: {actual_path}")
            write_report(
                f"  📸 [截圖] 時間: {time_code} | 目標: {', '.join(objects_list)} | "
                f"檔名: {os.path.basename(actual_path)} | SHA-256: {manifest_record['sha256']}"
            )
            return actual_path
        else:
            dlog(f"[DEBUG-SAVE] ❌ cv2.imencode failed for {final_path}")
            write_report(f"  ❌ [截圖失敗] 編碼錯誤: {filename}")
            return None
    except Exception as e:
        import traceback as tb
        dlog(f"[DEBUG-SAVE] ❌ Exception: {e}")
        dlog(tb.format_exc())
        write_report(f"  ❌ [截圖失敗] 寫入異常: {str(e)}")
        return None

# ==========================================
# 鑑識超解析 (Super Resolution) - NCNN 模組
# ==========================================
NCNN_MODEL_DIR = os.path.join(CONFIG.BASE_DIR, 'models', 'realesrgan')
NCNN_EXE_PATH = os.path.join(NCNN_MODEL_DIR, 'realesrgan-ncnn-vulkan.exe')
NCNN_DOWNLOAD_URL = 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip'
NCNN_ZIP_PATH = os.path.join(CONFIG.BASE_DIR, 'realesrgan-windows.zip')
NCNN_ZIP_SHA256 = 'ABC02804E17982A3BE33675E4D471E91EA374E65B70167ABC09E31ACB412802D'
NCNN_EXE_SHA256 = '07E49F7CBB4EDE01AE4DD4C399D3A7E5846E3D2085C3128EFF881E55CB7B1A0C'
NCNN_REQUIRED_FILES = (
    'realesrgan-ncnn-vulkan.exe',
    'models/realesrgan-x4plus.bin',
    'models/realesrgan-x4plus.param',
    'models/realesrgan-x4plus-anime.bin',
    'models/realesrgan-x4plus-anime.param',
)

sr_abort_flag = False


def validate_sr_engine(engine_dir=NCNN_MODEL_DIR, expected_exe_sha256=NCNN_EXE_SHA256):
    if not all(os.path.isfile(os.path.join(engine_dir, relative_path)) for relative_path in NCNN_REQUIRED_FILES):
        return False
    if expected_exe_sha256:
        return calculate_sha256(os.path.join(engine_dir, 'realesrgan-ncnn-vulkan.exe')) == expected_exe_sha256.upper()
    return True


def _validate_safe_zip(zip_ref):
    for member in zip_ref.infolist():
        member_path = member.filename.replace('\\', '/')
        path_parts = [part for part in member_path.split('/') if part]
        if member_path.startswith('/') or re.match(r'^[A-Za-z]:', member_path) or '..' in path_parts:
            raise ValueError(f"ZIP 含不安全路徑: {member.filename}")
        unix_mode = member.external_attr >> 16
        if (unix_mode & 0o170000) == 0o120000:
            raise ValueError(f"ZIP 不允許符號連結: {member.filename}")


def install_sr_engine_from_zip(
    zip_path,
    engine_dir=NCNN_MODEL_DIR,
    expected_sha256=NCNN_ZIP_SHA256,
    expected_exe_sha256=NCNN_EXE_SHA256,
):
    actual_sha256 = calculate_sha256(zip_path)
    if expected_sha256 and actual_sha256 != expected_sha256.upper():
        raise ValueError(f"Real-ESRGAN ZIP SHA-256 不符: {actual_sha256}")

    parent_dir = os.path.dirname(engine_dir)
    os.makedirs(parent_dir, exist_ok=True)
    staging_dir = tempfile.mkdtemp(prefix='.realesrgan-staging-', dir=parent_dir)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            _validate_safe_zip(zip_ref)
            zip_ref.extractall(staging_dir)
        executable_matches = list(Path(staging_dir).rglob('realesrgan-ncnn-vulkan.exe'))
        if len(executable_matches) != 1:
            raise ValueError("ZIP 內找不到唯一的 Real-ESRGAN 執行檔")
        extracted_root = str(executable_matches[0].parent)
        if not validate_sr_engine(extracted_root, expected_exe_sha256):
            raise ValueError("ZIP 缺少必要模型或執行檔")
        backup_dir = engine_dir + '.previous'
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        if os.path.exists(engine_dir):
            os.replace(engine_dir, backup_dir)
        try:
            shutil.move(extracted_root, engine_dir)
            if not validate_sr_engine(engine_dir, expected_exe_sha256):
                raise RuntimeError("Real-ESRGAN 安裝後驗證失敗")
        except Exception:
            if os.path.exists(engine_dir):
                shutil.rmtree(engine_dir)
            if os.path.exists(backup_dir):
                os.replace(backup_dir, engine_dir)
            raise
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
    finally:
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
    return actual_sha256

def check_and_download_sr_model():
    global sr_abort_flag
    if not validate_sr_engine():
        print(">>> [系統預檢] 偵測到本機缺乏 NCNN 超解析引擎 (realesrgan-ncnn-vulkan.exe)")
        print(">>> [系統動作] 正在背景非同步下載免安裝引擎，請稍候 (約 25MB)...")
        partial_path = NCNN_ZIP_PATH + '.part'
        try:
            os.makedirs(NCNN_MODEL_DIR, exist_ok=True)
            req = urllib.request.urlopen(NCNN_DOWNLOAD_URL, timeout=30)
            with open(partial_path, 'wb') as f:
                while True:
                    if sr_abort_flag:
                        print(">>> [系統動作] 使用者已強制中止引擎下載！")
                        req.close()
                        return False
                    chunk = req.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            req.close()
            os.replace(partial_path, NCNN_ZIP_PATH)
            print(">>> [系統動作] 下載完成，正在驗證雜湊並安全解壓縮引擎...")
            install_sr_engine_from_zip(NCNN_ZIP_PATH)
            os.remove(NCNN_ZIP_PATH)
            print(">>> [系統動作] NCNN 引擎驗證與解壓縮完成！")
        except Exception as e:
            print(f"❌ [數位鑑識崩潰]：NCNN 引擎下載失敗 ({e})")
            print("💡 [系統處置建議]：請確認對外網路連線，或手動下載並解壓縮至 models/realesrgan/ 目錄。")
            for download_path in (partial_path, NCNN_ZIP_PATH):
                if os.path.exists(download_path):
                    os.remove(download_path)
            return False
    return True

@eel.expose
def abort_ai_super_resolution():
    global sr_abort_flag
    sr_abort_flag = True
    print(">>> [系統動作] 已接收前端中止信號，準備強制斬斷修復進程...")

@eel.expose
def run_ai_super_resolution(base64_str, mode='plate'):
    global sr_abort_flag
    sr_abort_flag = False

    def _run_sr():
        try:
            img_data, err = safe_base64_decode(base64_str)
            if err:
                eel.on_super_res_finished(None, f"❌ 影像編碼錯誤: {err}")()
                return
            np_arr = np.frombuffer(img_data, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            
            if img is None:
                eel.on_super_res_finished(None, "❌ 影像解碼失敗，請確認檔案格式是否正確。")()
                return

            fallback_triggered = False
            warning_msg = None

            if not check_and_download_sr_model():
                if sr_abort_flag:
                    return
                print(">>> [系統動作] NCNN 下載失敗或超時，進入備援流程！")
                warning_msg = "⚠️ [資安警告] 網路連線超時，AI已自動平滑降級為 OpenCV 備援鑑識模態！"
                fallback_triggered = True
            else:
                if sr_abort_flag:
                    return
                try:
                    print(f">>> [系統動作] 發動 NCNN 物理級 GPU 鑑識重建 ({mode} 模式)...")
                    model_name = "realesrgan-x4plus-anime" if mode == 'face' else "realesrgan-x4plus"
                    with tempfile.TemporaryDirectory(prefix='ag-sr-') as temp_dir:
                        temp_in = os.path.join(temp_dir, "input.png")
                        temp_out = os.path.join(temp_dir, "output.png")
                        if not cv2.imwrite(temp_in, img):
                            raise RuntimeError("無法建立 NCNN 暫存輸入影像")

                        cmd = [NCNN_EXE_PATH, "-i", temp_in, "-o", temp_out, "-n", model_name]
                        creation_flags = 0x08000000 if os.name == 'nt' else 0
                        process = subprocess.Popen(
                            cmd,
                            cwd=NCNN_MODEL_DIR,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=creation_flags,
                        )
                        while process.poll() is None:
                            if sr_abort_flag:
                                process.terminate()
                                try:
                                    process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=3)
                                print(">>> [系統動作] 鑑識重建已強制中止！")
                                return
                            time.sleep(0.1)

                        if process.returncode != 0 or not os.path.isfile(temp_out):
                            raise RuntimeError(f"NCNN 引擎回傳錯誤代碼 {process.returncode}")
                        result = cv2.imread(temp_out)
                        if result is None:
                            raise RuntimeError("NCNN 輸出影像無法解碼")
                    
                except Exception as ncnn_err:
                    print(f">>> [系統警告] NCNN 超解析執行失敗 ({ncnn_err})，自動切換至 OpenCV 備援流程！")
                    warning_msg = "⚠️ [備援提示] AI 核心引擎異常，已自動切換為高階銳化備援模態。"
                    fallback_triggered = True

            if fallback_triggered:
                if sr_abort_flag:
                    return
                print(">>> [系統動作] 發動第二軌備援：傳統最高階 Lanczos 內插法與 CLAHE 直方圖均衡化...")
                h, w = img.shape[:2]
                scaled = cv2.resize(img, (w * 4, h * 4), interpolation=cv2.INTER_LANCZOS4)
                
                ycrcb = cv2.cvtColor(scaled, cv2.COLOR_BGR2YCrCb)
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                ycrcb[:,:,0] = clahe.apply(ycrcb[:,:,0])
                result = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)

            if sr_abort_flag:
                return

            if mode == 'face' and fallback_triggered:
                print(">>> [系統動作] 備援人像五官模式後置處理：套用高階雙邊濾鏡 (Bilateral Filter)...")
                result = cv2.bilateralFilter(result, d=15, sigmaColor=100, sigmaSpace=100)

            # 通訊封包瘦身：傳送給前端預覽時使用 90 壓縮率，大幅降低 WebSocket 負載
            _, buffer = cv2.imencode('.jpg', result, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            res_b64 = base64.b64encode(buffer).decode('utf-8')
            eel.on_super_res_finished(res_b64, warning_msg)()
            
        except Exception as e:
            print(f"❌ [數位鑑識崩潰]：超解析引擎運算錯誤 ({e})")
            eel.on_super_res_finished(None, f"❌ 運算發生錯誤：{e}")()
            
    Thread(target=_run_sr, daemon=True).start()

@eel.expose
def save_enhanced_evidence(base64_str, mode='plate'):
    try:
        enhanced_dir = os.path.join(CONFIG.BASE_DIR, "enhanced_evidence")
        os.makedirs(enhanced_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "Enhanced_Face" if mode == 'face' else "Enhanced_Plate"
        filename = f"{prefix}_{timestamp}.png"
        filepath = os.path.join(enhanced_dir, filename)
        
        img_data, err = safe_base64_decode(base64_str)
        if err:
            print(f"❌ [數位鑑識崩潰]：修復檔案 Base64 解析失敗 ({err})")
            return False
        with open(filepath, 'wb') as f:
            f.write(img_data)
            
        print(f">>> [系統動作] 鑑識修復照片已儲存至: {filepath}")
        
        # Windows only: open folder and select file
        try:
            import subprocess
            subprocess.run(['explorer', '/select,', os.path.normpath(filepath)])
        except Exception:
            pass
            
        return True
    except Exception as e:
        print(f"❌ [數位鑑識崩潰]：修復檔案寫入失敗 ({e})")
        print("💡 [系統處置建議]：請確認 enhanced_evidence 目錄未被防毒軟體或隨身碟唯讀保護。")
        return False

def start_eel_app():
    web_dir = os.path.join(CONFIG.BASE_DIR, 'web')
    eel.init(web_dir)
    default_port = 8000
    max_attempts = 10

    for port_offset in range(max_attempts):
        current_port = default_port + port_offset
        try:
            print("==================================================")
            print("AG-MONITOR Forensic Player Engine Online!")
            print(f"http://localhost:{current_port}/index.html")
            print("==================================================")
            dlog(f"[BOOT] 嘗試於埠號 {current_port} 啟動 Eel GUI...")
            eel.start('index.html', size=(1280, 950), mode='edge', port=current_port, host='localhost')
            break
        except (OSError, Exception) as boot_err:
            dlog(f"[BOOT] 埠號 {current_port} 啟動失敗 ({boot_err})，準備切換備援埠號...")
            if port_offset == max_attempts - 1:
                print(f"[FATAL ERROR] 嘗試 {max_attempts} 個埠號 ({default_port}~{current_port}) 皆啟動失敗: {boot_err}")
                dlog(f"[BOOT] 致命錯誤: 所有備援埠號均無法綁定: {boot_err}")
                raise

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    os.makedirs(CONFIG.CAPTURES_DIR, exist_ok=True)
    try:
        start_eel_app()
    except Exception as e:
        print("Eel Boot Failed:", e)
