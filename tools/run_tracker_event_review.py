"""輸出 P3 基準 ID 變更事件前後畫面，供人工覆核。"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import cv2


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("YOLO_CONFIG_DIR", str(PROJECT_DIR / "captures" / ".ultralytics"))

VIDEO_PATH = PROJECT_DIR / "input_videos" / "CH09-20260326-173728-184505.avi"
START_SECONDS = 1040.0
EVENT_FRAME = 121
REVIEW_FRAMES = set(range(EVENT_FRAME - 4, EVENT_FRAME + 5))
TARGET_CLASSES = {0, 1, 2, 3, 5, 7}


def render_tracker(tracker_mode, output_dir):
    from ultralytics import YOLO

    import main

    _mode, tracker_path = main.resolve_tracker_config({"trackerMode": tracker_mode})
    capture = cv2.VideoCapture(str(VIDEO_PATH))
    if not capture.isOpened():
        raise RuntimeError(f"無法開啟事件影片: {VIDEO_PATH}")
    capture.set(cv2.CAP_PROP_POS_MSEC, START_SECONDS * 1000.0)
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    model = YOLO(str(PROJECT_DIR / "yolov8n.pt"))
    tracker_dir = output_dir / tracker_mode
    tracker_dir.mkdir(parents=True, exist_ok=False)
    records = []
    rendered_frames = []

    try:
        for frame_index in range(max(REVIEW_FRAMES) + 1):
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f"事件片段在第 {frame_index} 幀提前結束")
            result = model.track(
                frame,
                persist=True,
                tracker=tracker_path,
                conf=0.40,
                classes=sorted(TARGET_CLASSES),
                verbose=False,
            )[0]
            if frame_index not in REVIEW_FRAMES:
                continue

            annotated = frame.copy()
            frame_tracks = []
            boxes = result.boxes
            if boxes is not None and boxes.id is not None:
                coordinates = boxes.xyxy.cpu().numpy()
                classes = boxes.cls.cpu().numpy().astype(int)
                ids = boxes.id.cpu().numpy().astype(int)
                for box, class_id, track_id in zip(coordinates, classes, ids):
                    x1, y1, x2, y2 = map(int, box)
                    color = (0, 255, 255) if class_id == 7 else (0, 220, 0)
                    label = f"ID:{track_id} {main.CONFIG.TARGET_CLASSES.get(class_id, class_id)}"
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 3)
                    cv2.putText(
                        annotated,
                        label,
                        (x1, max(24, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        color,
                        2,
                    )
                    frame_tracks.append(
                        {"track_id": int(track_id), "class_id": int(class_id), "box": box.tolist()}
                    )

            video_seconds = START_SECONDS + frame_index / fps
            title = f"{tracker_mode} | frame {frame_index} | video {video_seconds:.3f}s"
            cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 42), (0, 0, 0), -1)
            cv2.putText(annotated, title, (12, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2)
            frame_path = tracker_dir / f"frame_{frame_index:03d}.jpg"
            if not cv2.imwrite(str(frame_path), annotated):
                raise RuntimeError(f"無法寫入覆核影像: {frame_path}")
            records.append({"frame_index": frame_index, "video_seconds": video_seconds, "tracks": frame_tracks})
            rendered_frames.append(annotated)
    finally:
        capture.release()

    tile_width = 480
    tiles = []
    for frame in rendered_frames:
        scale = tile_width / frame.shape[1]
        tiles.append(cv2.resize(frame, (tile_width, round(frame.shape[0] * scale))))
    rows = [cv2.hconcat(tiles[index : index + 3]) for index in range(0, len(tiles), 3)]
    contact_sheet = cv2.vconcat(rows)
    contact_path = output_dir / f"{tracker_mode}_contact_sheet.jpg"
    if not cv2.imwrite(str(contact_path), contact_sheet):
        raise RuntimeError(f"無法寫入覆核總覽: {contact_path}")
    return {"tracker_mode": tracker_mode, "contact_sheet": str(contact_path), "frames": records}


def main_entry():
    output_dir = PROJECT_DIR / "captures" / f"P3_ID_SWITCH_REVIEW_{datetime.now():%Y%m%d_%H%M%S}"
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "video": str(VIDEO_PATH),
        "event_frame": EVENT_FRAME,
        "review_frames": sorted(REVIEW_FRAMES),
        "results": [
            render_tracker("bytetrack", output_dir),
            render_tracker("botsort_reid", output_dir),
        ],
    }
    report_path = output_dir / "event_review.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report_path={report_path}")
    for result in report["results"]:
        print(f"contact_sheet={result['contact_sheet']}")


if __name__ == "__main__":
    main_entry()
