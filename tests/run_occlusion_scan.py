"""掃描長時間偵測間隔，尋找 BoT-SORT ReID 改善的遮蔽候選。"""

import json
import multiprocessing
import os
import sys
from datetime import datetime
from pathlib import Path

import cv2


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("YOLO_CONFIG_DIR", str(PROJECT_DIR / "captures" / ".ultralytics"))

VIDEO_NAME = "CH09-20260326-173728-184505.avi"
START_SECONDS = 1030.0
DURATION_SECONDS = 120.0
MIN_GAP_SECONDS = 3.0
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


def run_tracker(tracker_mode, output_path):
    from ultralytics import YOLO

    import main

    video_path = PROJECT_DIR / "input_videos" / VIDEO_NAME
    _mode, tracker_path = main.resolve_tracker_config({"trackerMode": tracker_mode})
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"無法開啟掃描影片: {video_path}")
    capture.set(cv2.CAP_PROP_POS_MSEC, START_SECONDS * 1000.0)
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    maximum_frames = round(DURATION_SECONDS * fps)
    model = YOLO(str(PROJECT_DIR / "yolov8n.pt"))
    observations = []
    try:
        for frame_index in range(maximum_frames):
            ok, frame = capture.read()
            if not ok:
                break
            result = model.track(
                frame,
                persist=True,
                tracker=tracker_path,
                conf=0.40,
                classes=sorted(TARGET_CLASSES),
                verbose=False,
            )[0]
            frame_tracks = []
            boxes = result.boxes
            if boxes is not None and boxes.id is not None:
                coordinates = boxes.xyxy.cpu().numpy()
                classes = boxes.cls.cpu().numpy().astype(int)
                ids = boxes.id.cpu().numpy().astype(int)
                for box, class_id, track_id in zip(coordinates, classes, ids):
                    frame_tracks.append(
                        {"track_id": int(track_id), "class_id": int(class_id), "box": box.tolist()}
                    )
            observations.append(frame_tracks)
    finally:
        capture.release()
    output_path.write_text(
        json.dumps(
            {"tracker_mode": tracker_mode, "fps": fps, "observations": observations},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def best_match(frame_tracks, target):
    matches = [
        (box_iou(track["box"], target["box"]), track)
        for track in frame_tracks
        if track["class_id"] == target["class_id"]
    ]
    return max(matches, default=(0.0, None), key=lambda item: item[0])


def find_candidates(byte_data, bot_data):
    fps = bot_data["fps"]
    minimum_gap_frames = round(MIN_GAP_SECONDS * fps)
    last_seen = {}
    candidates = []
    for frame_index, tracks in enumerate(bot_data["observations"]):
        for track in tracks:
            key = (track["class_id"], track["track_id"])
            if key in last_seen:
                previous_frame, previous_track = last_seen[key]
                gap_frames = frame_index - previous_frame - 1
                if gap_frames >= minimum_gap_frames:
                    byte_before_iou, byte_before = best_match(
                        byte_data["observations"][previous_frame], previous_track
                    )
                    byte_after_iou, byte_after = best_match(byte_data["observations"][frame_index], track)
                    candidates.append(
                        {
                            "class_id": track["class_id"],
                            "botsort_track_id": track["track_id"],
                            "previous_frame": previous_frame,
                            "current_frame": frame_index,
                            "gap_frames": gap_frames,
                            "gap_seconds": round(gap_frames / fps, 3),
                            "video_seconds_before": round(START_SECONDS + previous_frame / fps, 3),
                            "video_seconds_after": round(START_SECONDS + frame_index / fps, 3),
                            "bytetrack_id_before": byte_before["track_id"] if byte_before else None,
                            "bytetrack_id_after": byte_after["track_id"] if byte_after else None,
                            "bytetrack_iou_before": round(byte_before_iou, 4),
                            "bytetrack_iou_after": round(byte_after_iou, 4),
                            "botsort_box_before": previous_track["box"],
                            "botsort_box_after": track["box"],
                        }
                    )
            last_seen[key] = (frame_index, track)
    return candidates


def main_entry():
    output_dir = PROJECT_DIR / "captures" / f"P3_OCCLUSION_SCAN_{datetime.now():%Y%m%d_%H%M%S}"
    output_dir.mkdir(parents=True, exist_ok=False)
    context = multiprocessing.get_context("spawn")
    data_paths = {}
    for tracker_mode in ("bytetrack", "botsort_reid"):
        output_path = output_dir / f"{tracker_mode}_observations.json"
        worker = context.Process(target=run_tracker, args=(tracker_mode, output_path))
        worker.start()
        worker.join(timeout=1800)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=10)
            raise TimeoutError(f"{tracker_mode} 遮蔽掃描超過 30 分鐘")
        if worker.exitcode != 0 or not output_path.is_file():
            raise RuntimeError(f"{tracker_mode} 遮蔽掃描失敗，離開代碼 {worker.exitcode}")
        data_paths[tracker_mode] = output_path

    byte_data = json.loads(data_paths["bytetrack"].read_text(encoding="utf-8"))
    bot_data = json.loads(data_paths["botsort_reid"].read_text(encoding="utf-8"))
    candidates = find_candidates(byte_data, bot_data)
    useful_candidates = [
        candidate
        for candidate in candidates
        if candidate["bytetrack_id_before"] is not None
        and candidate["bytetrack_id_after"] is not None
        and candidate["bytetrack_id_before"] != candidate["bytetrack_id_after"]
        and candidate["bytetrack_iou_before"] >= 0.50
        and candidate["bytetrack_iou_after"] >= 0.50
    ]
    report = {
        "status": "passed",
        "video": VIDEO_NAME,
        "start_seconds": START_SECONDS,
        "duration_seconds": DURATION_SECONDS,
        "minimum_gap_seconds": MIN_GAP_SECONDS,
        "candidate_count": len(candidates),
        "useful_candidate_count": len(useful_candidates),
        "useful_candidates": useful_candidates,
    }
    report_path = output_dir / "occlusion_scan.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report_path={report_path}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main_entry()
