import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import main


class ForensicCoreTests(unittest.TestCase):
    def test_product_branding_uses_smart_video_screening_identity(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
        run_script = (PROJECT_DIR / "RUN.bat").read_text(encoding="utf-8")
        favicon = (PROJECT_DIR / "web" / "favicon.svg").read_text(encoding="utf-8")

        self.assertIn("AG-MONITOR 智慧影像快篩系統", html)
        self.assertIn("AG-MONITOR-Smart-Video-Screening", readme)
        self.assertIn("AG-MONITOR Smart Video Screening", run_script)
        self.assertIn('href="favicon.svg"', html)
        self.assertIn("智慧影像快篩系統", favicon)
        for retired_label in ("AG-Forensic-Player", "科技偵查戰術播放器", "智慧雙軌鑑識系統"):
            self.assertNotIn(retired_label, html)

    def test_tracker_config_uses_project_allowlist(self):
        mode, config_path = main.resolve_tracker_config({"trackerMode": "botsort_reid"})
        self.assertEqual(mode, "botsort_reid")
        self.assertEqual(Path(config_path), PROJECT_DIR / "trackers" / "ag_botsort_reid.yaml")
        config_text = Path(config_path).read_text(encoding="utf-8")
        self.assertIn("with_reid: True", config_text)
        self.assertIn("model: auto", config_text)
        with self.assertRaisesRegex(ValueError, "不支援的追蹤核心"):
            main.resolve_tracker_config({"trackerMode": "../../惡意設定.yaml"})

    def test_reid_keeps_tracking_alive_across_longer_occlusion(self):
        self.assertEqual(main.tracker_occlusion_grace_seconds("bytetrack"), 1.5)
        self.assertEqual(main.tracker_occlusion_grace_seconds("botsort_reid"), 6.0)
        source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("no_target_frames < occlusion_grace_frames", source)
        self.assertNotIn("no_target_frames < int(fps * 1.5)", source)

    def test_frontend_sends_and_locks_tracker_mode(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="trackerMode"', html)
        self.assertIn("trackerMode: document.getElementById(\"trackerMode\").value", html)
        self.assertIn("setProcessingControls(true)", html)
        self.assertIn("#trackerMode", html)

    def test_frontend_defaults_to_available_weight_and_reports_drag_diagnostics(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("prepareDefaultModel", html)
        self.assertIn("ensure_model_weight('yolo11n.pt')", html)
        self.assertIn("text/uri-list", html)
        self.assertIn("拖曳偵測：", html)
        self.assertIn("await addVideos()", html)

    def test_sidebar_and_main_panel_can_shrink_without_clipping_bottom_controls(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".main-view {", html)
        self.assertIn(".panel-left .mode-grid { grid-template-columns: 1fr;", html)
        self.assertGreaterEqual(html.count("min-height: 0;"), 3)

    def test_windows_video_dialog_uses_topmost_owner(self):
        source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("$owner.TopMost = $true", source)
        self.assertIn("$dialog.ShowDialog($owner)", source)
        self.assertIn("$owner.StartPosition = 'CenterScreen'", source)
        self.assertIn("$owner.Opacity = 0", source)

    def test_model_download_replaces_only_verified_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_name = "yolo11n.pt"
            expected_data = b"verified-model"
            expected_hash = hashlib.sha256(expected_data).hexdigest().upper()

            def fake_download(_url, destination):
                Path(destination).write_bytes(expected_data)

            with patch.object(main.CONFIG, "BASE_DIR", temp_dir), \
                 patch.object(main, "MODEL_ASSETS", {model_name: {"url": "https://example.invalid/model", "sha256": expected_hash}}), \
                 patch.object(main.urllib.request, "urlretrieve", side_effect=fake_download):
                result = main.ensure_model_weight(model_name)

            self.assertTrue(result["success"])
            self.assertTrue(result["downloaded"])
            self.assertEqual(Path(temp_dir, model_name).read_bytes(), expected_data)
            self.assertFalse(Path(temp_dir, model_name + ".download").exists())

    def test_model_allowlist_accepts_supported_names_and_rejects_paths(self):
        self.assertEqual(main.resolve_model_name({"aiModel": "yolo11n.pt"}), "yolo11n.pt")
        self.assertEqual(main.resolve_model_name({"aiModel": "yolo12s.pt"}), "yolo12s.pt")
        with self.assertRaisesRegex(ValueError, "不支援的 AI 模型"):
            main.resolve_model_name({"aiModel": "custom.pt"})

    def test_missing_model_error_lists_available_local_weight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "yolov8n.pt").write_bytes(b"test-weight")
            with patch.object(main.CONFIG, "BASE_DIR", temp_dir):
                with self.assertRaisesRegex(FileNotFoundError, "可用本機權重: yolov8n.pt"):
                    main.resolve_model_name({"aiModel": "yolo11n.pt"}, require_file=True)

    def test_watchdog_ignores_queue_backpressure_and_detects_decode_stall(self):
        self.assertFalse(main.should_trigger_decoder_deadlock("queue_wait", 120.0, timeout=30.0))
        self.assertFalse(main.should_trigger_decoder_deadlock("decoding", 29.9, timeout=30.0))
        self.assertTrue(main.should_trigger_decoder_deadlock("decoding", 30.0, timeout=30.0))

    def test_manual_mode_caches_paused_frame_instead_of_draining_decoder(self):
        source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("current_frame_cache = frame", source)
        self.assertIn("frame = current_frame_cache", source)
        self.assertNotIn("current_av_frame", source)

    def test_raw_stream_skip_builds_current_timecode_before_preview(self):
        source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
        raw_skip_block = source.split("if raw_skip_counter < static_skip_step:", 1)[1].split("else:", 1)[0]
        self.assertLess(raw_skip_block.index("t_str = format_timecode"), raw_skip_block.index("push_frame_to_ui"))
        self.assertIn("real_roi_poly, t_str", raw_skip_block)

    def test_unconfirmed_tracker_box_cannot_create_scene_event(self):
        source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
        event_block = source.split("new_scene_targets = []", 1)[1].split("if new_scene_targets:", 1)[0]
        self.assertIn("if not target.get('track_confirmed', False):", event_block)

    def test_tracker_generation_reset_clears_old_id_state(self):
        source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
        reset_block = source.split("# 重置 YOLO 追蹤器時會從頭分配 Track ID", 1)[1].split("continue", 1)[0]
        self.assertIn("track_states.clear()", reset_block)
        self.assertIn("id_alias_map.clear()", reset_block)
        self.assertLess(reset_block.index("track_states.clear()"), reset_block.index("model.predictor = None"))

    def test_player_one_shot_commands_increment_revisions(self):
        state = {
            "seek_req": None, "seek_revision": 0,
            "step_req": 0, "step_revision": 0,
            "manual_capture_req": False, "manual_capture_revision": 0,
            "playing": False, "reverse": False, "speed": 1.0,
        }
        with patch.object(main, "player_state", state), \
             patch.object(main.eel, "updatePlayState", create=True):
            main.seek_frame(25)
            main.step_frame(1)
            main.manual_capture()

        self.assertEqual(state["seek_revision"], 1)
        self.assertEqual(state["step_revision"], 1)
        self.assertEqual(state["manual_capture_revision"], 1)

    def test_safe_rename_records_hash_and_resolves_collision(self):
        payload = b"forensic-video"
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "CH07-20260326-173728-184505.avi"
            source.write_bytes(payload)
            collision = temp_path / "20260326_173728.avi"
            collision.write_bytes(b"existing")

            result = main.execute_rename_transaction(
                [str(source)], keep_old_name=False, manifest_dir=str(temp_path / "manifest")
            )

            self.assertTrue(result["success"])
            renamed = Path(result["new_paths"][0])
            self.assertEqual(renamed.name, "20260326_173728_1.avi")
            self.assertEqual(renamed.read_bytes(), payload)
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["items"][0]["sha256_before"], manifest["items"][0]["sha256_after"])
            self.assertTrue(Path(result["csv_path"]).exists())

    def test_safe_rename_rolls_back_completed_files_on_failure(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            sources = [
                temp_path / "CH07-20260326-173728-184505.avi",
                temp_path / "CH09-20260326-173728-184505.avi",
            ]
            for index, source in enumerate(sources):
                source.write_bytes(f"video-{index}".encode())

            calls = 0

            def failing_rename(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("模擬第二筆重新命名失敗")
                os.rename(source, destination)

            result = main.execute_rename_transaction(
                [str(path) for path in sources],
                keep_old_name=True,
                manifest_dir=str(temp_path / "manifest"),
                rename_func=failing_rename,
            )

            self.assertFalse(result["success"])
            self.assertTrue(all(path.exists() for path in sources))
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")

    def test_parse_channel_export_start_time(self):
        self.assertEqual(
            main.parse_start_time("CH07-20260326-173728-184505.avi"),
            main.datetime(2026, 3, 26, 17, 37, 28),
        )
        self.assertEqual(
            main.parse_start_time("CH09_20260326_173728_184505.avi"),
            main.datetime(2026, 3, 26, 17, 37, 28),
        )

    def test_existing_xvr_start_time_remains_supported(self):
        self.assertEqual(
            main.parse_start_time("XVR_ch1_main_20260604130000_20260604135959.mp4"),
            main.datetime(2026, 6, 4, 13, 0, 0),
        )

    def test_frontend_sends_all_live_processing_settings(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        for setting_name in ("confThresh", "skipSec", "classes", "inferenceSize", "riderAssist"):
            self.assertIn(f"update_live_setting('{setting_name}'", html)
        self.assertIn("executionMode: executionMode", html)
        self.assertIn("burnAnnotations: document.getElementById", html)
        self.assertNotIn('id="filterStationary"', html)

    def test_live_settings_override_all_supported_fields(self):
        current = {
            "confThresh": 0.40,
            "skipSec": 0.2,
            "classes": {"0": True, "2": True},
        }
        live = {
            "confThresh": 0.65,
            "skipSec": "1.5",
            "classes": {"0": False, "2": True},
        }

        resolved = main.resolve_live_processing_settings(live, current)

        self.assertEqual(resolved["confThresh"], 0.65)
        self.assertEqual(resolved["skipSec"], 1.5)
        self.assertEqual(resolved["classes"], {"0": False, "2": True})

    def test_evidence_metadata_contains_exact_sha256(self):
        payload = "AG-MONITOR 鑑識雜湊測試".encode("utf-8")
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            evidence_path = Path(temp_dir) / "證物.bin"
            evidence_path.write_bytes(payload)

            metadata = main.build_evidence_metadata(evidence_path)

            self.assertEqual(metadata["path"], str(evidence_path.resolve()))
            self.assertEqual(metadata["size"], len(payload))
            self.assertEqual(metadata["sha256"], hashlib.sha256(payload).hexdigest().upper())

    def test_scene_screenshot_keeps_original_resolution_and_creates_no_manifest(self):
        source = np.full((24, 32, 3), 127, dtype=np.uint8)
        original = source.copy()
        target = {"tid": 12, "cls_id": 2, "conf": 0.88, "xyxy": (2, 3, 20, 18)}
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            first_path = main.save_scene_screenshot(
                source, temp_dir, "2026/03/26 17:35:12.123", "CH07", target, [target], False
            )
            second_path = main.save_scene_screenshot(
                source, temp_dir, "2026/03/26 17:35:12.123", "CH07", target, [target], False
            )
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(Path(first_path).name, "CH07_17h35m12s_ID12_Car.jpg")
            saved = cv2.imread(first_path)
            self.assertEqual(saved.shape[:2], source.shape[:2])
            np.testing.assert_array_equal(source, original)
            self.assertFalse(any(Path(temp_dir).glob("*.jsonl")))
            self.assertFalse(any(Path(temp_dir).glob("*鑑識紀錄*.txt")))

    def test_scene_screenshot_preserves_unicode_video_name(self):
        source = np.zeros((16, 16, 3), dtype=np.uint8)
        target = {"tid": 7, "cls_id": 2, "conf": 0.9, "xyxy": (1, 1, 10, 10)}
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            output_path = main.save_scene_screenshot(
                source, temp_dir, "00:01:02", "路口東側_CH07", target, [target], False
            )

            self.assertEqual(Path(output_path).name, "路口東側_CH07_00h01m02s_ID7_Car.jpg")

    def test_annotation_output_uses_copy_and_draws_all_targets(self):
        source = np.zeros((80, 120, 3), dtype=np.uint8)
        original = source.copy()
        targets = [
            {"tid": 1, "cls_id": 2, "conf": 0.91, "xyxy": (5, 10, 45, 55)},
            {"tid": 2, "cls_id": 0, "conf": 0.82, "xyxy": (60, 15, 90, 70)},
        ]
        annotated = main.draw_scene_annotations(source, targets)
        np.testing.assert_array_equal(source, original)
        self.assertFalse(np.array_equal(annotated, source))

    def test_capture_writer_flushes_all_queued_scenes(self):
        source = np.full((24, 32, 3), 80, dtype=np.uint8)
        target = {"tid": 3, "cls_id": 3, "conf": 0.8, "xyxy": (2, 2, 20, 20)}
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            writer = main.CaptureWriter(maxsize=2)
            for second in range(3):
                self.assertTrue(writer.enqueue(source, temp_dir, f"00:00:0{second}", "cam", target, [target]))
            stats = writer.finish(flush=True)
            self.assertEqual(stats["events"], 3)
            self.assertEqual(stats["written"], 3)
            self.assertEqual(stats["discarded"], 0)
            self.assertEqual(len(list(Path(temp_dir).glob("*.jpg"))), 3)

    def test_capture_writer_force_stop_discards_pending_items(self):
        writer = main.CaptureWriter.__new__(main.CaptureWriter)
        writer._queue = main.queue.Queue(maxsize=4)
        writer._status_callback = None
        writer._lock = threading.Lock()
        writer._accepting = True
        writer._stats = {"events": 3, "written": 0, "discarded": 0, "errors": 0, "queued": 0, "state": "normal"}
        writer._thread = Mock()
        for _ in range(3):
            writer._queue.put({"invalid": True})
        stats = writer.finish(flush=False)
        self.assertEqual(stats["discarded"], 3)
        writer._thread.join.assert_called_once()

    def test_safe_base64_decode_validates_input_length_and_prefix(self):
        data, err = main.safe_base64_decode("data:image/png;base64,aGVsbG8=")
        self.assertIsNone(err)
        self.assertEqual(data, b"hello")

        data, err = main.safe_base64_decode("aGVsbG8")  # 無 padding 情況
        self.assertIsNone(err)
        self.assertEqual(data, b"hello")

        _, err = main.safe_base64_decode("a" * 100, max_bytes=50)
        self.assertIn("超出安全容量", err)

        _, err = main.safe_base64_decode(12345)
        self.assertIn("非字串格式", err)

    def test_start_eel_app_supports_port_fallback(self):
        attempts = []

        def fake_eel_start(filename, port=8000, **_kwargs):
            attempts.append(port)
            if port == 8000:
                raise OSError("[Errno 10048] address already in use")

        with patch.object(main.eel, "init"), patch.object(main.eel, "start", side_effect=fake_eel_start):
            main.start_eel_app()

        self.assertEqual(attempts, [8000, 8001])

    def test_live_processing_settings_preserve_inference_options(self):
        current = {
            "confThresh": 0.45,
            "skipSec": 0.2,
            "classes": {"0": True},
            "inferenceSize": 640,
            "riderAssist": False,
        }

        resolved = main.resolve_live_processing_settings(
            {"inferenceSize": 1280, "riderAssist": True}, current
        )

        self.assertEqual(resolved["inferenceSize"], 1280)
        self.assertTrue(resolved["riderAssist"])
        self.assertEqual(resolved["confThresh"], 0.45)

    def test_motion_confirmation_filters_jitter_and_keeps_small_targets(self):
        parked_state = {
            "start_centroid": (100.0, 100.0),
            "start_box_size": (200, 100),
            "is_moving": False,
            "motion_confirmations": 0,
        }
        self.assertFalse(main.record_motion_observation(parked_state, (110.0, 102.0), (203, 99)))
        self.assertFalse(main.record_motion_observation(parked_state, (109.0, 98.0), (201, 102)))

        distant_vehicle_state = {
            "start_centroid": (50.0, 50.0),
            "start_box_size": (60, 30),
            "is_moving": False,
            "motion_confirmations": 0,
        }
        self.assertFalse(main.record_motion_observation(distant_vehicle_state, (57.0, 50.0), (60, 30)))
        self.assertTrue(main.record_motion_observation(distant_vehicle_state, (58.0, 50.0), (60, 30)))

    def test_motion_guide_requires_area_and_two_consecutive_frames(self):
        detector = main.MotionGuideDetector(min_area=500, confirmation_frames=2)
        base = np.zeros((100, 100, 3), dtype=np.uint8)
        moved_one = base.copy()
        moved_one[10:40, 10:40] = 255
        moved_two = base.copy()
        moved_two[20:50, 20:50] = 255
        self.assertFalse(detector.observe(base))
        self.assertFalse(detector.observe(moved_one))
        self.assertTrue(detector.observe(moved_two))

        tiny_detector = main.MotionGuideDetector(min_area=500, confirmation_frames=2)
        tiny = base.copy()
        tiny[2:12, 2:12] = 255
        self.assertFalse(tiny_detector.observe(base))
        self.assertFalse(tiny_detector.observe(tiny))

    def test_frontend_exposes_headless_writer_and_force_stop_controls(self):
        html = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        for expected in ('value="headless"', 'id="burnAnnotations"', 'id="writerStatus"', 'id="btnForceStop"'):
            self.assertIn(expected, html)
        self.assertIn("eel.request_force_stop()", html)
        self.assertIn("eel.expose(updateWriterStatus)", html)
        self.assertIn("後端連線或啟動失敗", html)
        self.assertIn("<strong>啟動失敗</strong>", html)

    def test_start_processing_rejects_duplicate_task(self):
        with patch.object(main, "is_processing", True), \
             patch.object(main.eel, "updateStatus", create=True) as update_status:
            result = main.start_processing({})

        self.assertFalse(result["success"])
        self.assertIn("已有分析任務", result["msg"])
        update_status.assert_called_once()

    def test_start_processing_recovers_when_worker_thread_cannot_start(self):
        settings = {"aiModel": "yolov8n.pt", "inferenceSize": 960, "trackerMode": "bytetrack"}
        with patch.object(main, "video_queue", ["sample.mp4"]), \
             patch.object(main, "is_processing", False), \
             patch.object(main, "resolve_model_name", return_value="yolov8n.pt"), \
             patch.object(main, "resolve_tracker_config", return_value=("bytetrack", "tracker.yaml")), \
             patch.object(main, "Thread") as worker_thread, \
             patch.object(main.eel, "updateStatus", create=True):
            worker_thread.return_value.start.side_effect = RuntimeError("thread unavailable")
            result = main.start_processing(settings)

            self.assertFalse(result["success"])
            self.assertFalse(main.is_processing)
            self.assertIn("無法啟動", result["msg"])

    def test_add_dropped_paths_files_and_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            video1 = temp_path / "cam01.mp4"
            video1.write_bytes(b"dummy")
            video2 = temp_path / "sub" / "cam02.dav"
            video2.parent.mkdir(parents=True, exist_ok=True)
            video2.write_bytes(b"dummy")
            txt_file = temp_path / "note.txt"
            txt_file.write_bytes(b"dummy")

            with patch.object(main, "video_queue", []), patch.object(main, "load_preview_frame"):
                added = main.add_dropped_paths([str(video1), str(temp_path), str(txt_file)])
                # 應包含 video1 以及 temp_path 掃描到的 video2，但忽略 txt_file
                normalized_added = [os.path.normpath(p) for p in added]
                self.assertIn(os.path.normpath(str(video1)), normalized_added)
                self.assertIn(os.path.normpath(str(video2)), normalized_added)
                self.assertNotIn(os.path.normpath(str(txt_file)), normalized_added)

    def test_resolve_and_add_dropped_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            video1 = temp_path / "input_videos" / "target.mp4"
            video1.parent.mkdir(parents=True, exist_ok=True)
            video1.write_bytes(b"dummy12345")
            
            with patch.object(main.CONFIG, "BASE_DIR", str(temp_path)), \
                 patch.object(main, "video_queue", []), \
                 patch.object(main, "load_preview_frame"):
                
                meta = [{"name": "target.mp4", "size": len(b"dummy12345")}]
                res = main.resolve_and_add_dropped_files(meta)
                self.assertTrue(res["success"])
                self.assertEqual(len(res["added_paths"]), 1)
                self.assertEqual(os.path.normpath(res["added_paths"][0]), os.path.normpath(str(video1)))


if __name__ == "__main__":
    unittest.main()
