# Monitor-Filter-Tool 核心程式碼與 UI 結構封包

## 📄 檔案: main.py
``` py
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
            else:
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
        last_decoder_heartbeat = time.time()
        
        def watchdog_worker():
            nonlocal deadlock_detected
            while decode_thread_running:
                time.sleep(1)
                if time.time() - last_decoder_heartbeat > 5.0:
                    deadlock_detected = True
                    dlog("[WATCHDOG] 🚨 偵測到解碼執行緒卡死 (Deadlock)！觸發強制中斷！")
                    write_report(f"🚨 影片讀取失敗 (壞軌或死鎖): {video_name}")
                    eel.appendLog(f"🚨 {clean_v_name} 發生壞軌死鎖，看門狗已強制中斷", "danger")
                    break

        watchdog_thread = threading.Thread(target=watchdog_worker, daemon=True)
        watchdog_thread.start()

        def decoding_worker():
            nonlocal container, stream, last_decoder_heartbeat
            frame_iter = container.decode(stream)
            local_decoded_idx = -1
            
            while decode_thread_running:
                last_decoder_heartbeat = time.time()
                
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
                
            while decode_thread_running and not deadlock_detected:
                try:
                    item = frame_queue.get(timeout=0.1)
                    if item['idx'] == -1:
                        return None
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
                
                valid_targets = final_targets

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
        raise
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
                    save_legal_screenshot(state['best_frame'], output_dir, state['best_timecode'], state['best_summary'], prefix_name, state.get('best_target_info'))
                    eel.appendLog(f"[{state['best_timecode']}] 擷取 {state['best_summary'][0]}", "success")
            elif capture_mode == "事件起訖模式":
                if state['last_frame'] is not None:
                    save_legal_screenshot(state['last_frame'], output_dir, state['last_timecode'], [f"ID:{tid} {state['class_name']}(Exit)"], prefix_name, state.get('last_target_info'))
                    eel.appendLog(f"[{state['last_timecode']}] 擷取 ID:{tid} {state['class_name']}(Exit)", "success")
        del track_states[tid]

def _flush_all_track_states(track_states, capture_mode, output_dir, prefix_name, filter_stationary=True):
    for tid, state in track_states.items():
        if filter_stationary and not state['is_moving']:
            continue
        if capture_mode in ["雙格蒐證模式 (起點+最清晰)", "單次最清晰模式 (推薦)"]:
            if state['best_frame'] is not None:
                save_legal_screenshot(state['best_frame'], output_dir, state['best_timecode'], state['best_summary'], prefix_name, state.get('best_target_info'))
                eel.appendLog(f"[{state['best_timecode']}] 擷取 {state['best_summary'][0]}", "success")
        elif capture_mode == "事件起訖模式":
            if state['last_frame'] is not None:
                save_legal_screenshot(state['last_frame'], output_dir, state['last_timecode'], [f"ID:{tid} {state['class_name']}(Exit)"], prefix_name, state.get('last_target_info'))
                eel.appendLog(f"[{state['last_timecode']}] 擷取 ID:{tid} {state['class_name']}(Exit)", "success")
    track_states.clear()

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

```

## 📄 檔案: RUN.bat
``` bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo =======================================================
echo     AG-MONITOR Forensic Player - Dual-Mode Launcher
echo =======================================================
echo.

rem [ Defense Line 1: Check Local Python Environment ]
where python >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Local Python environment detected.
    
    python -c "import eel; import av; import ultralytics; import cv2" >nul 2>&1
    if errorlevel 1 (
        echo [InBox] Installing required forensic modules in background... Please wait...
        python -m pip uninstall -y opencv-python >nul 2>&1
        python -m pip install eel ultralytics opencv-contrib-python av lap lapx
        python -c "import eel; import av; import ultralytics; import cv2; import lap" >nul 2>&1
        if errorlevel 1 (
            echo [!] Failed to install modules.
            echo [-^>] Switching to portable mode...
            goto CHECK_EMBED
        )
    )
    
    echo -------------------------------------------------------
    echo [OK] Core Engine Ready! Launching AG-MONITOR...
    echo.
    python -B -u main.py
    pause
    exit /b
)

:CHECK_EMBED
rem [ Defense Line 2: Check Portable Environment ]
if exist ".\python-embed\python.exe" (
    echo [OK] Portable core detected, starting [Portable Mode]...
    echo [OK] Booting up tactical room...
    echo -------------------------------------------------------
    .\python-embed\python.exe -B -u main.py
    pause
    exit /b
)

echo [FATAL ERROR] Dual-boot failed!
echo 1. Local Python missing required modules and failed to install.
echo 2. ".\python-embed\" portable core directory not found.
echo.
pause
exit /b

```

## 📄 檔案: .vscode\settings.json
``` json
{}
```

## 📄 檔案: System-Optimizer-Tool.antigravity\main.py
``` py
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
```

## 📄 檔案: web\index.html
``` html
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <title>AG-MONITOR 科技偵查戰術播放器</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script type="text/javascript" src="/eel.js"></script>
    <style>
        /* CSS reset and base */
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { 
            height: 100vh; 
            width: 100vw;
            overflow: hidden; 
            background-color: #121212; 
            color: #E0E0E0; 
            font-family: "Microsoft JhengHei", sans-serif;
            display: flex;
            flex-direction: column;
        }

        /* Variables */
        :root {
            --bg-dark: #1E1E1E;
            --bg-panel: #252526;
            --border-color: #333333;
            --accent-green: #00FF66;
            --accent-blue: #007ACC;
            --text-main: #E0E0E0;
            --text-muted: #A0A0A0;
        }

        /* Layout */
        .panel-left {
            width: 350px;
            height: 100%;
            background-color: var(--bg-panel);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            overflow-y: auto;
        }
        
        .panel-right {
            flex-grow: 1;
            height: 100%;
            display: flex;
            flex-direction: column;
            background-color: var(--bg-dark);
            position: relative;
        }

        /* Left Panel Sections */
        .logo-container {
            padding: 10px;
            border-bottom: 1px solid var(--border-color);
            text-align: center;
            flex-shrink: 0;
        }
        
        .section {
            padding: 15px;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
        }
        .section-title {
            color: var(--accent-green);
            font-weight: bold;
            margin-bottom: 10px;
            font-size: 14px;
        }
        
        .queue-section {
            display: flex;
            flex-direction: column;
        }
        .queue-box {
            flex-grow: 1;
            overflow-y: auto;
            background: #1A1A1A;
            border: 1px solid var(--border-color);
            margin-top: 10px;
            padding: 5px;
        }
        .queue-item {
            padding: 5px;
            border-bottom: 1px solid #333;
            font-size: 13px;
        }

        /* Form elements */
        .btn-group { display: flex; gap: 5px; }
        .btn {
            background-color: #333;
            color: white;
            border: 1px solid #444;
            padding: 6px 12px;
            cursor: pointer;
            border-radius: 4px;
            font-size: 13px;
            flex-grow: 1;
            text-align: center;
            transition: 0.2s;
        }
        .btn:hover { background-color: #444; }
        .btn-start { background-color: #007ACC; border-color: #005A9E; }
        .btn-start:hover { background-color: #005A9E; }
        .btn-stop { background-color: #D9383A; border-color: #A32A2B; }
        .btn-stop:hover { background-color: #A32A2B; }
        .btn-active { background-color: var(--accent-green); color: black; font-weight: bold; }

        input[type=range] { width: 100%; margin-top: 5px; }
        .slider-header { display: flex; justify-content: space-between; font-size: 12px; }
        .highlight-val { color: var(--accent-green); }
        
        .cyber-select {
            width: 100%;
            background: #333;
            color: white;
            border: 1px solid #555;
            padding: 5px;
        }

        /* Right Panel */
        /* Top Mode Switcher */
        .mode-switcher-bar {
            height: 50px;
            background: #1A1A1A;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 20px;
        }
        .mode-btn {
            padding: 8px 20px;
            background: #252526;
            border: 1px solid #444;
            color: var(--text-muted);
            border-radius: 20px;
            cursor: pointer;
            font-size: 15px;
            font-weight: bold;
            transition: all 0.3s;
        }
        .mode-btn.active {
            background: rgba(0, 255, 102, 0.1);
            color: var(--accent-green);
            border-color: var(--accent-green);
            box-shadow: 0 0 10px rgba(0,255,102,0.3);
        }

        /* Canvas Area */
        .canvas-wrapper {
            flex-grow: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #000;
            position: relative;
            overflow: hidden;
        }
        #previewCanvas {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }
        .placeholder-text {
            position: absolute;
            color: #555;
            text-align: center;
            line-height: 1.5;
        }

        /* Player Controls (PotPlayer Style) */
        .player-controls {
            background: #1E1E1E;
            border-top: 1px solid var(--border-color);
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        /* Seek Bar */
        .timeline-container {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .time-display { font-family: monospace; font-size: 13px; color: var(--accent-green); width: 140px; text-align: center;}
        #timeline { flex-grow: 1; cursor: pointer; }

        /* Buttons Row */
        .control-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .control-group {
            display: flex;
            gap: 5px;
        }
        .icon-btn {
            background: transparent;
            border: 1px solid transparent;
            color: #CCC;
            font-size: 16px;
            width: 36px;
            height: 36px;
            cursor: pointer;
            border-radius: 4px;
            transition: 0.2s;
        }
        .icon-btn:hover { background: #333; color: white; }
        .speed-btn {
            background: #333;
            border: 1px solid #444;
            color: #CCC;
            padding: 4px 8px;
            cursor: pointer;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
        }
        .speed-btn.active {
            background: var(--accent-blue);
            color: white;
            border-color: var(--accent-blue);
        }

        /* Bottom Log Area */
        .log-wrapper {
            height: 200px;
            background: #1A1A1A;
            border-top: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
        }
        .status-bar {
            padding: 5px 10px;
            background: #252526;
            border-bottom: 1px solid #333;
            display: flex;
            justify-content: space-between;
            font-size: 13px;
        }
        .status-ok { color: var(--accent-green); }
        .status-warn { color: #FFCC00; }
        .status-danger { color: #FF4444; }
        
        .log-box {
            flex-grow: 1;
            overflow-y: auto;
            padding: 10px;
            font-family: monospace;
            font-size: 12px;
            line-height: 1.4;
        }
        .log-entry.info { color: #CCC; }
        .log-entry.success { color: var(--accent-green); }
        .log-entry.warn { color: #FFCC00; }
        .log-entry.error { color: #FF4444; }

        .hidden { display: none !important; }

        /* Global Tab Bar */
        .global-tab-bar {
            height: 40px;
            background: #000;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            padding: 0 10px;
            flex-shrink: 0;
        }
        .global-tab-btn {
            background: transparent;
            color: var(--text-muted);
            border: none;
            padding: 10px 20px;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
            outline: none;
            transition: 0.3s;
            border-bottom: 2px solid transparent;
        }
        .global-tab-btn:hover { color: white; }
        .global-tab-btn.active {
            color: var(--accent-green);
            border-bottom: 2px solid var(--accent-green);
            text-shadow: 0 0 5px rgba(0,255,102,0.5);
        }

        .main-view {
            display: none;
            flex-grow: 1;
            height: calc(100vh - 40px);
            overflow: hidden;
            position: relative;
        }
        .main-view.active {
            display: flex;
        }

        /* SR Workspace */
        #viewSR { flex-direction: column; background: var(--bg-dark); }
        .sr-controls {
            padding: 15px;
            background: var(--bg-panel);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            gap: 15px;
            justify-content: center;
            align-items: center;
        }
        .sr-controls .btn { max-width: 200px; padding: 10px 20px; font-size: 15px; }
        .sr-workspace {
            flex-grow: 1;
            display: flex;
            flex-direction: row;
            overflow: hidden;
        }
        .sr-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 15px;
        }
        .sr-divider {
            width: 1px;
            background: var(--accent-green);
            box-shadow: 0 0 5px rgba(0,255,102,0.5);
        }
        .sr-panel-title {
            color: var(--accent-green);
            font-weight: bold;
            margin-bottom: 10px;
            text-align: center;
            font-size: 16px;
        }
        .sr-canvas-wrap {
            flex-grow: 1;
            border: 1px dashed #444;
            background: #000;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
        }
        
        /* Queue Drawer */
        .queue-drawer {
            position: absolute;
            top: 0;
            left: -350px;
            width: 320px;
            height: 100%;
            background: rgba(20, 20, 20, 0.98);
            border-right: 2px solid var(--accent-green);
            box-shadow: 10px 0 20px rgba(0,0,0,0.8);
            z-index: 1005;
            transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            flex-direction: column;
            padding: 15px;
        }
        .queue-drawer.open {
            left: 0;
        }
        .drawer-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: var(--accent-green);
            font-weight: bold;
            font-size: 15px;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border-color);
        }
        .drawer-close-btn {
            background: transparent;
            border: none;
            color: #CCC;
            font-size: 18px;
            cursor: pointer;
            transition: 0.2s;
        }
        .drawer-close-btn:hover {
            color: white;
            transform: scale(1.2);
        }
    </style>
</head>
<body>
    <div class="global-tab-bar">
        <button id="mainTabPlayer" class="global-tab-btn active" onclick="switchMainTab('player')">👁️ 智慧戰術播放器</button>
        <button id="mainTabSR" class="global-tab-btn" onclick="switchMainTab('sr')">📸 數位鑑識照片修復</button>
        <button onclick="document.getElementById('helpModal').style.display='flex'" class="global-tab-btn" style="margin-left:auto; color: var(--accent-blue);"><i class="fas fa-book"></i> 系統操作手冊</button>
    </div>

    <div id="viewPlayer" class="main-view active">
        <div class="panel-left">
        <div class="logo-container">
            <svg width="200" height="48" viewBox="0 0 250 60">
                <polygon points="25,5 55,5 55,30 40,50 25,30" fill="#2A2A2A" stroke="#555555" stroke-width="2"/>
                <path d="M 15 45 A 25 25 0 0 1 65 -5" fill="none" stroke="#00FF66" stroke-width="1" stroke-dasharray="2,2"/>
                <path d="M 25 35 A 15 15 0 0 1 55 5" fill="none" stroke="#007ACC" stroke-width="1.5"/>
                <circle cx="40" cy="20" r="2" fill="#00FF66"/>
                <text x="75" y="25" fill="#FFFFFF" font-family="Arial" font-size="16" font-weight="bold" font-style="italic">AG-MONITOR</text>
                <text x="76" y="45" fill="#00FF66" font-family="'Microsoft JhengHei'" font-size="9" font-weight="bold">科技偵查 · 戰術播放器</text>
            </svg>
        </div>

        <div class="section queue-section">
            <div class="section-title">【1. 待處理證物清單】</div>
            <div class="btn-group" style="margin-bottom: 8px;">
                <button class="btn btn-default" style="flex:1; font-size:12px; padding: 6px 4px;" onclick="addVideos()">📁 匯入影片</button>
                <button class="btn btn-default" style="flex:1; font-size:12px; padding: 6px 4px;" onclick="addFolder()">📂 匯入資料夾</button>
                <button class="btn btn-default" style="flex:1; font-size:12px; padding: 6px 4px;" onclick="batchRename()">🏷️ 校正檔名</button>
            </div>
            <div class="btn-group">
                <button class="btn btn-default" style="flex:2; font-size:12px; padding: 6px 4px; border-color: var(--accent-green); color: var(--accent-green); font-weight: bold;" onclick="toggleQueueDrawer()">📋 監視器清單</button>
                <button class="btn btn-default" style="flex:1; font-size:12px; padding: 6px 4px;" onclick="clearQueue()">🗑️ 清除</button>
            </div>
        </div>

        <div class="section auto-ai-controls">
            <div class="section-title">【2. 智慧辨識設定】</div>
            
            <div style="margin-bottom: 15px;">
                <label style="font-size:14px; margin-bottom:5px; display:block;">AI 核心大腦 (模型選擇):</label>
                <select id="aiModel" class="cyber-select" style="margin-bottom: 5px;">
                    <option value="yolov8n.pt" selected>🏎️ 極速先鋒引擎 (Nano - 速度最快)</option>
                    <option value="yolov8s.pt">🦅 鷹眼精準引擎 (Small - 遠距/模糊特化)</option>
                </select>
            </div>

            <div class="slider-group">
                <div class="slider-header">
                    <span>AI 靈敏度門檻:</span>
                    <span id="confVal" class="highlight-val">0.40</span>
                </div>
                <input type="range" id="confSlider" min="0.10" max="0.90" step="0.01" value="0.40">
            </div>
            <div style="display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; font-size:13px;">
                <label><input type="checkbox" id="cls_person" checked> Person</label>
                <label><input type="checkbox" id="cls_bicycle" checked> Bicycle</label>
                <label><input type="checkbox" id="cls_car" checked> Car</label>
                <label><input type="checkbox" id="cls_motorcycle" checked> Moto</label>
                <label><input type="checkbox" id="cls_bus" checked> Bus</label>
                <label><input type="checkbox" id="cls_truck" checked> Truck</label>
            </div>
        </div>

        <div class="section auto-ai-controls">
            <div class="section-title">【3. 無人空景極速調速】</div>
            <label style="display:flex; align-items:center; gap:8px; margin-bottom:10px; font-size:14px; cursor:pointer;">
                <input type="checkbox" id="fastMode" checked> 啟用極速背景處理 (關閉實時畫面)
            </label>
            <label style="display:flex; align-items:center; gap:8px; margin-bottom:10px; font-size:14px; cursor:pointer;">
                <input type="checkbox" id="filterStationary" checked> 啟用靜止物件過濾 (自動忽略路邊停放車輛)
            </label>
            <div class="slider-group">
                <div class="slider-header">
                    <span>空景安全閃現跳躍間隔 (秒):</span>
                    <span id="skipSecVal" class="highlight-val" style="color: #00ffcc;">0.2s [標準安全]</span>
                </div>
                <input type="range" id="skipSecSlider" min="0.1" max="5.0" step="0.1" value="0.2">
            </div>
        </div>

        <div class="section auto-ai-controls">
            <div class="section-title">【4. 蒐證模式設定】</div>
            <select id="captureMode" class="cyber-select">
                <option value="雙格蒐證模式 (起點+最清晰)" selected>精華雙格蒐證 (起點格 + 最清晰特徵格)</option>
                <option value="持續追蹤模式 (預設)">動態定時連拍 (依設定秒數連續拍照)</option>
                <option value="事件起訖模式">進出瞬間捕捉 (僅抓進入與離開首尾兩張)</option>
            </select>
            <label style="display:flex; align-items:center; gap:8px; margin-top:10px; font-size:14px; cursor:pointer;">
                <input type="checkbox" id="singleFolder"> 將本次批次結果集中於單一資料夾
            </label>
        </div>

        <div class="section" style="margin-bottom: 0;">
            <div class="section-title">【5. 戰術啟動區】</div>
            <button class="btn btn-default" style="width: 100%; margin-bottom: 8px;" onclick="clearRoi()">⬡ 清除 ROI 防線</button>
            <button class="btn btn-folder" style="width: 100%; margin-bottom: 8px;" onclick="openFolder()">📂 開啟截圖資料夾</button>
            <button id="btnStartAuto" class="btn btn-start" style="width: 100%; padding:10px;" onclick="toggleAutoProcessing()">▶️ 啟動全自動 AI 過濾</button>
        </div>

        <!-- 底部版權與開發者資訊 -->
        <div style="margin-top: auto; padding: 10px; text-align: center; font-size: 11px; color: var(--text-muted); border-top: 1px solid var(--border-color); background: #1A1A1A;">
            AG-MONITOR v3.0<br>
            Developed by <a href="https://github.com/lianghao02" target="_blank" style="color: var(--accent-green); text-decoration: none; font-weight: bold;">@lianghao02</a>
        </div>
        </div>
        
        <!-- 側滑證物清單抽屜 -->
        <div id="queueDrawer" class="queue-drawer">
            <div class="drawer-header">
                <span>【監視器清單】</span>
                <button class="drawer-close-btn" onclick="toggleQueueDrawer()">✖</button>
            </div>
            <div class="queue-box" id="queueList" style="margin-bottom:0; flex-grow:1;"></div>
        </div>

    <div class="panel-right">
        <!-- 頂部雙模態切換 -->
        <div class="mode-switcher-bar">
            <button id="modeAutoBtn" class="mode-btn active" onclick="switchMode('auto')">🎯 全自動 AI 蒐證</button>
            <button id="modeManualBtn" class="mode-btn" onclick="switchMode('manual')">👁️ 即時人眼點視</button>
        </div>

        <!-- 畫布區 -->
        <div class="canvas-wrapper">
            <div id="placeholderText" class="placeholder-text">
                【AG-Forensic-Player 萬用 AI 科技偵查戰術播放器】<br><br>
                1. 支援 .mp4, .265, .h265, .264, .dav 等原生裸流 0 秒秒開<br>
                2. 導入後可於此劃定多邊形 ROI 防線<br>
                3. 切換上方【即時人眼點視】可作為專業證據播放器使用
            </div>
            <canvas id="previewCanvas"></canvas>
        </div>

        <!-- 播放器控制列 (僅在人眼點視模式顯示) -->
        <div id="playerControls" class="player-controls hidden">
            <div class="timeline-container">
                <div class="time-display" id="timeDisplay">00:00:00 / 00:00:00</div>
                <input type="range" id="timeline" min="0" max="100" value="0" step="0.1">
            </div>
            <div class="control-row">
                <div class="control-group">
                    <button class="icon-btn" onclick="eel.play_pause()()" title="播放/暫停 (Space)"><i class="fas fa-play" id="playIcon"></i></button>
                    <button class="icon-btn" onclick="eel.step_frame(-1)()" title="倒退一格 (Left Arrow)"><i class="fas fa-step-backward"></i></button>
                    <button class="icon-btn" onclick="eel.step_frame(1)()" title="前進一格 (Right Arrow)"><i class="fas fa-step-forward"></i></button>
                    <button class="icon-btn" onclick="eel.toggle_reverse()()" title="倒放開關"><i class="fas fa-backward" id="reverseIcon"></i></button>
                </div>
                <div class="control-group">
                    <button class="speed-btn active" onclick="setSpeed(1, this)">1x</button>
                    <button class="speed-btn" onclick="setSpeed(2, this)">2x</button>
                    <button class="speed-btn" onclick="setSpeed(4, this)">4x</button>
                    <button class="speed-btn" onclick="setSpeed(8, this)">8x</button>
                    <button class="speed-btn" onclick="setSpeed(16, this)">16x</button>
                </div>
                <div class="control-group">
                    <button class="btn btn-stop" style="font-weight:bold;" onclick="eel.manual_capture()()" title="快捷鍵: C">📸 手動快門 (C)</button>
                </div>
            </div>
        </div>

        <!-- 底部日誌區 -->
        <div class="log-wrapper">
            <div class="status-bar">
                <span id="statusText" class="status-ok">狀態: 系統準備就緒</span>
                <span id="progressText">0%</span>
            </div>
            
            <div id="unifiedProgressContainer" style="display: none; padding: 6px 10px; background: #111; border-bottom: 1px solid #333;">
                <div style="display: flex; justify-content: space-between; font-size: 12px; color: #00FF66; margin-bottom: 4px;">
                    <span id="unifiedProgressLabel" style="font-weight: bold;">分析中...</span>
                    <span id="unifiedProgressPct">0%</span>
                </div>
                <div style="width: 100%; height: 8px; background: #222; border-radius: 4px; overflow: hidden; box-shadow: inset 0 1px 3px rgba(0,0,0,0.5);">
                    <div id="unifiedProgressBar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #00b34a, #00FF66); transition: width 0.1s linear;"></div>
                </div>
            </div>

            <div class="log-box" id="logBox">
                <div class="log-entry info">系統已初始化，等待任務...</div>
            </div>
        </div>
        </div>
    </div>

    <!-- AI SR View -->
    <div id="viewSR" class="main-view">
        <div class="sr-controls">
            <input type="file" id="srFileInput" accept="image/*" class="hidden">
            <button class="btn btn-default" onclick="document.getElementById('srFileInput').click()">📁 選擇圖片</button>
            <div class="sr-mode-select" style="display:flex; align-items:center; gap:10px; color:#ccc; font-size:14px; margin:0 10px;">
                <label style="cursor:pointer;"><input type="radio" name="srMode" value="plate" checked> 🚗 文字車牌 (強化邊緣)</label>
                <label style="cursor:pointer;"><input type="radio" name="srMode" value="face"> 👤 人像五官 (降噪平滑)</label>
            </div>
            <button id="btnStartSR" class="btn btn-start" onclick="startSR()">⚡ 開始 AI 高畫質化</button>
            <button id="btnAbortSR" class="btn btn-stop" style="display:none; background-color:#FF4444;" onclick="abortSR()">❌ 中止修復</button>
            <button id="btnSaveSR" class="btn btn-stop" onclick="saveSR()">💾 儲存高畫質證物</button>
        </div>
        <div class="sr-workspace">
            <div class="sr-panel sr-panel-left">
                <div class="sr-panel-title">【🔍 原始證物格】</div>
                <div class="sr-canvas-wrap">
                    <span id="srLeftPlaceholder" class="placeholder-text">點擊上方按鈕選擇或拖曳證物圖片至此</span>
                    <img id="srOriginalImg" src="" style="display:none; max-width:100%; max-height:100%; object-fit:contain;">
                </div>
            </div>
            <div class="sr-divider"></div>
            <div class="sr-panel sr-panel-right">
                <div class="sr-panel-title">【🎯 AI 重建格】</div>
                <div class="sr-canvas-wrap">
                    <span id="srRightPlaceholder" class="placeholder-text">等待 AI 重建...</span>
                    <img id="srEnhancedImg" src="" style="display:none; max-width:100%; max-height:100%; object-fit:contain;">
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentMode = 'auto'; // 'auto' or 'manual'
        let isProcessing = false;
        let isPlaying = false;
        let isReverse = false;
        let roiPoints = [];
        let scaleInfo = null;
        let currentImage = null;

        // UI Initialization
        const confSlider = document.getElementById("confSlider");
        const confVal = document.getElementById("confVal");
        confSlider.oninput = function() { 
            confVal.textContent = parseFloat(this.value).toFixed(2); 
            if(isProcessing) eel.update_live_setting('confThresh', parseFloat(this.value))();
        };

        const skipSecSlider = document.getElementById("skipSecSlider");
        const skipSecVal = document.getElementById("skipSecVal");
        skipSecSlider.oninput = function() {
            let val = parseFloat(this.value);
            let label = val <= 1.0 ? "s [標準安全]" : "s [極速/大場景限定]";
            skipSecVal.textContent = val.toFixed(1) + label; 
            if(val <= 1.0) {
                skipSecVal.style.color = "#00ffcc";
            } else {
                skipSecVal.style.color = "#ff9900";
            }
            if(isProcessing) eel.update_live_setting('skipSec', val)();
        };

        const fastModeCb = document.getElementById("fastMode");
        fastModeCb.onchange = function() {
            if(isProcessing) eel.update_live_setting('fastMode', this.checked)();
        };

        document.getElementById("captureMode").onchange = function() {
            if(isProcessing) eel.update_live_setting('captureMode', this.value)();
        };

        function updateLiveClasses() {
            if(isProcessing) {
                let classes = {
                    "0": document.getElementById("cls_person").checked,
                    "1": document.getElementById("cls_bicycle").checked,
                    "2": document.getElementById("cls_car").checked,
                    "3": document.getElementById("cls_motorcycle").checked,
                    "5": document.getElementById("cls_bus").checked,
                    "7": document.getElementById("cls_truck").checked
                };
                eel.update_live_setting('classes', classes)();
            }
        }
        
        function toggleQueueDrawer() {
            let drawer = document.getElementById("queueDrawer");
            drawer.classList.toggle("open");
        }
        document.getElementById("cls_person").onchange = updateLiveClasses;
        document.getElementById("cls_bicycle").onchange = updateLiveClasses;
        document.getElementById("cls_car").onchange = updateLiveClasses;
        document.getElementById("cls_motorcycle").onchange = updateLiveClasses;
        document.getElementById("cls_bus").onchange = updateLiveClasses;
        document.getElementById("cls_truck").onchange = updateLiveClasses;
        document.getElementById("singleFolder").onchange = function() {
            if(isProcessing) eel.update_live_setting('singleFolder', this.checked)();
        };

        const timeline = document.getElementById("timeline");
        timeline.oninput = function() {
            if(currentMode === 'manual') eel.seek_frame(parseFloat(this.value))();
        };

        async function batchRename() {
            if(isProcessing) {
                appendLog("分析中無法重新命名檔案", "error");
                return;
            }
            
            let keepOld = confirm("系統即將把清單內的影片換算回真實時間。\n\n請問是否要【保留原始檔名】作為安全對照？\n\n[確定 / OK] = 保留 (安全無損，強烈推薦)\n[取消 / Cancel] = 不保留 (極致乾淨，舊檔名將永久刪除)");
            
            let res = await eel.batch_rename_videos(keepOld)();
            if(!res.success) {
                appendLog("重新命名失敗: " + res.msg, "error");
                return;
            }
            let queueBox = document.getElementById("queueList");
            queueBox.innerHTML = "";
            for(let p of res.new_paths) {
                let item = document.createElement("div");
                item.className = "queue-item";
                item.textContent = p.split(/[\\/]/).pop();
                queueBox.appendChild(item);
            }
            
            appendLog(`智能校正檔名完成！共修改了 ${res.count} 個檔案`, "success");
            
            // 強勢的完成提示，讓使用者明確知道瞬間完成了
            alert(`✅ 檔案名稱已全部校正完畢！\n\n系統已修改了 ${res.count} 個實體檔案。\n您可以直接開始進行 AI 證據過濾了！`);
        }
        
        function showUnifiedProgress(label, pct) {
            let container = document.getElementById("unifiedProgressContainer");
            container.style.display = "block";
            document.getElementById("unifiedProgressLabel").innerText = label;
            document.getElementById("unifiedProgressPct").innerText = pct.toFixed(1) + "%";
            document.getElementById("unifiedProgressBar").style.width = pct + "%";
        }

        function hideUnifiedProgress() {
            document.getElementById("unifiedProgressContainer").style.display = "none";
        }

        eel.expose(updateRenameProgress);
        function updateRenameProgress(current, total) {
            let pct = total > 0 ? (current / total) * 100 : 0;
            showUnifiedProgress(`🏷️ 檔案校正中 (${current}/${total})`, pct);
            if (current >= total) {
                setTimeout(hideUnifiedProgress, 1000);
            }
        }

        function switchMode(mode) {
            if(isProcessing) {
                appendLog("請先停止目前任務再切換模式！", "warn");
                return;
            }
            currentMode = mode;
            document.getElementById('modeAutoBtn').className = mode === 'auto' ? 'mode-btn active' : 'mode-btn';
            document.getElementById('modeManualBtn').className = mode === 'manual' ? 'mode-btn active' : 'mode-btn';
            
            const autoControls = document.querySelectorAll('.auto-ai-controls');
            autoControls.forEach(el => el.style.display = mode === 'auto' ? 'block' : 'none');
            
            document.getElementById('playerControls').className = mode === 'manual' ? 'player-controls' : 'player-controls hidden';
            
            let btn = document.getElementById('btnStartAuto');
            if(mode === 'auto') {
                btn.innerHTML = "▶️ 啟動全自動 AI 過濾";
            } else {
                btn.innerHTML = "▶️ 載入戰術播放引擎";
            }
            
            eel.set_engine_mode(mode)();
        }

        function setSpeed(speed, btnElement) {
            document.querySelectorAll('.speed-btn').forEach(btn => btn.classList.remove('active'));
            btnElement.classList.add('active');
            eel.set_speed(speed)();
        }

        // Eel Calls
        async function addFolder() {
            if(isProcessing) return;
            let paths = await eel.add_folder_dialog()();
            if(paths && paths.length > 0) {
                document.getElementById("placeholderText").style.display = "none";
                let queueBox = document.getElementById("queueList");
                for(let p of paths) {
                    let item = document.createElement("div");
                    item.className = "queue-item";
                    item.textContent = p.split(/[\\/]/).pop();
                    item.onclick = function() {
                        if(isProcessing) {
                            eel.play_specific_video(p)();
                            toggleQueueDrawer(); // Auto-close drawer
                        }
                    };
                    queueBox.appendChild(item);
                }
            }
        }

        async function addVideos() {
            if(isProcessing) return;
            let paths = await eel.add_videos_dialog()();
            if(paths && paths.length > 0) {
                document.getElementById("placeholderText").style.display = "none";
                let queueBox = document.getElementById("queueList");
                for(let p of paths) {
                    let item = document.createElement("div");
                    item.className = "queue-item";
                    item.textContent = p.split(/[\\/]/).pop();
                    item.onclick = function() {
                        if(isProcessing) {
                            eel.play_specific_video(p)();
                            toggleQueueDrawer(); // Auto-close drawer
                        }
                    };
                    queueBox.appendChild(item);
                }
            }
        }

        function clearQueue() {
            if(isProcessing) return;
            eel.clear_queue()();
            document.getElementById("queueList").innerHTML = "";
            clearRoi();
            document.getElementById("placeholderText").style.display = "block";
            let canvas = document.getElementById("previewCanvas");
            canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
            currentImage = null;
        }

        function clearRoi() {
            roiPoints = [];
            eel.set_roi_points(roiPoints)();
            drawCanvas();
        }

        function openFolder() {
            eel.open_capture_folder()();
        }

        async function toggleAutoProcessing() {
            let btn = document.getElementById("btnStartAuto");
            if(isProcessing) {
                btn.innerHTML = currentMode === 'auto' ? "▶️ 啟動全自動 AI 過濾" : "▶️ 載入戰術播放引擎";
                btn.className = "btn btn-start";
                document.getElementById("aiModel").disabled = false;
                eel.request_stop()();
                return;
            }
            
            let settings = {
                aiModel: document.getElementById("aiModel").value,
                confThresh: parseFloat(confSlider.value),
                captureMode: document.getElementById("captureMode").value,
                classes: {
                    "0": document.getElementById("cls_person").checked,
                    "1": document.getElementById("cls_bicycle").checked,
                    "2": document.getElementById("cls_car").checked,
                    "3": document.getElementById("cls_motorcycle").checked,
                    "5": document.getElementById("cls_bus").checked,
                    "7": document.getElementById("cls_truck").checked
                },
                fastMode: fastModeCb.checked,
                filterStationary: document.getElementById("filterStationary").checked,
                skipSec: parseFloat(skipSecSlider.value),
                singleFolder: document.getElementById("singleFolder").checked
            };
            
            btn.innerHTML = currentMode === 'auto' ? "⏹️ 停止全自動分析" : "⏹️ 關閉播放引擎";
            btn.className = "btn btn-stop";
            document.getElementById("aiModel").disabled = true;
            isProcessing = true;
            
            document.getElementById("logBox").innerHTML = "";
            appendLog(currentMode === 'auto' ? ">>> 系統啟動，全記憶體零延遲解碼引擎發動..." : ">>> 戰術播放引擎已載入，請使用下方控制列操作", "info");
            
            await eel.start_processing(settings)();
        }

        // Eel Exposed Functions
        eel.expose(updateStatus);
        function updateStatus(text, level) {
            let el = document.getElementById("statusText");
            el.textContent = text;
            if(level === 'ok') el.className = "status-ok";
            else if(level === 'warn') el.className = "status-warn";
            else el.className = "status-danger";
        }

        eel.expose(updateProgress);
        function updateProgress(percent, timecode) {
            document.getElementById("progressText").textContent = parseFloat(percent).toFixed(1) + "%";
            document.getElementById("timeline").value = percent;
            if(timecode) {
                document.getElementById("timeDisplay").textContent = timecode;
            }
            if(isProcessing) {
                showUnifiedProgress("🕵️ AI 證據全自動過濾分析中", parseFloat(percent));
            }
        }

        eel.expose(updatePlayState);
        function updatePlayState(playing, reverse) {
            isPlaying = playing;
            isReverse = reverse;
            document.getElementById("playIcon").className = playing ? "fas fa-pause" : "fas fa-play";
            document.getElementById("reverseIcon").style.color = reverse ? "var(--accent-green)" : "#CCC";
        }

        eel.expose(appendLog);
        function appendLog(msg, type='info') {
            let box = document.getElementById("logBox");
            let entry = document.createElement("div");
            entry.className = "log-entry " + type;
            entry.textContent = msg;
            box.appendChild(entry);
            box.scrollTop = box.scrollHeight;
        }

        eel.expose(processingFinished);
        function processingFinished() {
            isProcessing = false;
            hideUnifiedProgress();
            let btn = document.getElementById("btnStartAuto");
            btn.innerHTML = currentMode === 'auto' ? "▶️ 啟動全自動 AI 過濾" : "▶️ 載入戰術播放引擎";
            btn.className = "btn btn-start";
            document.getElementById("aiModel").disabled = false;
            document.getElementById("progressText").textContent = "0%";
            document.getElementById("timeline").value = 0;
            appendLog(">>> 任務結束或已手動中止", "info");
        }

        let currentBoxes = [];
        let currentOsdText = "";

        eel.expose(setPreviewImage);
        function setPreviewImage(base64Data, scaleInfoObj, jsonBoxes=[], roiPts=[], timeCodeStr="") {
            scaleInfo = scaleInfoObj;
            currentBoxes = jsonBoxes;
            currentOsdText = timeCodeStr;
            
            document.getElementById("placeholderText").style.display = "none";
            let canvas = document.getElementById("previewCanvas");
            canvas.width = scaleInfo.canvas_w;
            canvas.height = scaleInfo.canvas_h;
            
            currentImage = new Image();
            currentImage.onload = function() { drawCanvas(); }
            currentImage.src = "data:image/jpeg;base64," + base64Data;
        }

        // Canvas ROI Logic
        const canvas = document.getElementById("previewCanvas");
        canvas.addEventListener('click', function(e) {
            if(!scaleInfo || !currentImage) return;
            const rect = canvas.getBoundingClientRect();
            const scaleX = canvas.width / rect.width;
            const scaleY = canvas.height / rect.height;
            
            const x = (e.clientX - rect.left) * scaleX;
            const y = (e.clientY - rect.top) * scaleY;
            roiPoints.push([x, y]);
            eel.set_roi_points(roiPoints)();
            drawCanvas();
        });

        function drawCanvas() {
            if(!currentImage) return;
            let ctx = canvas.getContext("2d");
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(currentImage, 0, 0);
            
            if(currentOsdText) {
                ctx.font = "16px 'Segoe UI', Arial, sans-serif";
                let text = "AG-MONITOR | " + currentOsdText;
                let metrics = ctx.measureText(text);
                ctx.fillStyle = "rgba(0, 0, 0, 0.7)";
                ctx.fillRect(5, canvas.height - 25, metrics.width + 10, 20);
                ctx.fillStyle = "#00FFFF";
                ctx.fillText(text, 10, canvas.height - 6);
            }

            if(currentBoxes && currentBoxes.length > 0) {
                for(let box of currentBoxes) {
                    let w = box.x2 - box.x1;
                    let h = box.y2 - box.y1;
                    
                    ctx.shadowColor = "rgba(0, 0, 0, 0.5)";
                    ctx.shadowBlur = 4;
                    ctx.strokeStyle = "#FF3366";
                    ctx.lineWidth = 2;
                    ctx.strokeRect(box.x1, box.y1, w, h);
                    ctx.shadowBlur = 0;
                    
                    let label = "ID:" + box.tid + " " + box.cls_name + " " + box.conf.toFixed(2);
                    ctx.font = "bold 12px 'Segoe UI', Arial, sans-serif";
                    let tw = ctx.measureText(label).width;
                    
                    ctx.fillStyle = "rgba(255, 51, 102, 0.85)";
                    ctx.fillRect(box.x1, box.y1 - 18, tw + 6, 18);
                    ctx.fillStyle = "#FFFFFF";
                    ctx.fillText(label, box.x1 + 3, box.y1 - 5);
                }
            }
            
            if(roiPoints.length > 0) {
                ctx.strokeStyle = "#00FF66";
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(roiPoints[0][0], roiPoints[0][1]);
                for(let i=1; i<roiPoints.length; i++) ctx.lineTo(roiPoints[i][0], roiPoints[i][1]);
                if(roiPoints.length > 1) ctx.lineTo(roiPoints[0][0], roiPoints[0][1]);
                ctx.stroke();
                
                ctx.fillStyle = "#D9383A";
                for(let pt of roiPoints) {
                    ctx.beginPath();
                    ctx.arc(pt[0], pt[1], 4, 0, 2*Math.PI);
                    ctx.fill();
                }
            }
        }

        // Keyboard bindings
        document.addEventListener('keydown', function(e) {
            if(e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if(document.getElementById("viewPlayer").classList.contains("active") && currentMode === 'manual') {
                if(e.code === 'Space') {
                    e.preventDefault();
                    eel.play_pause()();
                } else if(e.code === 'ArrowRight') {
                    e.preventDefault();
                    eel.step_frame(1)();
                } else if(e.code === 'ArrowLeft') {
                    e.preventDefault();
                    eel.step_frame(-1)();
                } else if(e.code === 'KeyC') {
                    e.preventDefault();
                    eel.manual_capture()();
                }
            }
        });

        // Global Tab Switch Logic
        function switchMainTab(tab) {
            document.getElementById("mainTabPlayer").className = tab === 'player' ? "global-tab-btn active" : "global-tab-btn";
            document.getElementById("mainTabSR").className = tab === 'sr' ? "global-tab-btn active" : "global-tab-btn";
            
            document.getElementById("viewPlayer").className = tab === 'player' ? "main-view active" : "main-view";
            document.getElementById("viewSR").className = tab === 'sr' ? "main-view active" : "main-view";
        }

        // SR Workspace Logic
        let srOriginalBase64 = null;
        let srEnhancedBase64 = null;
        let isSRProcessing = false;

        document.getElementById('srFileInput').addEventListener('change', function(e) {
            const file = e.target.files[0];
            if(file) {
                const reader = new FileReader();
                reader.onload = function(evt) {
                    srOriginalBase64 = evt.target.result.split(',')[1];
                    document.getElementById('srOriginalImg').src = evt.target.result;
                    document.getElementById('srOriginalImg').style.display = 'block';
                    document.getElementById('srLeftPlaceholder').style.display = 'none';
                    
                    document.getElementById('srEnhancedImg').style.display = 'none';
                    document.getElementById('srRightPlaceholder').style.display = 'block';
                    document.getElementById('srRightPlaceholder').textContent = '圖片已載入，請點擊開始 AI 高畫質化';
                    srEnhancedBase64 = null;
                };
                reader.readAsDataURL(file);
            }
        });

        // Drag & Drop Implementation for Photo Repair
        const srCanvasWrap = document.querySelector('.sr-panel-left .sr-canvas-wrap');
        srCanvasWrap.addEventListener('dragover', function(e) {
            e.preventDefault();
            srCanvasWrap.style.borderColor = 'var(--accent-green)';
            srCanvasWrap.style.background = 'rgba(0, 255, 102, 0.05)';
        });
        
        srCanvasWrap.addEventListener('dragleave', function(e) {
            e.preventDefault();
            srCanvasWrap.style.borderColor = '#444';
            srCanvasWrap.style.background = '#000';
        });
        
        srCanvasWrap.addEventListener('drop', function(e) {
            e.preventDefault();
            srCanvasWrap.style.borderColor = '#444';
            srCanvasWrap.style.background = '#000';
            
            const files = e.dataTransfer.files;
            if (files && files.length > 0) {
                const file = files[0];
                if (file.type.startsWith('image/')) {
                    const reader = new FileReader();
                    reader.onload = function(evt) {
                        srOriginalBase64 = evt.target.result.split(',')[1];
                        document.getElementById('srOriginalImg').src = evt.target.result;
                        document.getElementById('srOriginalImg').style.display = 'block';
                        document.getElementById('srLeftPlaceholder').style.display = 'none';
                        
                        document.getElementById('srEnhancedImg').style.display = 'none';
                        document.getElementById('srRightPlaceholder').style.display = 'block';
                        document.getElementById('srRightPlaceholder').textContent = '圖片已載入，請點擊開始 AI 高畫質化';
                        srEnhancedBase64 = null;
                    };
                    reader.readAsDataURL(file);
                } else {
                    alert("請拖曳圖片格式檔案！");
                }
            }
        });

        async function startSR() {
            if(isSRProcessing) return;
            if(!srOriginalBase64) {
                alert("請先選擇一張原始證物圖片！");
                return;
            }
            
            isSRProcessing = true;
            let btn = document.getElementById("btnStartSR");
            btn.innerHTML = "⏳ AI 鑑識運算中...";
            btn.style.opacity = "0.5";
            btn.style.cursor = "not-allowed";
            
            document.getElementById("btnAbortSR").style.display = "inline-block";
            
            document.getElementById('srRightPlaceholder').style.display = 'block';
            document.getElementById('srRightPlaceholder').textContent = '🧠 載入深度學習模型並進行像素重建... (或從雲端拉取權重檔)';
            document.getElementById('srEnhancedImg').style.display = 'none';
            
            let mode = document.querySelector('input[name="srMode"]:checked').value;
            
            // Call eel non-blocking
            eel.run_ai_super_resolution(srOriginalBase64, mode)();
        }

        function abortSR() {
            if(!isSRProcessing) return;
            eel.abort_ai_super_resolution()();
            
            isSRProcessing = false;
            let btn = document.getElementById("btnStartSR");
            btn.innerHTML = "⚡ 開始 AI 高畫質化";
            btn.style.opacity = "1";
            btn.style.cursor = "pointer";
            
            document.getElementById("btnAbortSR").style.display = "none";
            document.getElementById('srRightPlaceholder').style.display = 'block';
            document.getElementById('srRightPlaceholder').textContent = '⚠️ 已經強制中止修復進程，系統資源已釋放。';
        }

        eel.expose(on_super_res_finished);
        function on_super_res_finished(base64Data, errorMsg) {
            isSRProcessing = false;
            let btn = document.getElementById("btnStartSR");
            btn.innerHTML = "⚡ 開始 AI 高畫質化";
            btn.style.opacity = "1";
            btn.style.cursor = "pointer";
            
            document.getElementById("btnAbortSR").style.display = "none";

            if(errorMsg) {
                document.getElementById('srRightPlaceholder').innerHTML = `<span style="color:#FF4444;">${errorMsg}</span>`;
            } else if (base64Data) {
                srEnhancedBase64 = base64Data;
                document.getElementById('srEnhancedImg').src = "data:image/jpeg;base64," + base64Data;
                document.getElementById('srEnhancedImg').style.display = 'block';
                document.getElementById('srRightPlaceholder').style.display = 'none';
            }
        }

        function saveSR() {
            if(isSRProcessing) return;
            if(!srEnhancedBase64) {
                alert("沒有可儲存的高畫質重建結果！");
                return;
            }
            let mode = document.querySelector('input[name="srMode"]:checked').value;
            eel.save_enhanced_evidence(srEnhancedBase64, mode)();
        }
    </script>

    <!-- 操作手冊 Modal -->
    <div id="helpModal" style="display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); z-index: 9999; justify-content: center; align-items: center; backdrop-filter: blur(5px);">
        <div style="background: var(--bg-panel); border: 1px solid var(--accent-green); box-shadow: 0 0 20px rgba(0,255,102,0.2); width: 90%; max-width: 800px; max-height: 85vh; display: flex; flex-direction: column; border-radius: 8px; overflow: hidden;">
            <div style="padding: 15px 20px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; background: #1A1A1A;">
                <span style="color: var(--accent-green); font-weight: bold; font-size: 18px;"><i class="fas fa-book"></i> AG-MONITOR 系統操作手冊</span>
                <button onclick="document.getElementById('helpModal').style.display='none'" style="background: transparent; border: none; color: #AAA; font-size: 24px; cursor: pointer; transition: 0.2s;" onmouseover="this.style.color='#FFF'" onmouseout="this.style.color='#AAA'">&times;</button>
            </div>
            <div style="padding: 25px; overflow-y: auto; line-height: 1.7; font-size: 14px; color: #E0E0E0;">
                <h3 style="color: var(--accent-blue); margin-bottom: 10px; border-bottom: 1px solid #333; padding-bottom: 5px;">🎯 模式一：全自動 AI 蒐證</h3>
                <p style="margin-bottom: 15px; color: #BBB;">適合大量影片或長時間空景的自動化過濾與截圖。</p>
                <ol style="margin-left: 20px; margin-bottom: 20px; color: #DDD;">
                    <li style="margin-bottom: 8px;"><strong>匯入證物：</strong>點擊左側「匯入影片」或「匯入資料夾」，支援 .mp4, .265, .dav 等特殊格式。</li>
                    <li style="margin-bottom: 8px;"><strong>畫定防線：</strong>在中央畫面上點擊滑鼠左鍵，畫出多邊形的「ROI 感興趣區域」。(點擊左側「清除 ROI」可重畫)</li>
                    <li style="margin-bottom: 8px;"><strong>選擇設定：</strong>設定 AI 靈敏度、選擇要捕捉的目標 (人/車)，並設定「蒐證模式」(例如精華雙格或持續追蹤)。</li>
                    <li style="margin-bottom: 8px;"><strong>啟動引擎：</strong>點擊「啟動全自動 AI 過濾」，系統會在背景極速掃描。完成後點擊「開啟截圖資料夾」檢視成果。</li>
                </ol>

                <h3 style="color: var(--accent-blue); margin-bottom: 10px; border-bottom: 1px solid #333; padding-bottom: 5px;">👁️ 模式二：即時人眼點視</h3>
                <p style="margin-bottom: 15px; color: #BBB;">作為專業級證據播放器，支援逐格精確檢視。</p>
                <ul style="margin-left: 20px; margin-bottom: 20px; color: #DDD;">
                    <li style="margin-bottom: 8px;">點擊右上方切換至「即時人眼點視」模式。</li>
                    <li style="margin-bottom: 8px;">下方將出現播放控制列，支援快轉 (最高 16x)、倒放。</li>
                    <li style="margin-bottom: 8px;"><strong>快捷鍵支援：</strong>空白鍵 (播放/暫停)、左方向鍵 (倒退一格)、右方向鍵 (前進一格)。</li>
                    <li style="margin-bottom: 8px;"><strong>手動截圖：</strong>看到關鍵畫面時，可直接按下快捷鍵 <kbd style="background: #333; padding: 2px 6px; border-radius: 4px;">C</kbd> 進行無損截圖。</li>
                </ul>

                <h3 style="color: var(--accent-blue); margin-bottom: 10px; border-bottom: 1px solid #333; padding-bottom: 5px;">📸 模式三：數位鑑識照片修復 (SR)</h3>
                <p style="margin-bottom: 15px; color: #BBB;">針對模糊的車牌或人臉進行 AI 去噪與超解析重建。</p>
                <ul style="margin-left: 20px; margin-bottom: 20px; color: #DDD;">
                    <li style="margin-bottom: 8px;">點擊最上方頁籤切換至「數位鑑識照片修復」。</li>
                    <li style="margin-bottom: 8px;">匯入剛才截下來的模糊照片，選擇對應的模型 (車牌強化或人臉特化)。</li>
                    <li style="margin-bottom: 8px;">點擊「開始 AI 高畫質化」，完成後可點擊「儲存鑑識結果」。</li>
                </ul>
            </div>
        </div>
    </div>
</body>
</html>

```

