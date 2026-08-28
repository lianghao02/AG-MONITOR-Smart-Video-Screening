import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
os.environ.setdefault("YOLO_CONFIG_DIR", str(PROJECT_DIR / "captures" / ".ultralytics"))
os.environ.setdefault("MPLCONFIGDIR", str(PROJECT_DIR / "captures" / ".matplotlib"))
Path(os.environ["YOLO_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

EXPECTED_VERSIONS = {
    "av": "17.1.0",
    "Eel": "0.18.2",
    "lap": "0.5.13",
    "opencv-contrib-python": "4.13.0.92",
    "opencv-python": "4.13.0.92",
    "ultralytics": "8.4.67",
}


def verify_runtime():
    errors = []
    if not ((3, 10) <= sys.version_info[:2] < (3, 14)):
        errors.append(f"不支援的 Python 版本: {sys.version.split()[0]}（需要 3.10～3.13）")

    for package_name, expected in EXPECTED_VERSIONS.items():
        try:
            installed = version(package_name)
        except PackageNotFoundError:
            errors.append(f"缺少套件: {package_name}=={expected}")
            continue
        if installed != expected:
            errors.append(f"版本不符: {package_name}=={installed}，需要 {expected}")

    try:
        import av  # noqa: F401
        import cv2  # noqa: F401
        import eel  # noqa: F401
        import lap  # noqa: F401
        from ultralytics import YOLO  # noqa: F401
    except Exception as error:
        errors.append(f"核心套件匯入失敗: {error}")

    return errors


if __name__ == "__main__":
    failures = verify_runtime()
    if failures:
        for failure in failures:
            print(f"[ERROR] {failure}")
        raise SystemExit(1)
    print(f"[OK] AG-MONITOR runtime verified with Python {sys.version.split()[0]}")
