import json
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
TOOLS_DIR = BASE_DIR / "tools"


def _find_steam_path() -> Optional[Path]:
    """Locate Steam installation folder via Windows registry or common paths."""
    if os.name == "nt":
        try:
            import winreg

            for root, subkey, val_name in [
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            ]:
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        val, _ = winreg.QueryValueEx(k, val_name)
                        if val and Path(val).exists():
                            return Path(val)
                except OSError:
                    continue
        except ImportError:
            pass

    for candidate in [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path(r"D:\Steam"),
    ]:
        if candidate.exists():
            return candidate
    return None


def find_cs2_exe() -> str:
    """Auto-discover cs2.exe inside Steam library folders."""
    steam_path = _find_steam_path()
    library_paths: List[Path] = []
    if steam_path:
        library_paths.append(steam_path)
        vdf_path = steam_path / "steamapps" / "libraryfolders.vdf"
        if vdf_path.exists():
            try:
                content = vdf_path.read_text(encoding="utf-8", errors="ignore")
                for match in re.finditer(r'"path"\s+"([^"]+)"', content):
                    raw_path = match.group(1).replace("\\\\", "\\")
                    p = Path(raw_path)
                    if p.exists() and p not in library_paths:
                        library_paths.append(p)
            except OSError:
                pass

    rel_cs2 = Path("steamapps") / "common" / "Counter-Strike Global Offensive" / "game" / "bin" / "win64" / "cs2.exe"
    for lib in library_paths:
        candidate = lib / rel_cs2
        if candidate.exists():
            return str(candidate.resolve())

    # Check common drive letters directly
    for drive in ["C:", "D:", "E:", "F:"]:
        for base in [f"{drive}\\SteamLibrary", f"{drive}\\Steam", f"{drive}\\Program Files (x86)\\Steam"]:
            candidate = Path(base) / rel_cs2
            if candidate.exists():
                return str(candidate.resolve())

    return ""


def find_ffmpeg_exe() -> str:
    """Locate ffmpeg executable in PATH or local tools directory."""
    local_ffmpeg = TOOLS_DIR / "ffmpeg" / "ffmpeg.exe"
    if local_ffmpeg.exists():
        return str(local_ffmpeg.resolve())

    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return str(Path(which_ffmpeg).resolve())

    return ""


def find_ffprobe_exe(ffmpeg_path: str = "") -> str:
    """Locate ffprobe executable next to ffmpeg or in PATH."""
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.exists():
            return str(sibling.resolve())
    which_ffprobe = shutil.which("ffprobe")
    if which_ffprobe:
        return str(Path(which_ffprobe).resolve())
    return ""


def find_hlae_exe() -> str:
    """Locate HLAE.exe in local tools directory, PATH, or standard folders."""
    candidates = [
        TOOLS_DIR / "hlae" / "HLAE.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "HLAE" / "HLAE.exe",
        Path(os.environ.get("APPDATA", "")) / "HLAE" / "HLAE.exe",
        Path(r"C:\HLAE\HLAE.exe"),
        Path(r"C:\Tools\HLAE\HLAE.exe"),
        Path(r"C:\Program Files (x86)\HLAE\HLAE.exe"),
        Path(r"C:\Program Files\HLAE\HLAE.exe"),
    ]
    for c in candidates:
        if c and c.exists():
            return str(c.resolve())

    which_hlae = shutil.which("HLAE") or shutil.which("hlae")
    if which_hlae:
        return str(Path(which_hlae).resolve())

    return ""


def check_nvenc_available(ffmpeg_exe: str) -> bool:
    """Test whether h264_nvenc is supported and functional on this machine."""
    if not ffmpeg_exe or not Path(ffmpeg_exe).exists():
        return False
    try:
        res = subprocess.run(
            [
                ffmpeg_exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.1",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return res.returncode == 0
    except Exception:
        return False


def download_hlae(target_dir: Optional[Path] = None) -> str:
    """Download latest portable HLAE release from GitHub into tools/hlae."""
    dest_dir = target_dir or (TOOLS_DIR / "hlae")
    dest_dir.mkdir(parents=True, exist_ok=True)
    hlae_exe = dest_dir / "HLAE.exe"
    if hlae_exe.exists():
        return str(hlae_exe.resolve())

    req = urllib.request.Request(
        "https://api.github.com/repos/advancedfx/advancedfx/releases/latest",
        headers={"User-Agent": "CS2-Movie-Maker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        release = json.loads(resp.read().decode("utf-8"))

    download_url = None
    for asset in release.get("assets", []):
        name = asset.get("name", "").lower()
        if name.startswith("hlae_") and name.endswith(".zip"):
            download_url = asset.get("browser_download_url")
            break

    if not download_url:
        raise RuntimeError("Не удалось найти архив HLAE в последнем релизе GitHub.")

    zip_path = dest_dir / "hlae_latest.zip"
    req_dl = urllib.request.Request(download_url, headers={"User-Agent": "CS2-Movie-Maker/1.0"})
    with urllib.request.urlopen(req_dl, timeout=60) as resp, open(zip_path, "wb") as out_f:
        shutil.copyfileobj(resp, out_f)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    try:
        zip_path.unlink()
    except OSError:
        pass

    if not hlae_exe.exists():
        # Search inside subdirectories if extracted into a nested folder
        found = list(dest_dir.rglob("HLAE.exe"))
        if found:
            return str(found[0].resolve())
        raise RuntimeError("Архив HLAE распакован, но HLAE.exe не найден.")

    return str(hlae_exe.resolve())


def get_default_config() -> Dict[str, Any]:
    cs2_exe = find_cs2_exe()
    ffmpeg_exe = find_ffmpeg_exe()
    hlae_exe = find_hlae_exe()
    nvenc_ok = check_nvenc_available(ffmpeg_exe)

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    return {
        "cs2_path": cs2_exe,
        "ffmpeg_path": ffmpeg_exe,
        "hlae_path": hlae_exe,
        "output_dir": str(DEFAULT_OUTPUT_DIR.resolve()),
        "recording_backend": "hlae" if hlae_exe else "netcon_capture",
        "width": 1920,
        "height": 1080,
        "fps": 144,
        "encoder": "h264_nvenc" if nvenc_ok else "libx264",
        "nvenc_available": nvenc_ok,
        "crf": 18,
        "bitrate": "20M",
        "hud_enabled": False,
        "only_deathnotices": True,
        "xray_enabled": False,
        "kill_pre_seconds": 2.0,
        "kill_post_seconds": 2.0,
        "merge_gap_seconds": 5.0,
        "death_pre_seconds": 4.0,
        "death_post_seconds": 1.5,
        "crossfade_enabled": False,
        "crossfade_duration": 0.2,
        "music_path": "",
        "music_volume": 0.3,
        "game_volume": 0.85,
        "netcon_port": 2121,
    }


def load_config() -> Dict[str, Any]:
    defaults = get_default_config()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if v not in (None, "") or k in ("music_path", "hlae_path"):
                    defaults[k] = v
            # Re-fill auto-discovered paths if saved ones are empty or missing
            if not defaults.get("cs2_path") or not Path(defaults["cs2_path"]).exists():
                auto_cs2 = find_cs2_exe()
                if auto_cs2:
                    defaults["cs2_path"] = auto_cs2
            if not defaults.get("ffmpeg_path") or not Path(defaults["ffmpeg_path"]).exists():
                auto_ffmpeg = find_ffmpeg_exe()
                if auto_ffmpeg:
                    defaults["ffmpeg_path"] = auto_ffmpeg
            if not defaults.get("hlae_path") or not Path(defaults["hlae_path"]).exists():
                auto_hlae = find_hlae_exe()
                if auto_hlae:
                    defaults["hlae_path"] = auto_hlae
        except Exception:
            pass

    save_config(defaults)
    return defaults


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out_dir = cfg.get("output_dir")
    if out_dir:
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def check_environment(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate paths and provide helpful hints for missing components."""
    c = cfg or load_config()
    cs2_ok = bool(c.get("cs2_path") and Path(c["cs2_path"]).is_file())
    ffmpeg_ok = bool(c.get("ffmpeg_path") and Path(c["ffmpeg_path"]).is_file())
    hlae_ok = bool(c.get("hlae_path") and Path(c["hlae_path"]).is_file())
    nvenc_ok = check_nvenc_available(c.get("ffmpeg_path", "")) if ffmpeg_ok else False

    hints: List[str] = []
    if not cs2_ok:
        hints.append(
            "CS2 (cs2.exe) не найден автоматически. Укажите путь вручную в настройках "
            "(обычно: Steam\\steamapps\\common\\Counter-Strike Global Offensive\\game\\bin\\win64\\cs2.exe)."
        )
    if not ffmpeg_ok:
        hints.append(
            "FFmpeg (ffmpeg.exe) не найден. Установите FFmpeg (например: winget install Gyan.FFmpeg) "
            "или укажите путь к ffmpeg.exe в настройках."
        )
    if not hlae_ok:
        hints.append(
            "HLAE не установлен. Вы можете нажать кнопку «Скачать HLAE» в настройках для идеальной записи через "
            "mirv_streams, либо использовать встроенный режим захвата окна CS2 через FFmpeg (работает без HLAE)."
        )

    return {
        "cs2_ok": cs2_ok,
        "cs2_path": c.get("cs2_path", ""),
        "ffmpeg_ok": ffmpeg_ok,
        "ffmpeg_path": c.get("ffmpeg_path", ""),
        "hlae_ok": hlae_ok,
        "hlae_path": c.get("hlae_path", ""),
        "nvenc_available": nvenc_ok,
        "output_dir": c.get("output_dir", str(DEFAULT_OUTPUT_DIR)),
        "hints": hints,
    }
