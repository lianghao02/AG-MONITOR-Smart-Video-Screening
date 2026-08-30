"""建立 AG-MONITOR Windows x64 CPU 完整離線可攜版。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_ROOT = (PROJECT_ROOT / "dist").resolve()
VERSION = (PROJECT_ROOT / "version.txt").read_text(encoding="utf-8-sig").strip()
if not VERSION.startswith("v") or not VERSION[1:].replace(".", "").isdigit():
    raise ValueError(f"version.txt 格式不正確：{VERSION!r}")

PRODUCT_ID = "AG-MONITOR-Smart-Video-Screening"
RELEASE_NAME = f"{PRODUCT_ID}-{VERSION}-win-x64-portable"
# 實體建置路徑刻意縮短，避免 PyTorch 深層目錄撞上 Windows MAX_PATH；
# ZIP 內的頂層名稱與對外資產名稱仍保留完整產品識別。
RELEASE_DIR = (DIST_ROOT / f"AG-MONITOR-{VERSION}").resolve()
CURRENT_DIR = RELEASE_DIR / "current"
DATA_DIR = RELEASE_DIR / "data"
ARCHIVE_PATH = DIST_ROOT / f"{RELEASE_NAME}.zip"

APP_FILES = (
    "main.py",
    "version.txt",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "requirements.txt",
    "portable-requirements.txt",
    "requirements-release.txt",
)
APP_DIRS = ("web", "trackers")

MODEL_ASSETS = {
    "yolov8n.pt": {
        "sha256": "F59B3D833E2FF32E194B5BB8E08D211DC7C5BDF144B90D2C8412C47CCFC83B36",
        "url": "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt",
    },
    "yolo11n.pt": {
        "sha256": "0EBBC80D4A7680D14987A577CD21342B65ECFD94632BD9A8DA63AE6417644EE1",
        "url": "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt",
    },
    "yolo11s.pt": {
        "sha256": "85A76FE86DD8AFE384648546B56A7A78580C7CB7B404FC595F97969322D502D5",
        "url": "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt",
    },
    "yolo12n.pt": {
        "sha256": "419FF3DCA37D69BACC93A50FA0C186A1C6F9FE62FAE0F108B0872829689E9CA6",
        "url": "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo12n.pt",
    },
    "yolo12s.pt": {
        "sha256": "E915C2C4286E3F6F8610EF106FA3F94A7B8C19B30ECCEDE5887E22C33EF75F58",
        "url": "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo12s.pt",
    },
}

LAUNCH_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
title AG-MONITOR Smart Video Screening
set "AG_MONITOR_DATA_DIR=%~dp0data"
set "YOLO_CONFIG_DIR=%~dp0data\captures\.ultralytics"
set "MPLCONFIGDIR=%~dp0data\captures\.matplotlib"

if not exist "current\runtime\python.exe" goto RUNTIME_ERROR
if not exist "current\main.py" goto RUNTIME_ERROR
if not exist "data\captures" mkdir "data\captures"
if not exist "data\logs" mkdir "data\logs"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%"
if not exist "%MPLCONFIGDIR%" mkdir "%MPLCONFIGDIR%"

"current\runtime\python.exe" -B -c "import av, cv2, eel, lap, torch, ultralytics" >"data\logs\startup_error.log" 2>&1
if errorlevel 1 goto IMPORT_ERROR
del /Q "data\logs\startup_error.log" >nul 2>&1
start "" /D "%~dp0current" "%~dp0current\runtime\pythonw.exe" -B "main.py"
exit /b 0

:RUNTIME_ERROR
echo [ERROR] Portable runtime is incomplete. Extract the complete ZIP before running.
pause
exit /b 1

:IMPORT_ERROR
echo [ERROR] Runtime import check failed. See data\logs\startup_error.log.
type "data\logs\startup_error.log"
pause
exit /b 1
"""

RELEASE_INFO = f"""AG-MONITOR 智慧影像快篩系統 {VERSION}

使用方式：
1. 將 ZIP 完整解壓縮至本機可寫入的資料夾。
2. 執行 AG-MONITOR.exe；若遭機關政策阻擋，可改執行「啟動程式.bat」查看錯誤。
3. 截圖與執行紀錄保存在 data 資料夾，更新程式時請保留該資料夾。

發行內容：
- Windows 10／11 x64、Python 3.13、Torch CPU 完整離線 Runtime。
- YOLOv8n、YOLO11n、YOLO11s、YOLO12n、YOLO12s 五個模型。
- 不需安裝 Python、pip、CUDA，也不會在首次啟動時下載套件或模型。

授權與責任：
- 本專案依 GNU AGPL-3.0 公開，第三方條款見 current/THIRD_PARTY_NOTICES.md。
- AI 結果僅供人工快篩，不保證零漏失或零誤報，也不取代原始影片及人工判斷。
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def assert_release_paths() -> None:
    if RELEASE_DIR.parent != DIST_ROOT or DIST_ROOT.parent != PROJECT_ROOT:
        raise RuntimeError(f"拒絕使用非預期發行路徑：{RELEASE_DIR}")


def git_output(*args: str) -> str:
    # Codex 工作區或由不同 Windows 帳號執行中央建置時，Repository
    # 擁有者可能與目前帳號不同。只對本專案單次放行，不修改全域 Git 設定。
    safe_directory = PROJECT_ROOT.as_posix()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={safe_directory}", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def check_git_state(allow_dirty: bool) -> tuple[str, bool]:
    commit = git_output("rev-parse", "HEAD")
    dirty = bool(git_output("status", "--porcelain", "--untracked-files=no"))
    if dirty and not allow_dirty:
        raise RuntimeError("Git 工作目錄有已追蹤變更；正式建置前請先提交，或測試時使用 --allow-dirty")
    return commit, dirty


def download_models() -> None:
    for name, metadata in MODEL_ASSETS.items():
        target = PROJECT_ROOT / name
        if target.is_file() and sha256_file(target) == metadata["sha256"]:
            continue
        temporary = target.with_suffix(target.suffix + ".download")
        temporary.unlink(missing_ok=True)
        print(f"[MODEL] 正在下載 {name}...")
        urllib.request.urlretrieve(metadata["url"], temporary)
        actual = sha256_file(temporary)
        if actual != metadata["sha256"]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"{name} SHA-256 不符：{actual}")
        temporary.replace(target)


def verify_and_copy_models() -> None:
    for name, metadata in MODEL_ASSETS.items():
        source = PROJECT_ROOT / name
        if not source.is_file():
            raise FileNotFoundError(f"缺少發行模型：{name}；可使用 --download-models 取得")
        actual = sha256_file(source)
        if actual != metadata["sha256"]:
            raise RuntimeError(f"{name} SHA-256 不符：{actual}")
        shutil.copy2(source, CURRENT_DIR / name)


def copy_application() -> None:
    for name in APP_FILES:
        source = PROJECT_ROOT / name
        if not source.is_file():
            raise FileNotFoundError(f"缺少發行必要檔案：{name}")
        shutil.copy2(source, CURRENT_DIR / name)
    for name in APP_DIRS:
        source = PROJECT_ROOT / name
        if not source.is_dir():
            raise FileNotFoundError(f"缺少發行必要資料夾：{name}")
        shutil.copytree(
            source,
            CURRENT_DIR / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"),
        )


def copy_runtime() -> Path:
    source = PROJECT_ROOT / "python_embed"
    target = CURRENT_DIR / "runtime"
    if not (source / "python.exe").is_file() or not (source / "pythonw.exe").is_file():
        raise FileNotFoundError("找不到完整 python_embed Runtime")
    print("[RUNTIME] 正在建立精簡且已驗證的 Python 可攜環境...")

    def ignore_runtime(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        try:
            relative_parts = tuple(part.lower() for part in current.relative_to(source).parts)
        except ValueError:
            relative_parts = ()
        ignored = {
            name
            for name in names
            if name == "__pycache__"
            or name.endswith((".pyc", ".pyo", ".lib"))
        }
        # torch/include 是編譯擴充模組用的 C/C++ 標頭，桌面推論不會使用。
        if relative_parts[-1:] == ("torch",):
            ignored.add("include")
        # 第三方授權檔另以短路徑攤平保存，避免解壓縮時超過 MAX_PATH。
        if "licenses" in relative_parts and "third_party" in relative_parts:
            ignored.update(names)
        return ignored

    shutil.copytree(source, target, ignore=ignore_runtime, dirs_exist_ok=True)
    copy_flattened_runtime_licenses(source, CURRENT_DIR / "runtime-licenses")
    return target / "python.exe"


def copy_flattened_runtime_licenses(source: Path, target: Path) -> None:
    """保存 PyTorch 深層第三方授權內容，並以短路徑避免 Windows 解壓失敗。"""
    roots = sorted((source / "Lib" / "site-packages").glob("torch-*.dist-info/licenses/third_party"))
    if not roots:
        return
    target.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []
    seen_hashes: set[str] = set()
    sequence = 0
    for root in roots:
        for license_file in sorted(path for path in root.rglob("*") if path.is_file()):
            digest = sha256_file(license_file)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            sequence += 1
            suffix = license_file.suffix if len(license_file.suffix) <= 8 else ".txt"
            output_name = f"{sequence:03d}_{digest[:12]}{suffix or '.txt'}"
            shutil.copy2(license_file, target / output_name)
            manifest.append(
                {
                    "file": output_name,
                    "source": license_file.relative_to(source).as_posix(),
                    "sha256": digest,
                }
            )
    (target / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_locked_versions() -> dict[str, str]:
    locked: dict[str, str] = {}
    for raw_line in (PROJECT_ROOT / "requirements-release.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        locked[name.lower().replace("_", "-")] = version
    return locked


def verify_runtime_versions(runtime_python: Path) -> None:
    code = (
        "import json; from importlib.metadata import distributions; "
        "print(json.dumps({d.metadata['Name'].lower().replace('_','-'): d.version "
        "for d in distributions() if d.metadata.get('Name')}, sort_keys=True))"
    )
    result = subprocess.run(
        [str(runtime_python), "-B", "-c", code],
        cwd=CURRENT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    installed = json.loads(result.stdout)
    mismatches = [
        f"{name}: 預期 {version}，實際 {installed.get(name, '缺少')}"
        for name, version in parse_locked_versions().items()
        if installed.get(name) != version
    ]
    if mismatches:
        raise RuntimeError("Runtime 版本不符：\n" + "\n".join(mismatches))


def verify_models(runtime_python: Path) -> None:
    model_names = list(MODEL_ASSETS)
    script = (
        "import json, os, numpy as np; from ultralytics import YOLO; "
        f"names={model_names!r}; rows=[]; image=np.zeros((320,320,3), dtype=np.uint8); "
        "[(lambda m,n: rows.append({'model':n,'task':m.task,'results':len(m.predict(image, imgsz=320, verbose=False))}))(YOLO(n),n) for n in names]; "
        "print('MODEL_VERIFY=' + json.dumps(rows, ensure_ascii=False))"
    )
    env = os.environ.copy()
    env["AG_MONITOR_DATA_DIR"] = str(DATA_DIR)
    env["YOLO_CONFIG_DIR"] = str(DATA_DIR / "captures" / ".ultralytics")
    env["MPLCONFIGDIR"] = str(DATA_DIR / "captures" / ".matplotlib")
    Path(env["YOLO_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(runtime_python), "-B", "-c", script],
        cwd=CURRENT_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"模型 CPU 推論驗證失敗：\n{details}")
    print(result.stdout.strip())


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def create_launcher_icon(target: Path) -> None:
    size = 256
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            dx, dy = x - 128, y - 128
            radius = (dx * dx + dy * dy) ** 0.5
            if radius <= 112:
                base = int(30 + 45 * (1 - y / size))
                color = (16, 45 + base, 74 + base, 255)
                if abs(dx) < 16 or abs(dy) < 16:
                    color = (95, 214, 222, 255)
                if 55 < radius < 72:
                    color = (223, 239, 236, 255)
            else:
                color = (0, 0, 0, 0)
            rows.extend(color)
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += png_chunk(b"IEND", b"")
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    target.write_bytes(header + entry + png)


def build_launcher() -> None:
    source = PROJECT_ROOT / "scripts" / "launcher" / "Launcher.cs"
    icon = DIST_ROOT / ".build" / "AG-MONITOR.ico"
    icon.parent.mkdir(parents=True, exist_ok=True)
    create_launcher_icon(icon)
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windir / r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        windir / r"Microsoft.NET\Framework\v4.0.30319\csc.exe",
    )
    compiler = next((item for item in candidates if item.is_file()), None)
    if compiler is None:
        raise FileNotFoundError("找不到 Windows C# 編譯器 csc.exe")
    output = RELEASE_DIR / "AG-MONITOR.exe"
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/optimize+",
            "/platform:anycpu",
            f"/win32icon:{icon}",
            f"/out:{output}",
            "/reference:System.Windows.Forms.dll,System.dll",
            str(source),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )


def write_release_metadata(commit: str, dirty: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "captures").mkdir(exist_ok=True)
    (DATA_DIR / "logs").mkdir(exist_ok=True)
    (DATA_DIR / "README.txt").write_text(
        "此資料夾保存截圖與執行紀錄；更新程式時請保留。\n", encoding="utf-8-sig"
    )
    (RELEASE_DIR / "啟動程式.bat").write_text(LAUNCH_BAT, encoding="ascii")
    (RELEASE_DIR / "發行說明.txt").write_text(RELEASE_INFO, encoding="utf-8-sig")
    build_info = {
        "product": PRODUCT_ID,
        "version": VERSION,
        "platform": "Windows 10/11 x64",
        "runtime": "Python 3.13 / Torch CPU",
        "source_commit": commit,
        "source_dirty": dirty,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": {name: metadata["sha256"] for name, metadata in MODEL_ASSETS.items()},
    }
    (RELEASE_DIR / "BUILD_INFO.json").write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_checksums() -> None:
    rows = []
    for path in sorted(RELEASE_DIR.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS.txt":
            continue
        rows.append(f"{sha256_file(path)} *{path.relative_to(RELEASE_DIR).as_posix()}")
    (RELEASE_DIR / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


def create_archive() -> None:
    ARCHIVE_PATH.unlink(missing_ok=True)
    print("[ZIP] 正在建立完整可攜壓縮檔...")
    with zipfile.ZipFile(ARCHIVE_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(RELEASE_DIR.rglob("*")):
            if path.is_file():
                relative = path.relative_to(RELEASE_DIR).as_posix()
                archive.write(path, f"{RELEASE_NAME}/{relative}")
    digest = sha256_file(ARCHIVE_PATH)
    ARCHIVE_PATH.with_suffix(".zip.sha256").write_text(
        f"{digest} *{ARCHIVE_PATH.name}\n", encoding="utf-8"
    )
    print(f"[OK] ZIP：{ARCHIVE_PATH} ({ARCHIVE_PATH.stat().st_size / 1024 / 1024:.2f} MB)")
    print(f"[OK] SHA-256：{digest}")



def safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    import stat
    def on_error(func, p, _):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    try:
        subprocess.run(f'cmd /c "rmdir /s /q \"{path}\""', shell=True, check=False)
    except Exception:
        pass
    if path.exists():
        shutil.rmtree(path, onerror=on_error)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--skip-model-inference", action="store_true")
    args = parser.parse_args()

    assert_release_paths()
    commit, dirty = check_git_state(args.allow_dirty)
    if args.download_models:
        download_models()
    if RELEASE_DIR.exists():
        safe_rmtree(RELEASE_DIR)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)

    copy_application()
    verify_and_copy_models()
    runtime_python = copy_runtime()
    verify_runtime_versions(runtime_python)
    build_launcher()
    write_release_metadata(commit, dirty)
    if not args.skip_model_inference:
        verify_models(runtime_python)
    write_checksums()
    create_archive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
