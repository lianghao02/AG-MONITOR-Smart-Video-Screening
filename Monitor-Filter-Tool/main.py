import os
os.environ["OPENCV_FFMPEG_LOG_LEVEL"] = "-1"
os.environ["PYAV_LOGGING"] = "off"
os.environ["YOLO_VERBOSE"] = "False"
os.environ["YOLO_OFFLINE"] = "True"
import cv2
import numpy as np
import time
import base64
import math
import re
import urllib.request
import zipfile
import subprocess
from datetime import datetime, timedelta
import threading
from threading import Thread
from PIL import Image, ImageDraw, ImageFont
import eel
import tkinter as tk
from tkinter import filedialog
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

class CONFIG:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CAPTURES_DIR = os.path.join(BASE_DIR, "captures")
    APP_TITLE = "AG-MONITOR 科技偵查戰術播放器"
    
    SMART_SKIP_SEC = 3.0   
    MOTION_THRESH = 25     
    MOTION_MIN_AREA = 500  

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

@eel.expose
def add_videos_dialog():
    global video_queue
    root = tk.Tk()
    root.attributes("-topmost", True)
    root.withdraw()
    files = filedialog.askopenfilenames(
        title="選擇視訊檔案",
        filetypes=[("視訊檔案", "*.mp4 *.avi *.mkv *.mov *.m4v *.h264 *.h265 *.264 *.265 *.dav *.flv *.ts *.wmv")]
    )
    root.destroy()
    
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
    root = tk.Tk()
    root.attributes("-topmost", True)
    root.withdraw()
    folder_path = filedialog.askdirectory(title="選擇包含影片的資料夾 (將自動掃描所有子資料夾)")
    root.destroy()
    
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

@eel.expose
def batch_rename_videos(keep_old_name=True):
    global video_queue
    with list_lock:
        if is_processing:
            return {"success": False, "msg": "分析中無法重新命名"}
        
        new_queue = []
        count = 0
        total = len(video_queue)
        
        for idx, path in enumerate(video_queue):
            if idx % 50 == 0:
                eel.updateRenameProgress(idx, total)()
                eel.sleep(0.001) # let UI update
                
            dir_name = os.path.dirname(path)
            base_name = os.path.basename(path)
            
            # Auto-repair logic for files with trapped extensions e.g. foo_[bar.mp4] -> foo_[bar].mp4
            if base_name.endswith("]"):
                name_no_ext, broken_ext = os.path.splitext(base_name)
                if broken_ext.endswith("]"):
                    real_ext = broken_ext[:-1] # remove ]
                    repaired_name = name_no_ext + "]" + real_ext
                    repaired_path = os.path.join(dir_name, repaired_name)
                    try:
                        os.rename(path, repaired_path)
                        new_queue.append(repaired_path)
                        count += 1
                        continue
                    except Exception:
                        pass
            
            dt = parse_start_time(base_name)
            if not dt:
                new_queue.append(path)
                continue
                
            time_str = dt.strftime("%Y%m%d_%H%M%S")
            
            # Check if it already matches target format to skip safely
            if keep_old_name and base_name.startswith(f"{time_str}_["):
                new_queue.append(path)
                continue
            if not keep_old_name and base_name.startswith(time_str) and "_[" not in base_name:
                new_queue.append(path)
                continue
                
            if keep_old_name:
                name_no_ext, ext = os.path.splitext(base_name)
                # Prevent nested brackets by removing previous timestamp prefix if it exists
                if re.match(r'^20\d{6}_\d{6}_\[', name_no_ext) and name_no_ext.endswith("]"):
                    name_no_ext = name_no_ext[17:-1]
                new_name = f"{time_str}_[{name_no_ext}]{ext}"
            else:
                ext = os.path.splitext(base_name)[1]
                new_name = f"{time_str}{ext}"
                
            new_path = os.path.join(dir_name, new_name)
            
            if not keep_old_name and os.path.exists(new_path) and new_path != path:
                counter = 1
                ext = os.path.splitext(base_name)[1]
                while os.path.exists(new_path):
                    new_name = f"{time_str}_{counter}{ext}"
                    new_path = os.path.join(dir_name, new_name)
                    counter += 1

            try:
                os.rename(path, new_path)
                new_queue.append(new_path)
                count += 1
            except Exception as e:
                print(f"Rename failed for {path}: {e}")
                new_queue.append(path)
                
        eel.updateRenameProgress(total, total)()
        video_queue = new_queue
        return {"success": True, "count": count, "new_paths": new_queue}

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
    global_live_settings[key] = value

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

def process_wrapper(video_path, video_name, settings, batch_output_dir, ui_queue, shared_state, model_name):
    import sys
    import threading
    import time
    from ultralytics import YOLO
    
    global eel, stop_requested, skip_video_path, player_state, global_live_settings, roi_points, scale_info, model
    
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
        write_report(f"極速背景處理: {'開啟' if settings.get('fastMode', True) else '關閉'}")
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
                shared_state['stop_requested'] = stop_requested
                shared_state['skip_video_path'] = skip_video_path
                shared_state['player_state'] = player_state
                shared_state['live_settings'] = global_live_settings
                shared_state['roi_points'] = roi_points
                shared_state['scale_info'] = scale_info
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
            write_report(f"▶ 開始分析影片: {v_name}")
            
            p = multiprocessing.Process(
                target=process_wrapper, 
                args=(video_path, v_name, settings, batch_output_dir, ui_queue, shared_state, model_name)
            )
            p.start()
            
            # Watchdog loop: wait for process to finish or crash, while staying responsive to stop requests
            while p.is_alive():
                if stop_requested:
                    shared_state['stop_requested'] = True
                p.join(timeout=0.5)
            
            if p.exitcode != 0:
                write_report(f"❌ 發生致命崩潰錯誤 (看門狗已介入)\n")
                
            write_report(f"✅ 完成分析影片: {v_name}\n")
            gc.collect()
            
            # Check if user requested to skip to a specific video during this process
            skip_path = shared_state['skip_video_path']
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
        else:
            eel.updateProgress(100, "")
            eel.updateStatus("狀態: 全部完成！", "ok")
            eel.appendLog("所有佇列影片處理完成", "success")

    except Exception as e:
        err_msg = traceback.format_exc()
        eel.updateStatus("系統崩潰", "danger")
        eel.appendLog(f"系統崩潰: {str(e)}", "error")
        print(err_msg)
    finally:
        sync_running = False
        is_processing = False
        eel.processingFinished()

def parse_start_time(filename):
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
        skip_sec = float(settings.get('skipSec', 0.20))
        static_skip_step = int(fps * skip_sec)
        
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

        def decoding_worker():
            nonlocal container, stream
            frame_iter = container.decode(stream)
            local_decoded_idx = -1
            
            while decode_thread_running:
                try:
                    cmd = command_queue.get_nowait()
                    if cmd['action'] == 'seek':
                        target_idx = cmd['target']
                        pts = int(target_idx / fps / float(stream.time_base))
                        try:
                            container.seek(pts, stream=stream, backward=True)
                            frame_iter = container.decode(stream)
                            local_decoded_idx = -1
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
                    f = next(frame_iter)
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
                            frame_queue.put({'idx': local_decoded_idx, 'frame': bgr_frame}, timeout=0.05)
                            break
                        except queue.Full:
                            continue
                except StopIteration:
                    frame_queue.put({'idx': -1, 'frame': None})
                    break
                except Exception as e:
                    time.sleep(0.01)

        decoding_thread = threading.Thread(target=decoding_worker, daemon=True)
        decoding_thread.start()

        last_received_idx = -1

        def get_frame(target_idx):
            nonlocal last_received_idx
            
            # 若倒退或是跳躍過大，發送 seek 指令
            if target_idx < last_received_idx or (target_idx - last_received_idx) > 30:
                command_queue.put({'action': 'seek', 'target': target_idx})
                
            while decode_thread_running:
                try:
                    item = frame_queue.get(timeout=0.1)
                    if item['idx'] == -1:
                        return None
                    last_received_idx = item['idx']
                    if last_received_idx >= target_idx:
                        return item['frame']
                except queue.Empty:
                    if not decode_thread_running:
                        return None
                    continue
            return None

        last_ui_update, last_pushed_idx = time.time(), -1
        
        while True:
            if stop_requested or skip_video_path is not None:
                break
            
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
                        cv2.polylines(annotated, [real_roi_poly], True, (0, 255, 0), 2)
                    
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
                conf_thresh = global_live_settings.get('confThresh', conf_thresh)
                fast_mode = global_live_settings.get('fastMode', fast_mode)
                skip_sec = float(global_live_settings.get('skipSec', skip_sec))
                static_skip_step = int(fps * skip_sec)
                
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

                # ---------------- YOLO Detection ----------------
                if is_dynamic_mode:
                    results = model.track(frame, persist=True, tracker="bytetrack.yaml", verbose=False, conf=conf_thresh)[0]
                else:
                    results = model.predict(frame, verbose=False, conf=conf_thresh)[0]
                    
                boxes = results.boxes
                annotated_frame = frame.copy()
                valid_targets = []

                if boxes is not None:
                    for box in boxes:
                        conf = float(box.conf[0])
                        if conf < conf_thresh:
                            continue
                        cls_id = int(box.cls[0])
                        if cls_id not in CONFIG.TARGET_CLASSES or not class_vars.get(str(cls_id), True):
                            continue

                        raw_tid = int(box.id[0]) if box.id is not None else 0
                        tid = id_alias_map.get(raw_tid, raw_tid) if raw_tid != 0 else 0
                        xyxy = box.xyxy[0].cpu().numpy()
                        x1, y1, x2, y2 = map(int, xyxy)
                        centroid = ((x1 + x2) / 2, y2)

                        inside_roi = True
                        if real_roi_poly is not None:
                            dist = cv2.pointPolygonTest(real_roi_poly, centroid, False)
                            inside_roi = dist >= 0

                        if inside_roi:
                            valid_targets.append({'tid': tid, 'raw_tid': raw_tid, 'conf': conf, 'cls_id': cls_id, 'xyxy': (x1, y1, x2, y2)})

                # ---------------- Filter Overlapping Targets ----------------
                drop_indices = set()
                for i in range(len(valid_targets)):
                    if i in drop_indices: continue
                    t1 = valid_targets[i]
                    x1_1, y1_1, x2_1, y2_1 = t1['xyxy']
                    area1 = (x2_1 - x1_1) * (y2_1 - y1_1)
                    
                    for j in range(i + 1, len(valid_targets)):
                        if j in drop_indices: continue
                        t2 = valid_targets[j]
                        x1_2, y1_2, x2_2, y2_2 = t2['xyxy']
                        area2 = (x2_2 - x1_2) * (y2_2 - y1_2)
                        
                        ix1, iy1 = max(x1_1, x1_2), max(y1_1, y1_2)
                        ix2, iy2 = min(x2_1, x2_2), min(y2_1, y2_2)
                        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
                        inter_area = iw * ih
                        if inter_area > 0:
                            is_rider = (t1['cls_id'] == 0 and t2['cls_id'] in [1, 3]) or (t1['cls_id'] in [1, 3] and t2['cls_id'] == 0)
                            threshold = 0.15 if is_rider else 0.6
                            
                            if inter_area / min(area1, area2) > threshold:
                                # 優先保留車輛 (cls_id != 0)，並以信心度為輔助判斷
                                score1 = 10 if t1['cls_id'] != 0 else 0
                                score2 = 10 if t2['cls_id'] != 0 else 0
                                score1 += t1['conf']
                                score2 += t2['conf']
                                
                                if score1 > score2:
                                    drop_indices.add(j)
                                    t1['xyxy'] = (min(x1_1, x1_2), min(y1_1, y1_2), max(x2_1, x2_2), max(y2_1, y2_2))
                                else:
                                    drop_indices.add(i)
                                    t2['xyxy'] = (min(x1_1, x1_2), min(y1_1, y1_2), max(x2_1, x2_2), max(y2_1, y2_2))
                                    break
                                
                final_targets = []
                for i, t in enumerate(valid_targets):
                    if i not in drop_indices:
                        final_targets.append(t)
                        x1, y1, x2, y2 = t['xyxy']
                        cls_id, tid, conf = t['cls_id'], t['tid'], t['conf']
                        cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (0, 0, 255), 1)
                        cv2.putText(annotated_frame, f"ID:{tid} {CONFIG.TARGET_CLASSES[cls_id]} {conf:.2f}",
                            (x1, max(15, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
                
                valid_targets = final_targets

                if real_roi_poly is not None:
                    cv2.polylines(annotated_frame, [real_roi_poly], True, (0, 255, 0), 1)

                # ---------------- Motion & Skip Logic ----------------
                motion_detected = len(valid_targets) > 0

                if motion_detected:
                    no_target_frames = 0
                else:
                    no_target_frames += 1

                if is_dynamic_mode:
                    if no_target_frames < int(fps * 1.5):
                        motion_detected = True

                if is_dynamic_mode and target_frame_idx < dynamic_lock_until:
                    motion_detected = True

                if not is_dynamic_mode:
                    if motion_detected:
                        dynamic_lock_until = target_frame_idx
                        old_target = target_frame_idx
                        target_frame_idx = max(0, target_frame_idx - static_skip_step)
                        is_dynamic_mode = True
                        
                        if is_raw_stream:
                            # Raw 流：絕對不能 seek，直接保留現有迭代器，重置 target 計數器即可
                            target_frame_idx = old_target
                            dlog(f"[DEBUG-SKIP] Raw stream: skipping seek, staying at frame {target_frame_idx}")
                        else:
                            command_queue.put({'action': 'seek', 'target': target_frame_idx})
                        continue
                    else:
                        _run_grace_period_gc(milliseconds, track_states, capture_mode, output_dir, clean_v_name)
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
                        continue
                else:
                    if not motion_detected and target_frame_idx >= dynamic_lock_until:
                        is_dynamic_mode = False
                        raw_skip_counter = 0  # 重新進入靜態模式，重置跳過計數器
                        if is_raw_stream:
                            target_frame_idx += 1
                        else:
                            target_frame_idx += static_skip_step
                        _run_grace_period_gc(milliseconds, track_states, capture_mode, output_dir, clean_v_name)
                        
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
                            if 0 < time_diff <= 1500:
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
                                'best_frame': annotated_frame.copy(),
                                'best_timecode': time_code_str,
                                'best_summary': [f"{summary_str}({conf:.2f} Peak)"],
                                'last_frame': annotated_frame.copy(),
                                'last_timecode': time_code_str,
                                'last_seen_msec': milliseconds,
                                'last_continuous_capture_msec': milliseconds,
                                'last_centroid': (cx, cy),
                                'start_box_size': (w, h),
                                'last_box_size': (w, h),
                                'start_centroid': (cx, cy),
                                'is_moving': False,
                                'entry_captured': False
                            }
                            
                    if tid in track_states:
                        state = track_states[tid]
                        state['last_seen_msec'] = milliseconds
                        state['last_frame'] = annotated_frame.copy()
                        state['last_timecode'] = time_code_str
                        state['last_centroid'] = (cx, cy)
                        state['last_box_size'] = (w, h)
                        
                        if conf > state['best_conf']:
                            state['best_conf'] = conf
                            state['best_frame'] = annotated_frame.copy()
                            state['best_timecode'] = time_code_str
                            state['best_summary'] = [f"{summary_str}({conf:.2f} Peak)"]
                            
                        if not state['is_moving']:
                            # 比較與初始位置的總位移，避免 YOLO 邊界框的單幀抖動被誤判為移動
                            start_cx, start_cy = state['start_centroid']
                            dist = math.hypot(cx - start_cx, cy - start_cy)
                            sw, sh = state['start_box_size']
                            size_diff = max(abs(w - sw), abs(h - sh))
                            
                            # 必須與初始狀態相差 12 像素，或是大小改變 15 像素，才確認為真實移動
                            if dist > 12 or size_diff > 15:
                                state['is_moving'] = True
                                dlog(f"[DEBUG-MOVE] ID:{tid} {state['class_name']} 移動! dist={dist:.1f} size_diff={size_diff}")
                                eel.appendLog(f"[{time_code_str}] ID:{tid} {state['class_name']} 偵測到移動 (累積位移:{dist:.1f}px, 形變:{size_diff}px)", "info")
                        state['prev_centroid'] = (cx, cy)
                                
                        if state['is_moving'] and not state['entry_captured']:
                            state['entry_captured'] = True
                            dlog(f"[DEBUG-CAPTURE] 準備截圖! mode={capture_mode} output_dir={output_dir}")
                            if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "事件起訖模式"]:
                                save_legal_screenshot(state['start_frame'], output_dir, state['start_timecode'], [f"ID:{tid} {state['class_name']}(Entry)"], clean_v_name)
                                eel.appendLog(f"[{state['start_timecode']}] 擷取 ID:{tid} {state['class_name']}(Entry)", "success")
                            elif capture_mode == "持續追蹤模式 (預設)":
                                save_legal_screenshot(state['start_frame'], output_dir, state['start_timecode'], [f"ID:{tid} {state['class_name']}(Track-Entry)"], clean_v_name)
                                eel.appendLog(f"[{state['start_timecode']}] 擷取 ID:{tid} {state['class_name']}(Track-Entry)", "success")
                    
                    if capture_mode == "持續追蹤模式 (預設)":
                        state = track_states[tid]
                        if state['is_moving']:
                            if (milliseconds - state['last_continuous_capture_msec']) >= 3000:
                                state['last_continuous_capture_msec'] = milliseconds
                                save_legal_screenshot(annotated_frame, output_dir, time_code_str, [f"{summary_str}(Track)"], clean_v_name)
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
                filter_stationary = settings.get('filterStationary', True)
                _run_grace_period_gc(milliseconds, track_states, capture_mode, output_dir, clean_v_name, filter_stationary)

                target_frame_idx += 1 

        if engine_mode == 'auto':
            filter_stationary = settings.get('filterStationary', True)
            _flush_all_track_states(track_states, capture_mode, output_dir, clean_v_name, filter_stationary)
        container.close()

    except Exception as e:
        err_msg = traceback.format_exc()
        write_report(f"  ❌ 影片解碼異常 ({video_name}): {str(e)}")
        eel.appendLog(f"[{video_name}] 解碼毀損診斷: {str(e)}", "error")
        eel.appendLog("處置建議: 可能是編碼異常或檔案殘缺，請重新提取原始檔案。", "warn")
        print(f"Exception for {video_name}:\n{err_msg}")
    finally:
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

def _run_grace_period_gc(curr_msec, track_states, capture_mode, output_dir, prefix_name, filter_stationary=True):
    expired_ids = []
    for tid, state in track_states.items():
        if (curr_msec - state['last_seen_msec']) > 1500:
            expired_ids.append(tid)
    for tid in expired_ids:
        state = track_states[tid]
        if filter_stationary and not state['is_moving']:
            pass
        else:
            if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "單次最清晰模式 (推薦)"]:
                if state['best_frame'] is not None:
                    save_legal_screenshot(state['best_frame'], output_dir, state['best_timecode'], state['best_summary'], prefix_name)
                    eel.appendLog(f"[{state['best_timecode']}] 擷取 {state['best_summary'][0]}", "success")
            elif capture_mode == "事件起訖模式":
                if state['last_frame'] is not None:
                    save_legal_screenshot(state['last_frame'], output_dir, state['last_timecode'], [f"ID:{tid} {state['class_name']}(Exit)"], prefix_name)
                    eel.appendLog(f"[{state['last_timecode']}] 擷取 ID:{tid} {state['class_name']}(Exit)", "success")
        del track_states[tid]

def _flush_all_track_states(track_states, capture_mode, output_dir, prefix_name, filter_stationary=True):
    for tid, state in track_states.items():
        if filter_stationary and not state['is_moving']:
            continue
        if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "單次最清晰模式 (推薦)"]:
            if state['best_frame'] is not None:
                save_legal_screenshot(state['best_frame'], output_dir, state['best_timecode'], state['best_summary'], prefix_name)
                eel.appendLog(f"[{state['best_timecode']}] 擷取 {state['best_summary'][0]}", "success")
        elif capture_mode == "事件起訖模式":
            if state['last_frame'] is not None:
                save_legal_screenshot(state['last_frame'], output_dir, state['last_timecode'], [f"ID:{tid} {state['class_name']}(Exit)"], prefix_name)
                eel.appendLog(f"[{state['last_timecode']}] 擷取 ID:{tid} {state['class_name']}(Exit)", "success")
    track_states.clear()

def save_legal_screenshot(frame, output_dir, time_code, objects_list, prefix_name="evidence"):
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
            with open(final_path, 'wb') as f:
                f.write(buffer)
            dlog(f"[DEBUG-SAVE] ✅ File written OK: {final_path}")
            write_report(f"  📸 [截圖] 時間: {time_code} | 目標: {', '.join(objects_list)} | 檔名: {filename}")
        else:
            dlog(f"[DEBUG-SAVE] ❌ cv2.imencode failed for {final_path}")
            write_report(f"  ❌ [截圖失敗] 編碼錯誤: {filename}")
    except Exception as e:
        import traceback as tb
        dlog(f"[DEBUG-SAVE] ❌ Exception: {e}")
        dlog(tb.format_exc())
        write_report(f"  ❌ [截圖失敗] 寫入異常: {str(e)}")

# ==========================================
# 鑑識超解析 (Super Resolution) - NCNN 模組
# ==========================================
NCNN_MODEL_DIR = os.path.join(CONFIG.BASE_DIR, 'models', 'realesrgan')
NCNN_EXE_PATH = os.path.join(NCNN_MODEL_DIR, 'realesrgan-ncnn-vulkan.exe')
NCNN_DOWNLOAD_URL = 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip'
NCNN_ZIP_PATH = os.path.join(CONFIG.BASE_DIR, 'realesrgan-windows.zip')

sr_abort_flag = False

def check_and_download_sr_model():
    global sr_abort_flag
    if not os.path.exists(NCNN_EXE_PATH):
        print(">>> [系統預檢] 偵測到本機缺乏 NCNN 超解析引擎 (realesrgan-ncnn-vulkan.exe)")
        print(">>> [系統動作] 正在背景非同步下載免安裝引擎，請稍候 (約 25MB)...")
        try:
            os.makedirs(NCNN_MODEL_DIR, exist_ok=True)
            req = urllib.request.urlopen(NCNN_DOWNLOAD_URL, timeout=30)
            with open(NCNN_ZIP_PATH, 'wb') as f:
                while True:
                    if sr_abort_flag:
                        print(">>> [系統動作] 使用者已強制中止引擎下載！")
                        return False
                    chunk = req.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            print(">>> [系統動作] 下載完成，正在解壓縮引擎...")
            with zipfile.ZipFile(NCNN_ZIP_PATH, 'r') as zip_ref:
                zip_ref.extractall(NCNN_MODEL_DIR)
            os.remove(NCNN_ZIP_PATH)
            print(">>> [系統動作] NCNN 引擎解壓縮完成！")
        except Exception as e:
            print(f"❌ [數位鑑識崩潰]：NCNN 引擎下載失敗 ({e})")
            print("💡 [系統處置建議]：請確認對外網路連線，或手動下載並解壓縮至 models/realesrgan/ 目錄。")
            if os.path.exists(NCNN_ZIP_PATH):
                os.remove(NCNN_ZIP_PATH)
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
            img_data = base64.b64decode(base64_str)
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
                    # NCNN 運算
                    print(f">>> [系統動作] 發動 NCNN 物理級 GPU 鑑識重建 ({mode} 模式)...")
                    # 建立暫存圖
                    temp_in = os.path.join(CONFIG.BASE_DIR, "temp_sr_in.png")
                    temp_out = os.path.join(CONFIG.BASE_DIR, "temp_sr_out.png")
                    cv2.imwrite(temp_in, img)
                    
                    # 選擇模型：車牌用 x4plus，人像用 x4plus-anime
                    model_name = "realesrgan-x4plus-anime" if mode == 'face' else "realesrgan-x4plus"
                    
                    # 呼叫 subprocess
                    cmd = [NCNN_EXE_PATH, "-i", temp_in, "-o", temp_out, "-n", model_name]
                    CREATE_NO_WINDOW = 0x08000000
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                    
                    # 等待並允許中斷
                    while process.poll() is None:
                        if sr_abort_flag:
                            process.terminate()
                            print(">>> [系統動作] 鑑識重建已強制中止！")
                            if os.path.exists(temp_in): os.remove(temp_in)
                            if os.path.exists(temp_out): os.remove(temp_out)
                            return
                        time.sleep(0.1)
                        
                    if process.returncode == 0 and os.path.exists(temp_out):
                        result = cv2.imread(temp_out)
                    else:
                        raise Exception(f"NCNN 引擎回傳錯誤代碼 {process.returncode}")
                        
                    # 清理暫存檔
                    if os.path.exists(temp_in): os.remove(temp_in)
                    if os.path.exists(temp_out): os.remove(temp_out)
                    
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
        
        img_data = base64.b64decode(base64_str)
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

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    os.makedirs(CONFIG.CAPTURES_DIR, exist_ok=True)
    try:
        web_dir = os.path.join(CONFIG.BASE_DIR, 'web')
        eel.init(web_dir)
        print("==================================================")
        print("AG-MONITOR Forensic Player Engine Online!")
        print("http://localhost:8000/index.html")
        print("==================================================")
        eel.start('index.html', size=(1280, 950), mode='edge', port=8000)
    except Exception as e:
        print("Eel Boot Failed:", e)
