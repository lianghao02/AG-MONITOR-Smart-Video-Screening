import sys
import unittest
from pathlib import Path

import av


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import main


class RealVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.videos = sorted(path for path in (PROJECT_DIR / "input_videos").glob("*") if path.is_file())
        if not cls.videos:
            raise unittest.SkipTest("input_videos 內沒有實體測試影片")
        cls.model = main.YOLO(str(PROJECT_DIR / "yolov8n.pt"))

    def test_all_videos_decode_and_seek(self):
        for video_path in self.videos:
            print(f"[REAL VIDEO] 開始: {video_path.name}", flush=True)
            with self.subTest(video=video_path.name), av.open(str(video_path), metadata_errors="ignore") as container:
                stream = container.streams.video[0]
                duration = (
                    float(stream.duration * stream.time_base)
                    if stream.duration is not None
                    else float(container.duration / av.time_base)
                )
                decoded = []
                for ratio in (0.0, 0.5, 0.95):
                    print(f"[REAL VIDEO] {video_path.name} seek={ratio:.2f}", flush=True)
                    if ratio:
                        target_pts = int(duration * ratio / float(stream.time_base))
                        container.seek(target_pts, stream=stream, backward=True)
                    frame = next(container.decode(stream))
                    image = frame.to_ndarray(format="bgr24")
                    self.assertEqual(image.shape[2], 3)
                    decoded.append(image)

                result = self.model.predict(decoded[1], verbose=False, conf=0.40)
                self.assertEqual(len(result), 1)
            print(f"[REAL VIDEO] 完成: {video_path.name}", flush=True)


if __name__ == "__main__":
    unittest.main()
