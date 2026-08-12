import multiprocessing
import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))


def _spawn_context_probe(result_queue, report_path):
    import main

    main._configure_worker_context("manual", report_path)
    main.write_report("跨程序鑑識紀錄測試")
    result_queue.put((main.engine_mode, main.current_report_path))


def _spawn_manual_video(video_path, output_dir, ui_queue, shared_state, report_path):
    import main

    settings = {
        "aiModel": "yolov8n.pt",
        "confThresh": 0.40,
        "captureMode": "雙格蒐證模式 (起點+最清晰)",
        "classes": {"0": True, "1": True, "2": True, "3": True, "5": True, "7": True},
        "fastMode": False,
        "filterStationary": True,
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
        report_path,
    )


def _spawn_auto_video(video_path, output_dir, ui_queue, shared_state, report_path):
    import main

    settings = {
        "aiModel": "yolov8n.pt",
        "trackerMode": "botsort_reid",
        "confThresh": 0.40,
        "captureMode": "雙格蒐證模式 (起點+最清晰)",
        "classes": {"0": True, "1": True, "2": True, "3": True, "5": True, "7": True},
        "fastMode": False,
        "filterStationary": True,
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
        report_path,
    )


class WorkerContextTests(unittest.TestCase):
    @staticmethod
    def _shared_state(manager, playing=False, manual_capture=False):
        return manager.dict({
            "stop_requested": False,
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
        })

    def test_spawn_worker_receives_mode_and_report_path(self):
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            report_path = os.path.join(temp_dir, "系統鑑識紀錄.txt")
            process = context.Process(
                target=_spawn_context_probe,
                args=(result_queue, report_path),
            )
            process.start()
            process.join(timeout=90)

            self.assertFalse(process.is_alive(), "測試子程序未在期限內結束")
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(result_queue.get(timeout=5), ("manual", report_path))
            self.assertIn("跨程序鑑識紀錄測試", Path(report_path).read_text(encoding="utf-8"))

    def test_manual_mode_decodes_real_video_in_spawn_worker(self):
        videos = sorted((PROJECT_DIR / "input_videos").glob("*"))
        if not videos:
            self.skipTest("input_videos 內沒有實體測試影片")

        context = multiprocessing.get_context("spawn")
        manager = context.Manager()
        ui_queue = manager.Queue()
        shared_state = self._shared_state(manager, playing=True, manual_capture=True)

        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            report_path = os.path.join(temp_dir, "實體影片鑑識紀錄.txt")
            process = context.Process(
                target=_spawn_manual_video,
                args=(str(videos[0]), temp_dir, ui_queue, shared_state, report_path),
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
            self.assertTrue(list(Path(temp_dir).glob("*.jpg")), "人工點視沒有產生手動快門證物")
            self.assertIn("[截圖]", Path(report_path).read_text(encoding="utf-8"))
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
            report_path = os.path.join(temp_dir, "自動分析鑑識紀錄.txt")
            process = context.Process(
                target=_spawn_auto_video,
                args=(str(videos[0]), temp_dir, ui_queue, shared_state, report_path),
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
            report_path = os.path.join(temp_dir, "毀損鑑識紀錄.txt")
            process = context.Process(
                target=_spawn_auto_video,
                args=(str(corrupt_path), temp_dir, ui_queue, shared_state, report_path),
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
