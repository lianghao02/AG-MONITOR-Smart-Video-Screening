"""在同一實體片段比較 ByteTrack 與 BoT-SORT ReID 的追蹤代理指標。"""

import json
import multiprocessing
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import psutil


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("YOLO_CONFIG_DIR", str(PROJECT_DIR / "captures" / ".ultralytics"))


VIDEO_NAME = "CH09-20260326-173728-184505.avi"
START_SECONDS = 1040.0
DURATION_SECONDS = 20.0
CONFIDENCE = 0.40
TARGET_CLASSES = {0, 1, 2, 3, 5, 7}


def box_iou(box_a, box_b):
    left = max(box_a[0], box_b[0])
    top = max(box_a[1], box_b[1])
    right = min(box_a[2], box_b[2])
    bottom = min(box_a[3], box_b[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area_a = max(0.0, box_a[2] - box_a[0]) * max(0.0, box_a[3] - box_a[1])
    area_b = max(0.0, box_b[2] - box_b[0]) * max(0.0, box_b[3] - box_b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def adjacent_id_changes(previous, current, threshold=0.50):
    """以同類別相鄰幀 IoU 配對估算 ID 變更；此值不是人工標註的 ID Switch。"""
    candidates = []
    for previous_index, old in enumerate(previous):
        for current_index, new in enumerate(current):
            if old["class_id"] != new["class_id"]:
                continue
            overlap = box_iou(old["box"], new["box"])
            if overlap >= threshold:
                candidates.append((overlap, previous_index, current_index))
    changes = 0
    events = []
    matched_previous = set()
    matched_current = set()
    for _overlap, previous_index, current_index in sorted(candidates, reverse=True):
        if previous_index in matched_previous or current_index in matched_current:
            continue
        matched_previous.add(previous_index)
        matched_current.add(current_index)
        if previous[previous_index]["track_id"] != current[current_index]["track_id"]:
            changes += 1
            events.append(
                {
                    "class_id": current[current_index]["class_id"],
                    "previous_track_id": previous[previous_index]["track_id"],
                    "current_track_id": current[current_index]["track_id"],
                    "iou": round(_overlap, 4),
                    "previous_box": previous[previous_index]["box"],
                    "current_box": current[current_index]["box"],
                }
            )
    return changes, len(matched_current), events


def run_tracker(tracker_mode, result_queue):
    from ultralytics import YOLO

    import main

    video_path = PROJECT_DIR / "input_videos" / VIDEO_NAME
    _mode, tracker_path = main.resolve_tracker_config({"trackerMode": tracker_mode})
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"無法開啟基準影片: {video_path}")
    capture.set(cv2.CAP_PROP_POS_MSEC, START_SECONDS * 1000.0)
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    maximum_frames = max(1, round(DURATION_SECONDS * fps))
    model = YOLO(str(PROJECT_DIR / "yolov8n.pt"))
    process = psutil.Process()

    started = time.perf_counter()
    frame_count = 0
    detection_count = 0
    idless_count = 0
    id_change_proxy = 0
    adjacent_matches = 0
    switch_events = []
    peak_rss = process.memory_info().rss
    track_lengths = {}
    previous = []

    try:
        while frame_count < maximum_frames:
            ok, frame = capture.read()
            if not ok:
                break
            result = model.track(
                frame,
                persist=True,
                tracker=tracker_path,
                conf=CONFIDENCE,
                classes=sorted(TARGET_CLASSES),
                verbose=False,
            )[0]
            current = []
            boxes = result.boxes
            if boxes is not None and len(boxes):
                coordinates = boxes.xyxy.cpu().numpy()
                classes = boxes.cls.cpu().numpy().astype(int)
                ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else np.full(len(boxes), -1)
                for coordinates_item, class_id, track_id in zip(coordinates, classes, ids):
                    detection_count += 1
                    if track_id < 0:
                        idless_count += 1
                        continue
                    track_lengths[int(track_id)] = track_lengths.get(int(track_id), 0) + 1
                    current.append(
                        {
                            "box": coordinates_item.tolist(),
                            "class_id": int(class_id),
                            "track_id": int(track_id),
                        }
                    )
            changes, matches, frame_events = adjacent_id_changes(previous, current)
            for event in frame_events:
                event["frame_index"] = frame_count
                event["video_seconds"] = round(START_SECONDS + frame_count / fps, 3)
                switch_events.append(event)
            id_change_proxy += changes
            adjacent_matches += matches
            previous = current
            frame_count += 1
            peak_rss = max(peak_rss, process.memory_info().rss)
    finally:
        capture.release()

    elapsed = time.perf_counter() - started
    short_tracks = sum(1 for length in track_lengths.values() if length <= 3)
    result_queue.put(
        {
            "tracker_mode": tracker_mode,
            "tracker_path": tracker_path,
            "frames": frame_count,
            "video_fps": fps,
            "elapsed_seconds": round(elapsed, 3),
            "processing_fps": round(frame_count / elapsed, 3) if elapsed else 0.0,
            "peak_rss_bytes": peak_rss,
            "detections": detection_count,
            "idless_detections": idless_count,
            "unique_track_ids": len(track_lengths),
            "short_tracks_le_3_frames": short_tracks,
            "adjacent_iou_matches": adjacent_matches,
            "adjacent_id_change_proxy": id_change_proxy,
            "switch_events": switch_events,
            "mean_track_length_frames": round(sum(track_lengths.values()) / len(track_lengths), 3)
            if track_lengths
            else 0.0,
        }
    )


def main_entry():
    context = multiprocessing.get_context("spawn")
    benchmark_results = []
    run_orders = (("bytetrack", "botsort_reid"), ("botsort_reid", "bytetrack"))
    for round_number, tracker_order in enumerate(run_orders, start=1):
        for order_number, tracker_mode in enumerate(tracker_order, start=1):
            result_queue = context.Queue()
            worker = context.Process(target=run_tracker, args=(tracker_mode, result_queue))
            worker.start()
            worker.join(timeout=1800)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
                raise TimeoutError(f"{tracker_mode} 基準超過 30 分鐘")
            if worker.exitcode != 0:
                raise RuntimeError(f"{tracker_mode} 基準失敗，離開代碼 {worker.exitcode}")
            result = result_queue.get(timeout=10)
            result.update(round=round_number, order=order_number)
            benchmark_results.append(result)

    summaries = []
    for tracker_mode in ("bytetrack", "botsort_reid"):
        runs = [result for result in benchmark_results if result["tracker_mode"] == tracker_mode]
        summaries.append(
            {
                "tracker_mode": tracker_mode,
                "run_count": len(runs),
                "median_processing_fps": round(statistics.median(run["processing_fps"] for run in runs), 3),
                "max_peak_rss_bytes": max(run["peak_rss_bytes"] for run in runs),
                "unique_track_ids": [run["unique_track_ids"] for run in runs],
                "short_tracks_le_3_frames": [run["short_tracks_le_3_frames"] for run in runs],
                "adjacent_id_change_proxy": [run["adjacent_id_change_proxy"] for run in runs],
                "mean_track_length_frames": [run["mean_track_length_frames"] for run in runs],
            }
        )

    output_dir = PROJECT_DIR / "captures" / f"P3_TRACKER_BENCHMARK_{datetime.now():%Y%m%d_%H%M%S}"
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "passed",
        "video": VIDEO_NAME,
        "start_seconds": START_SECONDS,
        "duration_seconds": DURATION_SECONDS,
        "confidence": CONFIDENCE,
        "metric_warning": "adjacent_id_change_proxy 為相鄰幀 IoU 代理指標，不等同人工標註的 ID Switch。",
        "run_orders": run_orders,
        "results": benchmark_results,
        "summaries": summaries,
    }
    report_path = output_dir / "tracker_benchmark.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report_path={report_path}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main_entry()
