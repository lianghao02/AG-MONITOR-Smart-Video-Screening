"""以四支實體影片執行完整批次，輸出可稽核的長時間測試報告。"""

import json
import multiprocessing
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil


PROJECT_DIR = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

import main


class HeadlessEel:
    def __init__(self):
        self.counts = {}
        self.lock = threading.Lock()

    def __getattr__(self, name):
        def call(*_args, **_kwargs):
            with self.lock:
                self.counts[name] = self.counts.get(name, 0) + 1
            return lambda: None

        return call


def memory_monitor(stop_event, result):
    process = psutil.Process()
    while not stop_event.wait(0.5):
        rss = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        result["peak_rss_bytes"] = max(result.get("peak_rss_bytes", 0), rss)


def run():
    videos = sorted(path for path in (PROJECT_DIR / "input_videos").iterdir() if path.is_file())
    if len(videos) != 4:
        raise RuntimeError(f"長時間測試要求剛好四支影片，目前為 {len(videos)} 支")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = PROJECT_DIR / "captures" / f"P2_LONG_TEST_{run_id}"
    output_root.mkdir(parents=True, exist_ok=False)

    main.CONFIG.CAPTURES_DIR = str(output_root)
    main.video_queue = [str(path.resolve()) for path in videos]
    main.engine_mode = "auto"
    main.stop_requested = False
    main.skip_video_path = None
    main.global_live_settings = {}
    main.roi_points = []
    main.scale_info = None
    main.player_state = {
        "playing": False,
        "reverse": False,
        "speed": 1.0,
        "seek_req": None,
        "step_req": 0,
        "manual_capture_req": False,
    }
    headless_eel = HeadlessEel()
    main.eel = headless_eel

    settings = {
        "aiModel": str((PROJECT_DIR / "yolov8n.pt").resolve()),
        "trackerMode": "bytetrack",
        "confThresh": 0.90,
        "captureMode": "雙格蒐證模式 (起點+最清晰)",
        "classes": {"0": True, "1": True, "2": True, "3": True, "5": True, "7": True},
        "fastMode": True,
        "filterStationary": True,
        "skipSec": 5.0,
        "singleFolder": True,
    }

    metrics = {"peak_rss_bytes": 0}
    monitor_stop = threading.Event()
    monitor = threading.Thread(target=memory_monitor, args=(monitor_stop, metrics), daemon=True)
    started = time.monotonic()
    monitor.start()
    try:
        main.batch_processing_worker(settings)
    finally:
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        monitor_stop.set()
        monitor.join(timeout=3)

    reports = list(output_root.rglob("系統鑑識紀錄.txt"))
    if len(reports) != 1:
        raise RuntimeError(f"預期一份鑑識紀錄，實際為 {len(reports)} 份")
    report_text = reports[0].read_text(encoding="utf-8")
    completed = [video.name for video in videos if f"完成 {video.name}" in report_text]
    sha_records = report_text.count("SHA-256:")
    screenshots = list(output_root.rglob("*.jpg"))

    result = {
        "run_id": run_id,
        "status": "passed" if len(completed) == 4 and sha_records == 4 else "failed",
        "settings": settings,
        "videos": [{"name": video.name, "size": video.stat().st_size} for video in videos],
        "completed_videos": completed,
        "sha256_record_count": sha_records,
        "screenshot_count": len(screenshots),
        "report_path": str(reports[0]),
        "ui_event_counts": headless_eel.counts,
        **metrics,
    }
    result_path = output_root / "long_batch_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "passed":
        raise RuntimeError("長時間批次驗證未通過，請檢查測試報告")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
