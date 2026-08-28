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
os.environ.setdefault(
    "MPLCONFIGDIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", ".matplotlib"),
)
os.makedirs(os.environ["YOLO_CONFIG_DIR"], exist_ok=True)
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
import cv2
import numpy as np
import time
import base64
import csv
import hashlib
import json
import math
import queue
import re
import subprocess
from datetime import datetime, timedelta
import threading
from threading import Thread
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
    APP_TITLE = "AG-MONITOR 智慧影像快篩系統"
    
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
    MOTION_GUIDE_MIN_AREA = 500
    MOTION_GUIDE_CONFIRMATION_FRAMES = 2
    MOTION_GUIDE_BASE_CONF = 0.35
    MOTION_GUIDE_COMPENSATED_CONF = 0.18
    CAPTURE_QUEUE_SIZE = 8
    CAPTURE_JPEG_QUALITY = 95
    MODEL_ALLOWLIST = {
        "yolov8n.pt", "yolov8s.pt",
        "yolo11n.pt", "yolo11s.pt",
        "yolo12n.pt", "yolo12s.pt",
    }
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
force_stop_requested = False
skip_video_path = None
model = None
current_model_name = None
list_lock = threading.Lock()
global_live_settings = {}


def calculate_sha256(file_path, chunk_size=CONFIG.HASH_CHUNK_SIZE):
    """以串流方式計算檔案雜湊，避免大型監視器影片占滿記憶體。"""
    digest = hashlib.sha256()
    with open(file_path, "rb") as file_obj:
        while chunk := file_obj.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()


def build_evidence_metadata(file_path):
    """僅供安全重新命名交易驗證使用，不由影片分析流程呼叫。"""
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


def resolve_model_name(settings, require_file=False):
    """只允許載入明確支援的本機模型權重，避免任意路徑注入。"""
    model_name = os.path.basename(str(settings.get("aiModel", "yolov8n.pt")))
    if model_name not in CONFIG.MODEL_ALLOWLIST:
        raise ValueError(f"不支援的 AI 模型: {model_name}")
    model_path = os.path.join(CONFIG.BASE_DIR, model_name)
    if require_file and not os.path.isfile(model_path):
        raise FileNotFoundError(f"找不到模型權重: {model_name}")
    return model_name


def tracker_occlusion_grace_seconds(tracker_mode):
    """ReID 必須維持足夠長的連續追蹤，才能跨越三秒以上遮蔽。"""
    if tracker_mode == "botsort_reid":
        return CONFIG.BOTSORT_REID_OCCLUSION_GRACE_SEC
    return CONFIG.BYTETRACK_OCCLUSION_GRACE_SEC


def resolve_live_processing_settings(live_settings, current_settings):
    """合併分析中的即時設定；未變更欄位沿用目前值。"""
    resolved = current_settings.copy()
    for key in (
        "confThresh", "skipSec", "classes", "inferenceSize", "riderAssist",
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


def format_capture_timecode(time_code):
    """將實際或相對時間碼轉為適合排序的檔名字串。"""
    matches = re.findall(r"(\d{2}):(\d{2}):(\d{2})", str(time_code))
    if not matches:
        return "00h00m00s"
    hours, minutes, seconds = matches[-1]
    return f"{hours}h{minutes}m{seconds}s"


def _safe_filename_part(value, fallback):
    # 保留中文等 Unicode 名稱，只替換 Windows 禁止字元與控制字元。
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", str(value)).strip(" ._")
    return cleaned or fallback


def _target_label(target):
    class_name = target.get("class_name")
    if not class_name:
        class_name = CONFIG.TARGET_CLASSES.get(target.get("cls_id"), "target")
    return str(class_name).title()


def draw_scene_annotations(frame, targets):
    """只在輸出副本上繪製同一場景的全部目標框。"""
    annotated = frame.copy()
    colors = {
        0: (88, 180, 255), 1: (109, 190, 112), 2: (235, 167, 70),
        3: (181, 108, 220), 5: (75, 190, 205), 7: (95, 120, 230),
    }
    frame_width = annotated.shape[1]
    thickness = 1 if frame_width < 1280 else 2 if frame_width < 2000 else 3
    font_scale = 0.45 if frame_width < 1280 else 0.60 if frame_width < 2000 else 0.80
    for target in targets:
        x1, y1, x2, y2 = map(int, target["xyxy"])
        class_id = int(target.get("cls_id", -1))
        color = colors.get(class_id, (0, 210, 255))
        track_id = int(target.get("tid", 0))
        confidence = float(target.get("conf", 0.0))
        label = f"ID:{track_id} {_target_label(target)} {confidence:.2f}"
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, thickness)
        cv2.putText(
            annotated, label, (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX,
            font_scale, color, max(1, thickness - 1), cv2.LINE_AA,
        )
    return annotated


def save_scene_screenshot(frame, output_dir, time_code, prefix_name, primary_target, targets, burn_annotations=False):
    """以原解析度寫入全景圖；不產生雜湊、清冊或浮水印。"""
    os.makedirs(output_dir, exist_ok=True)
    output_frame = draw_scene_annotations(frame, targets) if burn_annotations else frame
    target_id = int(primary_target.get("tid", 0))
    target_type = _safe_filename_part(_target_label(primary_target), "Target")
    safe_prefix = _safe_filename_part(prefix_name, "video")
    safe_time = format_capture_timecode(time_code)
    filename = f"{safe_prefix}_{safe_time}_ID{target_id}_{target_type}.jpg"
    final_path = os.path.join(output_dir, filename)
    success, encoded = cv2.imencode(
        ".jpg", output_frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), CONFIG.CAPTURE_JPEG_QUALITY],
    )
    if not success:
        return None

    stem, extension = os.path.splitext(final_path)
    collision_index = 0
    while True:
        actual_path = final_path if collision_index == 0 else f"{stem}_{collision_index}{extension}"
        try:
            with open(actual_path, "xb") as image_file:
                image_file.write(encoded.tobytes())
            return actual_path
        except FileExistsError:
            collision_index += 1


class CaptureWriter:
    """有界非同步全景截圖寫入器。"""

    _STOP = object()

    def __init__(self, maxsize=CONFIG.CAPTURE_QUEUE_SIZE, status_callback=None):
        self._queue = queue.Queue(maxsize=maxsize)
        self._status_callback = status_callback
        self._lock = threading.Lock()
        self._accepting = True
        self._stats = {"events": 0, "written": 0, "discarded": 0, "errors": 0, "queued": 0, "state": "normal"}
        self._thread = threading.Thread(target=self._worker, name="capture-writer", daemon=True)
        self._thread.start()

    def snapshot(self, state=None):
        with self._lock:
            result = dict(self._stats)
            result["queued"] = self._queue.qsize()
            if state is not None:
                result["state"] = state
                self._stats["state"] = state
        return result

    def _notify(self, state=None):
        stats = self.snapshot(state)
        if self._status_callback:
            try:
                self._status_callback(stats)
            except Exception:
                pass

    def enqueue(self, frame, output_dir, time_code, prefix_name, primary_target, targets, burn_annotations=False):
        if not self._accepting:
            return False
        event = {
            "frame": frame.copy(), "output_dir": output_dir, "time_code": time_code,
            "prefix_name": prefix_name, "primary_target": dict(primary_target),
            "targets": [dict(target) for target in targets],
            "burn_annotations": bool(burn_annotations),
        }
        while self._accepting:
            try:
                self._queue.put(event, timeout=0.1)
                with self._lock:
                    self._stats["events"] += 1
                self._notify("normal" if not self._queue.full() else "backpressure")
                return True
            except queue.Full:
                self._notify("backpressure")
        return False

    def finish(self, flush=True):
        self._accepting = False
        if flush:
            self._notify("flushing")
            self._queue.join()
        else:
            discarded = 0
            while True:
                try:
                    item = self._queue.get_nowait()
                    if item is not self._STOP:
                        discarded += 1
                    self._queue.task_done()
                except queue.Empty:
                    break
            with self._lock:
                self._stats["discarded"] += discarded
        self._queue.put(self._STOP)
        self._thread.join(timeout=5)
        self._notify("cancelled" if not flush else "completed")
        return self.snapshot()

    def _worker(self):
        while True:
            event = self._queue.get()
            try:
                if event is self._STOP:
                    return
                path = save_scene_screenshot(**event)
                with self._lock:
                    if path:
                        self._stats["written"] += 1
                    else:
                        self._stats["errors"] += 1
                self._notify()
            except Exception as error:
                dlog(f"[CAPTURE-WRITER] 寫入失敗: {error}")
                with self._lock:
                    self._stats["errors"] += 1
                self._notify("error")
            finally:
                self._queue.task_done()


class MotionGuideDetector:
    """以 ROI 內連續像素位移判斷是否啟用低門檻補償。"""

    def __init__(self, min_area=CONFIG.MOTION_GUIDE_MIN_AREA, confirmation_frames=CONFIG.MOTION_GUIDE_CONFIRMATION_FRAMES):
        self.min_area = int(min_area)
        self.confirmation_frames = int(confirmation_frames)
        self.previous_gray = None
        self.confirmations = 0

    def observe(self, frame, roi_poly=None):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self.previous_gray is None:
            self.previous_gray = gray
            return False
        difference = cv2.absdiff(self.previous_gray, gray)
        self.previous_gray = gray
        _, mask = cv2.threshold(difference, CONFIG.MOTION_THRESH, 255, cv2.THRESH_BINARY)
        if roi_poly is not None and len(roi_poly) >= 3:
            roi_mask = np.zeros_like(mask)
            cv2.fillPoly(roi_mask, [roi_poly], 255)
            mask = cv2.bitwise_and(mask, roi_mask)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        moving = any(cv2.contourArea(contour) >= self.min_area for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])
        self.confirmations = self.confirmations + 1 if moving else 0
        return self.confirmations >= self.confirmation_frames


# Player State
engine_mode = 'auto' # 'auto' or 'manual'
player_state = {
    'playing': False,
    'reverse': False,
    'speed': 1.0,
    'seek_req': None, # 0.0 ~ 100.0 percent
    'seek_revision': 0,
    'step_req': 0,    # frames to step
    'step_revision': 0,
    'manual_capture_req': False,
    'manual_capture_revision': 0,
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
    eel.updateStatus("狀態: 正在停止並完成截圖寫入...", "warn")


@eel.expose
def request_force_stop():
    global stop_requested, force_stop_requested
    force_stop_requested = True
    stop_requested = True
    eel.updateStatus("狀態: 正在強制停止，未寫入截圖將捨棄...", "danger")

@eel.expose
def update_live_setting(key, value):
    global global_live_settings
    old_value = global_live_settings.get(key)
    global_live_settings[key] = value
    return old_value != value

@eel.expose
def set_engine_mode(mode):
    global engine_mode, stop_requested
    if is_processing:
        return
    engine_mode = mode
    eel.appendLog(f"已切換至: {'全自動 AI 快篩' if mode == 'auto' else '即時人眼點視'}", "info")

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
        player_state['seek_revision'] = int(player_state.get('seek_revision', 0)) + 1

@eel.expose
def step_frame(steps):
    with player_lock:
        player_state['step_req'] = int(steps)
        player_state['step_revision'] = int(player_state.get('step_revision', 0)) + 1
        player_state['playing'] = False
        eel.updatePlayState(player_state['playing'], player_state['reverse'])

@eel.expose
def manual_capture():
    with player_lock:
        player_state['manual_capture_req'] = True
        player_state['manual_capture_revision'] = int(player_state.get('manual_capture_revision', 0)) + 1

@eel.expose
def start_processing(settings):
    global is_processing, stop_requested, force_stop_requested, global_live_settings, skip_video_path
    if is_processing:
        eel.updateStatus("狀態: 已有分析任務執行中", "warn")
        return {"success": False, "msg": "已有分析任務執行中"}
    if not video_queue:
        eel.updateStatus("狀態: 清單為空，無法開始", "danger")
        return {"success": False, "msg": "清單為空"}
    try:
        resolve_model_name(settings, require_file=True)
        inference_size = int(settings.get("inferenceSize", 960))
        if inference_size not in {640, 960, 1280}:
            raise ValueError(f"不支援的推論解析度: {inference_size}")
        resolve_tracker_config(settings)
    except (ValueError, FileNotFoundError) as error:
        eel.updateStatus(f"狀態: {error}", "danger")
        return {"success": False, "msg": str(error)}
    is_processing = True
    stop_requested = False
    force_stop_requested = False
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
        
    try:
        Thread(target=batch_processing_worker, args=(settings,), daemon=True).start()
    except Exception as error:
        is_processing = False
        eel.updateStatus(f"狀態: 分析工作無法啟動：{error}", "danger")
        return {"success": False, "msg": f"分析工作無法啟動：{error}"}
    return {"success": True}

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


def _configure_worker_context(process_engine_mode):
    """設定 Windows spawn 子程序無法從主程序繼承的必要狀態。"""
    global engine_mode
    engine_mode = process_engine_mode


def process_wrapper(
    video_path,
    video_name,
    settings,
    batch_output_dir,
    ui_queue,
    shared_state,
    model_name,
    process_engine_mode,
):
    import sys
    import threading
    import time
    from ultralytics import YOLO
    
    global eel, stop_requested, force_stop_requested, skip_video_path, player_state, global_live_settings
    global roi_points, scale_info, model
    
    class MockEel:
        def __getattr__(self, name):
            def wrapper(*args, **kwargs):
                ui_queue.put((name, args, kwargs))
                return lambda: None
            return wrapper
            
    eel = MockEel()
    
    stop_requested = False
    force_stop_requested = False
    skip_video_path = None
    player_state = shared_state.get('player_state', {})
    global_live_settings = shared_state.get('live_settings', {})
    roi_points = shared_state.get('roi_points', [])
    scale_info = shared_state.get('scale_info', None)
    # Windows multiprocessing 使用 spawn，子程序不會繼承主程序的全域狀態。
    _configure_worker_context(process_engine_mode)
    
    sub_sync_running = True
    def sub_sync_thread():
        global stop_requested, force_stop_requested, skip_video_path, player_state, global_live_settings, roi_points, scale_info
        while sub_sync_running:
            stop_requested = shared_state.get('stop_requested', False)
            force_stop_requested = shared_state.get('force_stop_requested', False)
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
        process_single_video(video_path, video_name, settings, batch_output_dir, shared_state)
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
    import traceback
    import gc

    sync_running = False
    manager = None
    summary = {"events": 0, "written": 0, "discarded": 0, "errors": 0, "forced": False, "outputDir": CONFIG.CAPTURES_DIR}
    try:
        model_name = resolve_model_name(settings, require_file=True)
        with list_lock:
            q = list(video_queue)
        total_v = len(q)
        single_folder = settings.get("singleFolder", False)
        batch_output_dir = None
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        if single_folder:
            batch_output_dir = os.path.join(CONFIG.CAPTURES_DIR, f"Batch_{timestamp_str}")
            os.makedirs(batch_output_dir, exist_ok=True)
            summary["outputDir"] = batch_output_dir

        manager = multiprocessing.Manager()
        ui_queue = manager.Queue()
        shared_state = manager.dict({
            'stop_requested': False,
            'force_stop_requested': False,
            'skip_video_path': None,
            'player_state': player_state,
            'live_settings': global_live_settings,
            'roi_points': roi_points,
            'scale_info': scale_info,
            'writer_stats': {},
        })

        sync_running = True
        def state_sync_thread():
            while sync_running:
                try:
                    shared_state['stop_requested'] = stop_requested
                    shared_state['force_stop_requested'] = force_stop_requested
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
            global skip_video_path
            skip_video_path = None
            shared_state['skip_video_path'] = None
            shared_state['writer_stats'] = {}
            v_name = os.path.basename(video_path)
            eel.updateStatus(f"狀態: 正在分析 ({current_idx + 1}/{total_v}) {v_name}", "ok")
            eel.appendLog(f"開始載入影片: {v_name}", "info")
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
                )
            )
            p.start()

            while p.is_alive():
                if force_stop_requested:
                    summary["forced"] = True
                    try:
                        shared_state['force_stop_requested'] = True
                        shared_state['stop_requested'] = True
                    except Exception:
                        pass
                    p.terminate()
                    p.join(timeout=5)
                    break
                if stop_requested:
                    try:
                        shared_state['stop_requested'] = True
                    except Exception:
                        pass
                p.join(timeout=0.25)

            writer_stats = dict(shared_state.get('writer_stats', {}))
            if force_stop_requested:
                pending = max(0, int(writer_stats.get("events", 0)) - int(writer_stats.get("written", 0)) - int(writer_stats.get("errors", 0)))
                writer_stats["discarded"] = int(writer_stats.get("discarded", 0)) + pending
            for key in ("events", "written", "discarded", "errors"):
                summary[key] += int(writer_stats.get(key, 0))

            requested_path = None
            try:
                requested_path = shared_state.get('skip_video_path', None)
            except Exception:
                pass
            if p.exitcode != 0 and not force_stop_requested:
                summary["errors"] += 1
                eel.appendLog(f"{v_name} 分析程序異常結束（代碼 {p.exitcode}）", "error")
            gc.collect()

            skip_path = requested_path
            if skip_path is not None:
                try:
                    current_idx = q.index(skip_path)
                except ValueError:
                    current_idx += 1
            else:
                current_idx += 1

        if force_stop_requested:
            eel.updateStatus("狀態: 已強制停止，未寫入截圖已捨棄", "danger")
            eel.appendLog(f"強制停止完成，捨棄 {summary['discarded']} 張未寫入截圖", "warn")
        elif stop_requested:
            eel.updateStatus("狀態: 已停止，截圖佇列已完成寫入", "warn")
            eel.appendLog("任務已安全停止", "warn")
        else:
            eel.updateProgress(100, "")
            eel.updateStatus("狀態: 全部完成！", "ok")
            eel.appendLog("所有佇列影片處理完成", "success")
    except Exception as e:
        err_msg = traceback.format_exc()
        summary["errors"] += 1
        eel.updateStatus("系統崩潰", "danger")
        eel.appendLog(f"系統崩潰: {str(e)}", "error")
        print(err_msg)
    finally:
        sync_running = False
        is_processing = False
        eel.processingFinished(summary)
        if manager is not None:
            try:
                manager.shutdown()
            except Exception:
                pass

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

def process_single_video(video_path, video_name, settings, batch_output_dir=None, shared_state=None):
    global stop_requested, real_roi_poly, skip_video_path
    capture_writer = None
    writer_finished = False
    
    clean_v_name = os.path.splitext(video_name)[0]
    clean_v_name = "".join([c for c in clean_v_name if c.isalnum() or c in (".", "_", "-", "[", "]")]).rstrip()
    if batch_output_dir:
        output_dir = batch_output_dir
    else:
        output_dir = os.path.join(CONFIG.CAPTURES_DIR, clean_v_name)
    os.makedirs(output_dir, exist_ok=True)

    def update_writer_status(stats):
        if shared_state is not None:
            try:
                shared_state['writer_stats'] = stats
            except Exception:
                pass
        eel.updateWriterStatus(stats)()

    capture_writer = CaptureWriter(status_callback=update_writer_status)
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
        headless = settings.get('executionMode', 'preview') == 'headless'
        burn_annotations = bool(settings.get('burnAnnotations', False))
        skip_sec = float(settings.get('skipSec', 0.20))
        inference_size = int(settings.get('inferenceSize', 960))
        rider_assist = bool(settings.get('riderAssist', True))
        motion_guide = MotionGuideDetector()
        static_skip_step = max(1, int(fps * skip_sec))
        
        track_states, id_alias_map = {}, {}
        target_frame_idx, decoded_frame_idx, is_dynamic_mode = 0, -1, False
        dynamic_lock_until, no_target_frames = 0, 0
        
        is_raw_stream = (stream.duration is None)
        stream.codec_context.skip_frame = 'DEFAULT'
        frame_iter = container.decode(stream)
        current_frame_cache = None
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
                if should_trigger_decoder_deadlock(phase, elapsed):
                    deadlock_detected = True
                    dlog(f"[WATCHDOG] 🚨 解碼階段 {elapsed:.1f}s 無進展，觸發熔斷")
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
        last_seek_revision = -1
        last_step_revision = -1
        last_capture_revision = -1
        
        while True:
            if stop_requested or skip_video_path is not None:
                break
            if deadlock_detected:
                raise RuntimeError("Watchdog triggered deadlock interruption")
            
            with player_lock:
                seek_revision = int(player_state.get('seek_revision', 0))
                step_revision = int(player_state.get('step_revision', 0))
                capture_revision = int(player_state.get('manual_capture_revision', 0))
                req_seek = player_state['seek_req'] if seek_revision != last_seek_revision else None
                req_step = player_state['step_req'] if step_revision != last_step_revision else 0
                is_play = player_state['playing']
                is_rev = player_state['reverse']
                p_speed = player_state['speed']
                req_capture = player_state['manual_capture_req'] if capture_revision != last_capture_revision else False
                last_seek_revision = seek_revision
                last_step_revision = step_revision
                last_capture_revision = capture_revision
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

                if target_idx != decoded_frame_idx or current_frame_cache is None:
                    frame = get_frame(target_idx)
                    if frame is None and is_play:
                        break
                    if frame is not None:
                        decoded_frame_idx = last_received_idx
                        target_idx = decoded_frame_idx
                        current_frame_cache = frame
                else:
                    frame = current_frame_cache

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
                        manual_target = {"tid": 0, "class_name": "Manual", "cls_id": -1, "conf": 1.0, "xyxy": (0, 0, 0, 0)}
                        if capture_writer.enqueue(
                            frame, output_dir, time_code_str, clean_v_name,
                            manual_target, [], False,
                        ):
                            eel.appendLog(f"[{time_code_str}] 📸 手動全景快門已排入寫入佇列", "success")

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
                    'skipSec': skip_sec,
                    'classes': class_vars,
                    'inferenceSize': inference_size,
                    'riderAssist': rider_assist,
                })
                conf_thresh = live_values['confThresh']
                skip_sec = live_values['skipSec']
                class_vars = live_values['classes']
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
                            ms = (target_frame_idx / fps) * 1000
                            t_str = format_timecode(ms, start_time_dt)
                            if not headless and _now - last_ui_update > 0.5:
                                push_frame_to_ui(frame, [], real_roi_poly, t_str)
                                last_ui_update = _now
                            if _now - last_progress_update > 0.2:
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
                motion_guided = motion_guide.observe(frame, real_roi_poly)
                effective_conf = conf_thresh
                if motion_guided and conf_thresh >= CONFIG.MOTION_GUIDE_BASE_CONF:
                    effective_conf = CONFIG.MOTION_GUIDE_COMPENSATED_CONF
                track_kwargs = {'conf': effective_conf, 'verbose': False}
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
                        if conf < effective_conf:
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

                        track_confirmed = box.id is not None
                        raw_tid = int(box.id[0]) if track_confirmed else 0
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
                            valid_targets.append({
                                'tid': tid, 'raw_tid': raw_tid, 'track_confirmed': track_confirmed,
                                'conf': conf, 'cls_id': target_cls, 'xyxy': (x1, y1, x2, y2),
                            })

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
                        
                        # 重置 YOLO 追蹤器時會從頭分配 Track ID；舊世代狀態不可沿用，
                        # 否則新車可能取得舊 ID 而被誤判為已截圖。
                        track_states.clear()
                        id_alias_map.clear()
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
                            milliseconds, track_states, reidentify_grace_msec,
                        )
                        if is_raw_stream:
                            target_frame_idx += 1  # Raw 流靜態模式：每幀前進 1
                        else:
                            target_frame_idx += static_skip_step
                        # 靜態空景模式：每 0.5 秒仍推送一幀到 UI，確保預覽畫面不凍結
                        _now = time.time()
                        if not headless and _now - last_ui_update > 0.5:
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
                            milliseconds, track_states, reidentify_grace_msec,
                        )
                        
                        # 退出動態模式：推送最後一幀讓使用者看到
                        _now = time.time()
                        if not headless and _now - last_ui_update > 0.5:
                            frame_h, frame_w = annotated_frame.shape[:2]
                            osd_text = f"AG-MONITOR | {time_code_str}"
                            (tw, th), _ = cv2.getTextSize(osd_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                            cv2.rectangle(annotated_frame, (5, frame_h - th - 15), (5 + tw + 10, frame_h - 5), (0, 0, 0), -1)
                            cv2.putText(annotated_frame, osd_text, (10, frame_h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                            push_frame_to_ui(frame, [], real_roi_poly, time_code_str)
                            last_ui_update = _now
                        continue


                # ---------------- Track States Management ----------------
                new_scene_targets = []
                for target in valid_targets:
                    if not target.get('track_confirmed', False):
                        continue
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
                                'last_seen_msec': milliseconds,
                                'last_centroid': (cx, cy),
                                'start_box_size': (w, h),
                                'last_box_size': (w, h),
                                'start_centroid': (cx, cy),
                                'is_moving': False,
                                'motion_confirmations': 0,
                                'entry_captured': False
                            }
                            
                    if tid in track_states:
                        target['tid'] = tid
                        state = track_states[tid]
                        state['last_seen_msec'] = milliseconds
                        state['last_centroid'] = (cx, cy)
                        state['last_box_size'] = (w, h)
                        
                        if conf > state['best_conf']:
                            state['best_conf'] = conf
                            
                        if not state['is_moving'] and record_motion_observation(state, (cx, cy), (w, h)):
                            dlog(f"[DEBUG-MOVE] ID:{tid} {state['class_name']} 已確認移動")
                            if not headless:
                                eel.appendLog(f"[{time_code_str}] ID:{tid} {state['class_name']} 已確認移動", "info")
                        state['prev_centroid'] = (cx, cy)
                                
                        if state['is_moving'] and not state['entry_captured']:
                            state['entry_captured'] = True
                            new_scene_targets.append(target.copy())

                if new_scene_targets:
                    primary_target = new_scene_targets[0]
                    queued = capture_writer.enqueue(
                        frame, output_dir, time_code_str, clean_v_name,
                        primary_target, valid_targets, burn_annotations,
                    )
                    if queued and not headless:
                        eel.appendLog(
                            f"[{time_code_str}] 全景事件已排入：ID:{primary_target['tid']} {_target_label(primary_target)}",
                            "success",
                        )

                if engine_mode == 'auto' and is_dynamic_mode and not headless:
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

                if not headless:
                    push_frame_to_ui(frame, valid_targets, real_roi_poly, time_code_str)
                    
                # 每幀釋放已離場的追蹤狀態，讓同一 ID 日後重新進場時可再次建立事件。
                _run_grace_period_gc(
                    milliseconds, track_states, reidentify_grace_msec,
                )

                target_frame_idx += 1 

        if engine_mode == 'auto':
            track_states.clear()
        writer_stats = capture_writer.finish(flush=not force_stop_requested)
        writer_finished = True
        if shared_state is not None:
            shared_state['writer_stats'] = writer_stats
        decode_thread_running = False
        set_decoder_phase("stopped", made_progress=True)
        container.close()

    except Exception as e:
        err_msg = traceback.format_exc()
        eel.appendLog(f"[{video_name}] 解碼毀損診斷: {str(e)}", "error")
        eel.appendLog("處置建議: 可能是編碼異常或檔案殘缺，請重新提取原始檔案。", "warn")
        print(f"Exception for {video_name}:\n{err_msg}")
        raise
    finally:
        if capture_writer is not None and not writer_finished:
            try:
                writer_stats = capture_writer.finish(flush=not force_stop_requested)
                if shared_state is not None:
                    shared_state['writer_stats'] = writer_stats
            except Exception as writer_error:
                dlog(f"[CAPTURE-WRITER] 收尾失敗: {writer_error}")
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

def _run_grace_period_gc(curr_msec, track_states, reidentify_grace_msec=1500):
    """釋放已離場目標；場景截圖在新移動目標確認時已完成排程。"""
    expired_ids = [
        tid for tid, state in track_states.items()
        if (curr_msec - state['last_seen_msec']) > reidentify_grace_msec
    ]
    for tid in expired_ids:
        del track_states[tid]

def start_eel_app():
    web_dir = os.path.join(CONFIG.BASE_DIR, 'web')
    eel.init(web_dir)
    default_port = 8000
    max_attempts = 10

    for port_offset in range(max_attempts):
        current_port = default_port + port_offset
        try:
            print("==================================================")
            print("AG-MONITOR Smart Video Screening Engine Online!")
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
