import multiprocessing
import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))


def _spawn_context_probe(result_queue):
    import main

    main._configure_worker_context("manual")
    result_queue.put(main.engine_mode)


def _spawn_manual_video(video_path, output_dir, ui_queue, shared_state):
    import main

    settings = {
        "aiModel": "yolov8n.pt",
        "confThresh": 0.40,
        "classes": {"0": True, "1": True, "2": True, "3": True, "5": True, "7": True},
        "executionMode": "preview",
        "burnAnnotations": False,
        "skipSec": 0.2,
        "singleFolder": False,
    }
    main.process_wrapper(
        video_path,
        Path(video_path).name,
        settings,
        output_dir,
        ui_queue,
        shared_state,
        "yolov8n.pt",
        "manual",
    )


def _spawn_auto_video(video_path, output_dir, ui_queue, shared_state):
    import main

    settings = {
        "aiModel": "yolov8n.pt",
        "trackerMode": "botsort_reid",
        "confThresh": 0.40,
        "classes": {"0": True, "1": True, "2": True, "3": True, "5": True, "7": True},
        "executionMode": "preview",
        "burnAnnotations": False,
        "skipSec": 0.2,
        "singleFolder": False,
    }
    main.process_wrapper(
        video_path,
        Path(video_path).name,
        settings,
        output_dir,
        ui_queue,
        shared_state,
        "yolov8n.pt",
        "auto",
    )


def _spawn_headless_video(video_path, output_dir, ui_queue, shared_state):
    import main

    settings = {
        "aiModel": "yolov8n.pt",
        "trackerMode": "bytetrack",
        "confThresh": 0.90,
        "classes": {"0": True, "1": True, "2": True, "3": True, "5": True, "7": True},
        "executionMode": "headless",
        "burnAnnotations": False,
        "skipSec": 0.2,
        "singleFolder": False,
        "inferenceSize": 640,
    }
    main.process_wrapper(
        video_path,
        Path(video_path).name,
        settings,
        output_dir,
        ui_queue,
        shared_state,
        "yolov8n.pt",
        "auto",
    )


class WorkerContextTests(unittest.TestCase):
    @staticmethod
    def _shared_state(manager, playing=False, manual_capture=False):
        return manager.dict({
            "stop_requested": False,
            "force_stop_requested": False,
            "skip_video_path": None,
            "player_state": {
                "playing": playing,
                "reverse": False,
                "speed": 1.0,
                "seek_req": None,
                "step_req": 0,
                "manual_capture_req": manual_capture,
            },
            "live_settings": {},
            "roi_points": [],
            "scale_info": None,
            "writer_stats": {},
        })

    def test_spawn_worker_receives_mode_without_report_context(self):
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()

        process = context.Process(target=_spawn_context_probe, args=(result_queue,))
        process.start()
        process.join(timeout=90)

        self.assertFalse(process.is_alive(), "測試子程序未在期限內結束")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(result_queue.get(timeout=5), "manual")

    def test_manual_mode_pauses_on_cached_synthetic_frame_and_captures_once(self):
        context = multiprocessing.get_context("spawn")
        manager = context.Manager()
        ui_queue = manager.Queue()
        shared_state = self._shared_state(manager, playing=False, manual_capture=True)

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            video_path = temp_path / "人工點視快取測試.avi"
            output_dir = temp_path / "captures"
            output_dir.mkdir()
            writer = cv2.VideoWriter(
                str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (96, 64)
            )
            self.assertTrue(writer.isOpened(), "無法建立合成測試影片")
            for index in range(12):
                frame = np.full((64, 96, 3), index * 10, dtype=np.uint8)
                writer.write(frame)
            writer.release()

            process = context.Process(
                target=_spawn_manual_video,
                args=(str(video_path), str(output_dir), ui_queue, shared_state),
            )
            process.start()
            preview_received = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.is_alive():
                try:
                    name, _args, _kwargs = ui_queue.get(timeout=1)
                    if name == "setPreviewImage":
                        preview_received = True
                        time.sleep(0.25)
                        break
                except queue.Empty:
                    continue

            shared_state["stop_requested"] = True
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

            self.assertTrue(preview_received, "人工點視未顯示合成影片畫格")
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(list(output_dir.glob("*.jpg"))), 1, "單次手動快門不應重複觸發")
        manager.shutdown()

    def test_headless_mode_analyzes_synthetic_video_without_preview_frames(self):
        context = multiprocessing.get_context("spawn")
        manager = context.Manager()
        ui_queue = manager.Queue()
        shared_state = self._shared_state(manager)

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            video_path = temp_path / "Headless快篩測試.avi"
            output_dir = temp_path / "captures"
            output_dir.mkdir()
            writer = cv2.VideoWriter(
                str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (96, 64)
            )
            self.assertTrue(writer.isOpened(), "無法建立 Headless 合成測試影片")
            for _ in range(12):
                writer.write(np.zeros((64, 96, 3), dtype=np.uint8))
            writer.release()

            process = context.Process(
                target=_spawn_headless_video,
                args=(str(video_path), str(output_dir), ui_queue, shared_state),
            )
            process.start()
            process.join(timeout=90)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

            self.assertEqual(process.exitcode, 0)
            messages = []
            while True:
                try:
                    messages.append(ui_queue.get_nowait())
                except queue.Empty:
                    break
            self.assertFalse(any(name == "setPreviewImage" for name, _args, _kwargs in messages))
        manager.shutdown()

    def test_manual_mode_decodes_real_video_in_spawn_worker(self):
        videos = sorted((PROJECT_DIR / "input_videos").glob("*"))
        if not videos:
            self.skipTest("input_videos 內沒有實體測試影片")

        context = multiprocessing.get_context("spawn")
        manager = context.Manager()
        ui_queue = manager.Queue()
        shared_state = self._shared_state(manager, playing=True, manual_capture=True)

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            process = context.Process(
                target=_spawn_manual_video,
                args=(str(videos[0]), temp_dir, ui_queue, shared_state),
            )
            process.start()
            preview_received = False
            deadline = time.monotonic() + 120

            while time.monotonic() < deadline and process.is_alive():
                try:
                    name, _args, _kwargs = ui_queue.get(timeout=1)
                    if name == "setPreviewImage":
                        preview_received = True
                        shared_state["stop_requested"] = True
                        break
                except queue.Empty:
                    continue

            shared_state["stop_requested"] = True
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

            self.assertTrue(preview_received, "人工點視子程序沒有送出實體影片預覽幀")
            self.assertEqual(process.exitcode, 0)
            captures = list(Path(temp_dir).glob("*.jpg"))
            self.assertTrue(captures, "人工點視沒有產生手動全景快門")
            self.assertFalse(list(Path(temp_dir).glob("*.jsonl")))
            self.assertFalse(list(Path(temp_dir).glob("*鑑識紀錄*.txt")))
        manager.shutdown()

    def test_auto_mode_analyzes_real_video_in_spawn_worker(self):
        videos = sorted((PROJECT_DIR / "input_videos").glob("*"))
        if not videos:
            self.skipTest("input_videos 內沒有實體測試影片")

        context = multiprocessing.get_context("spawn")
        manager = context.Manager()
        ui_queue = manager.Queue()
        shared_state = self._shared_state(manager)

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            process = context.Process(
                target=_spawn_auto_video,
                args=(str(videos[0]), temp_dir, ui_queue, shared_state),
            )
            process.start()
            preview_received = False
            deadline = time.monotonic() + 120

            while time.monotonic() < deadline and process.is_alive():
                try:
                    name, _args, _kwargs = ui_queue.get(timeout=1)
                    if name == "setPreviewImage":
                        preview_received = True
                        shared_state["stop_requested"] = True
                        break
                except queue.Empty:
                    continue

            shared_state["stop_requested"] = True
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

            self.assertTrue(preview_received, "自動分析子程序沒有送出實體影片預覽幀")
            self.assertEqual(process.exitcode, 0)
        manager.shutdown()

    def test_corrupt_video_isolated_to_failed_spawn_worker(self):
        context = multiprocessing.get_context("spawn")
        manager = context.Manager()
        ui_queue = manager.Queue()
        shared_state = self._shared_state(manager)

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            corrupt_path = Path(temp_dir) / "毀損證物.avi"
            corrupt_path.write_bytes(b"not-a-video")
            process = context.Process(
                target=_spawn_auto_video,
                args=(str(corrupt_path), temp_dir, ui_queue, shared_state),
            )
            process.start()
            process.join(timeout=90)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

            self.assertNotEqual(process.exitcode, 0)
            messages = []
            while True:
                try:
                    messages.append(ui_queue.get_nowait())
                except queue.Empty:
                    break
            self.assertTrue(any(name == "appendLog" for name, _args, _kwargs in messages))
        manager.shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
