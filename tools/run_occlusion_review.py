"""使用遮蔽掃描既有 JSON 產生候選事件前／中／後覆核圖，不重跑模型。"""

import json
from pathlib import Path

import cv2


PROJECT_DIR = Path(__file__).resolve().parents[1]
VIDEO_PATH = PROJECT_DIR / "input_videos" / "CH09-20260326-173728-184505.avi"


def draw_target(frame, track, title, color):
    annotated = frame.copy()
    cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 42), (0, 0, 0), -1)
    cv2.putText(annotated, title, (12, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2)
    if track:
        x1, y1, x2, y2 = map(int, track["box"])
        label = f"ID:{track['track_id']} class:{track['class_id']}"
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 4)
        cv2.putText(annotated, label, (x1, max(68, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return annotated


def nearest_target(tracks, class_id, reference_box):
    reference_center = ((reference_box[0] + reference_box[2]) / 2, (reference_box[1] + reference_box[3]) / 2)
    candidates = [track for track in tracks if track["class_id"] == class_id]
    return min(
        candidates,
        default=None,
        key=lambda track: (
            ((track["box"][0] + track["box"][2]) / 2 - reference_center[0]) ** 2
            + ((track["box"][1] + track["box"][3]) / 2 - reference_center[1]) ** 2
        ),
    )


def read_video_frames(start_seconds, frame_indices):
    capture = cv2.VideoCapture(str(VIDEO_PATH))
    if not capture.isOpened():
        raise RuntimeError(f"無法開啟覆核影片: {VIDEO_PATH}")
    capture.set(cv2.CAP_PROP_POS_MSEC, start_seconds * 1000.0)
    wanted = set(frame_indices)
    frames = {}
    try:
        for frame_index in range(max(wanted) + 1):
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f"影片於覆核幀 {frame_index} 前結束")
            if frame_index in wanted:
                frames[frame_index] = frame
    finally:
        capture.release()
    return frames


def main_entry():
    scan_dirs = sorted((PROJECT_DIR / "captures").glob("P3_OCCLUSION_SCAN_*"))
    if not scan_dirs:
        raise RuntimeError("找不到遮蔽掃描資料夾")
    scan_dir = scan_dirs[-1]
    report = json.loads((scan_dir / "occlusion_scan.json").read_text(encoding="utf-8"))
    byte_data = json.loads((scan_dir / "bytetrack_observations.json").read_text(encoding="utf-8"))
    bot_data = json.loads((scan_dir / "botsort_reid_observations.json").read_text(encoding="utf-8"))
    candidates = report["useful_candidates"]
    frame_indices = []
    for candidate in candidates:
        frame_indices.extend(
            [
                candidate["previous_frame"],
                (candidate["previous_frame"] + candidate["current_frame"]) // 2,
                candidate["current_frame"],
            ]
        )
    frames = read_video_frames(report["start_seconds"], frame_indices)

    outputs = []
    for candidate_index, candidate in enumerate(candidates, start=1):
        before = candidate["previous_frame"]
        middle = (before + candidate["current_frame"]) // 2
        after = candidate["current_frame"]
        class_id = candidate["class_id"]
        bot_id = candidate["botsort_track_id"]
        bot_before = next(
            (track for track in bot_data["observations"][before] if track["track_id"] == bot_id), None
        )
        bot_after = next(
            (track for track in bot_data["observations"][after] if track["track_id"] == bot_id), None
        )
        byte_before = nearest_target(
            byte_data["observations"][before], class_id, candidate["botsort_box_before"]
        )
        byte_after = nearest_target(byte_data["observations"][after], class_id, candidate["botsort_box_after"])

        byte_row = cv2.hconcat(
            [
                draw_target(frames[before], byte_before, f"Byte before f{before}", (0, 255, 255)),
                draw_target(frames[middle], None, f"gap middle f{middle}", (0, 255, 255)),
                draw_target(frames[after], byte_after, f"Byte after f{after}", (0, 255, 255)),
            ]
        )
        bot_row = cv2.hconcat(
            [
                draw_target(frames[before], bot_before, f"ReID before f{before}", (0, 220, 0)),
                draw_target(frames[middle], None, f"gap middle f{middle}", (0, 220, 0)),
                draw_target(frames[after], bot_after, f"ReID after f{after}", (0, 220, 0)),
            ]
        )
        sheet = cv2.vconcat([byte_row, bot_row])
        sheet_path = scan_dir / f"candidate_{candidate_index}_review.jpg"
        if not cv2.imwrite(str(sheet_path), sheet):
            raise RuntimeError(f"無法寫入候選覆核圖: {sheet_path}")
        outputs.append(str(sheet_path))

    print(f"scan_dir={scan_dir}")
    for output in outputs:
        print(f"review={output}")


if __name__ == "__main__":
    main_entry()
