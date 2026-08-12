import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import main


class ForensicCoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
