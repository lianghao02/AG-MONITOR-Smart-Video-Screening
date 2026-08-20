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
        self.assertIn('document.getElementById("trackerMode").disabled = true', html)

    def test_sr_download_failure_uses_opencv_fallback(self):
        completed = threading.Event()
        callback_result = {}

        class Receiver:
            def on_super_res_finished(self, encoded_image, warning):
                callback_result.update(image=encoded_image, warning=warning)
                completed.set()
                return lambda: None

        source = np.full((8, 12, 3), 127, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", source)
        self.assertTrue(ok)
        payload = base64.b64encode(encoded).decode("ascii")

        with patch.object(main, "eel", Receiver()), patch.object(
            main, "check_and_download_sr_model", return_value=False
        ):
            main.run_ai_super_resolution(payload, mode="plate")
            self.assertTrue(completed.wait(10), "OpenCV 備援流程未在期限內完成")

        self.assertIsNotNone(callback_result["image"])
        self.assertIn("備援", callback_result["warning"])

    def test_sr_invokes_ncnn_with_engine_working_directory(self):
        completed = threading.Event()
        callback_result = {}
        process_call = {}

        class Receiver:
            def on_super_res_finished(self, encoded_image, warning):
                callback_result.update(image=encoded_image, warning=warning)
                completed.set()
                return lambda: None

        class SuccessfulProcess:
            returncode = 0

            def __init__(self, command, **kwargs):
                process_call.update(command=command, kwargs=kwargs)
                image = cv2.imread(command[2])
                cv2.imwrite(command[4], image)

            def poll(self):
                return self.returncode

        source = np.full((8, 12, 3), 200, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", source)
        self.assertTrue(ok)
        payload = base64.b64encode(encoded).decode("ascii")

        with patch.object(main, "eel", Receiver()), patch.object(
            main, "check_and_download_sr_model", return_value=True
        ), patch.object(main.subprocess, "Popen", SuccessfulProcess):
            main.run_ai_super_resolution(payload, mode="plate")
            self.assertTrue(completed.wait(10), "NCNN 模擬流程未在期限內完成")

        self.assertEqual(process_call["kwargs"]["cwd"], main.NCNN_MODEL_DIR)
        self.assertEqual(process_call["command"][-1], "realesrgan-x4plus")
        self.assertIsNotNone(callback_result["image"])
        self.assertIsNone(callback_result["warning"])

    def test_sr_cancel_terminates_running_ncnn(self):
        started = threading.Event()
        terminated = threading.Event()

        class Receiver:
            def on_super_res_finished(self, *_args):
                return lambda: None

        class RunningProcess:
            returncode = None

            def __init__(self, *_args, **_kwargs):
                started.set()

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15
                terminated.set()

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9
                terminated.set()

        source = np.full((8, 12, 3), 64, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", source)
        self.assertTrue(ok)
        payload = base64.b64encode(encoded).decode("ascii")

        with patch.object(main, "eel", Receiver()), patch.object(
            main, "check_and_download_sr_model", return_value=True
        ), patch.object(main.subprocess, "Popen", RunningProcess):
            main.run_ai_super_resolution(payload, mode="face")
            self.assertTrue(started.wait(10), "NCNN 模擬程序未啟動")
            main.abort_ai_super_resolution()
            self.assertTrue(terminated.wait(10), "取消後未終止 NCNN 程序")
        main.sr_abort_flag = False

    def test_sr_zip_rejects_hash_mismatch_and_path_traversal(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            bad_hash_zip = temp_path / "bad-hash.zip"
            with zipfile.ZipFile(bad_hash_zip, "w") as archive:
                archive.writestr("placeholder.txt", "x")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                main.install_sr_engine_from_zip(
                    str(bad_hash_zip), str(temp_path / "engine"), expected_sha256="0" * 64
                )

            unsafe_zip = temp_path / "unsafe.zip"
            with zipfile.ZipFile(unsafe_zip, "w") as archive:
                archive.writestr("../escape.txt", "x")
            unsafe_hash = hashlib.sha256(unsafe_zip.read_bytes()).hexdigest().upper()
            with self.assertRaisesRegex(ValueError, "不安全路徑"):
                main.install_sr_engine_from_zip(
                    str(unsafe_zip), str(temp_path / "engine"), expected_sha256=unsafe_hash
                )
            self.assertFalse((temp_path / "escape.txt").exists())

    def test_sr_zip_installs_complete_engine_and_preserves_hash(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            archive_path = temp_path / "realesrgan.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for relative_path in main.NCNN_REQUIRED_FILES:
                    archive.writestr(f"release/{relative_path}", relative_path.encode("utf-8"))
            expected_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest().upper()
            expected_exe_hash = hashlib.sha256(b"realesrgan-ncnn-vulkan.exe").hexdigest().upper()
            engine_dir = temp_path / "engine"

            actual_hash = main.install_sr_engine_from_zip(
                str(archive_path),
                str(engine_dir),
                expected_sha256=expected_hash,
                expected_exe_sha256=expected_exe_hash,
            )

            self.assertEqual(actual_hash, expected_hash)
            self.assertTrue(main.validate_sr_engine(str(engine_dir), expected_exe_hash))

    def test_watchdog_ignores_queue_backpressure_and_detects_decode_stall(self):
        self.assertFalse(main.should_trigger_decoder_deadlock("queue_wait", 120.0, timeout=30.0))
        self.assertFalse(main.should_trigger_decoder_deadlock("decoding", 29.9, timeout=30.0))
        self.assertTrue(main.should_trigger_decoder_deadlock("decoding", 30.0, timeout=30.0))

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
        for setting_name in ("confThresh", "fastMode", "skipSec", "classes", "captureMode", "filterStationary"):
            self.assertIn(f"update_live_setting('{setting_name}'", html)

    def test_live_settings_override_all_supported_fields(self):
        current = {
            "confThresh": 0.40,
            "fastMode": True,
            "skipSec": 0.2,
            "classes": {"0": True, "2": True},
            "captureMode": "舊模式",
            "filterStationary": True,
        }
        live = {
            "confThresh": 0.65,
            "fastMode": False,
            "skipSec": "1.5",
            "classes": {"0": False, "2": True},
            "captureMode": "事件起訖模式",
            "filterStationary": False,
        }

        resolved = main.resolve_live_processing_settings(live, current)

        self.assertEqual(resolved["confThresh"], 0.65)
        self.assertFalse(resolved["fastMode"])
        self.assertEqual(resolved["skipSec"], 1.5)
        self.assertEqual(resolved["classes"], {"0": False, "2": True})
        self.assertEqual(resolved["captureMode"], "事件起訖模式")
        self.assertFalse(resolved["filterStationary"])

    def test_evidence_metadata_contains_exact_sha256(self):
        payload = "AG-MONITOR 鑑識雜湊測試".encode("utf-8")
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir:
            evidence_path = Path(temp_dir) / "證物.bin"
            evidence_path.write_bytes(payload)

            metadata = main.build_evidence_metadata(evidence_path)

            self.assertEqual(metadata["path"], str(evidence_path.resolve()))
            self.assertEqual(metadata["size"], len(payload))
            self.assertEqual(metadata["sha256"], hashlib.sha256(payload).hexdigest().upper())

    def test_capture_modes_flush_expected_evidence(self):
        base_state = {
            "is_moving": True,
            "best_frame": Mock(),
            "best_timecode": "00:00:01.000",
            "best_summary": ["ID:1 car"],
            "best_target_info": {},
            "last_frame": Mock(),
            "last_timecode": "00:00:02.000",
            "last_target_info": {},
            "class_name": "car",
        }

        with patch.object(main, "save_legal_screenshot") as save_mock, patch.object(main, "eel", Mock()):
            states = {1: base_state.copy()}
            main._flush_all_track_states(states, "雙格蒐證模式 (起點+最清晰)", "out", "video")
            self.assertEqual(save_mock.call_count, 1)

            save_mock.reset_mock()
            states = {1: base_state.copy()}
            main._flush_all_track_states(states, "事件起訖模式", "out", "video")
            self.assertEqual(save_mock.call_count, 1)
            self.assertIn("Exit", save_mock.call_args.args[3][0])

            save_mock.reset_mock()
            states = {1: base_state.copy()}
            main._flush_all_track_states(states, "持續追蹤模式 (預設)", "out", "video")
            self.assertEqual(save_mock.call_count, 0)

    def test_same_timecode_captures_never_overwrite_existing_evidence(self):
        source = np.full((24, 32, 3), 127, dtype=np.uint8)
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir, patch.object(
            main, "write_report"
        ), patch.object(main, "current_report_path", str(Path(temp_dir) / "系統鑑識紀錄.txt")):
            output_dir = Path(temp_dir) / "video"
            first_path = main.save_legal_screenshot(
                source.copy(), str(output_dir), "2026/03/26 17:37:28.123", ["ID:1 car"], "video"
            )
            second_path = main.save_legal_screenshot(
                source.copy(), str(output_dir), "2026/03/26 17:37:28.123", ["ID:2 car"], "video"
            )

            self.assertIsNotNone(first_path)
            self.assertIsNotNone(second_path)
            self.assertNotEqual(first_path, second_path)
            self.assertTrue(Path(first_path).is_file())
            self.assertTrue(Path(second_path).is_file())
            self.assertEqual(len(list(output_dir.glob("*.jpg"))), 2)
            manifest_path = Path(temp_dir) / main.CAPTURE_MANIFEST_FILENAME
            records = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["targets"], ["ID:1 car"])
            self.assertEqual(records[1]["targets"], ["ID:2 car"])
            self.assertTrue(all(record["source_prefix"] == "video" for record in records))
            for record in records:
                capture_path = Path(record["path"])
                self.assertEqual(record["size"], capture_path.stat().st_size)
                self.assertEqual(record["sha256"], hashlib.sha256(capture_path.read_bytes()).hexdigest().upper())

    def test_capture_manifest_failure_does_not_report_success(self):
        source = np.full((24, 32, 3), 127, dtype=np.uint8)
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as temp_dir, patch.object(
            main, "_append_capture_manifest", side_effect=OSError("模擬清冊寫入失敗")
        ), patch.object(main, "write_report") as report_mock:
            result = main.save_legal_screenshot(
                source, temp_dir, "2026/03/26 17:37:28.123", ["ID:1 car"], "video"
            )

        self.assertIsNone(result)
        self.assertTrue(any("清冊寫入失敗" in call.args[0] for call in report_mock.call_args_list))

    def test_capture_write_failure_is_not_reported_as_success(self):
        state = {
            "is_moving": True,
            "best_frame": Mock(),
            "best_timecode": "00:00:01.000",
            "best_summary": ["ID:1 car"],
            "best_target_info": {},
            "last_frame": Mock(),
            "last_timecode": "00:00:02.000",
            "last_target_info": {},
            "class_name": "car",
        }
        receiver = Mock()
        with patch.object(main, "save_legal_screenshot", return_value=None), patch.object(main, "eel", receiver):
            main._flush_all_track_states(
                {1: state}, "雙格蒐證模式 (起點+最清晰)", "out", "video"
            )

        receiver.appendLog.assert_called_once()
        self.assertEqual(receiver.appendLog.call_args.args[1], "error")
        self.assertIn("失敗", receiver.appendLog.call_args.args[0])

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
            "fastMode": True,
            "skipSec": 0.2,
            "classes": {"0": True},
            "captureMode": "",
            "filterStationary": True,
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
