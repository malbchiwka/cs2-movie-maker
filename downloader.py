import bz2
import ctypes
import gzip
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional, Set, Tuple

from config import find_cs2_exe

# Official Valve 57-character base dictionary for CS:GO / CS2 share codes
SHARECODE_DICTIONARY = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
SHARECODE_REGEX = re.compile(
    r"(CSGO-[A-HJ-NP-Za-km-z2-9]{5}-[A-HJ-NP-Za-km-z2-9]{5}-[A-HJ-NP-Za-km-z2-9]{5}-[A-HJ-NP-Za-km-z2-9]{5}-[A-HJ-NP-Za-km-z2-9]{5})"
)


def extract_sharecode(val: str) -> Optional[str]:
    """Extract CSGO-xxxxx-xxxxx-xxxxx-xxxxx-xxxxx from a raw sharecode or steam://rungame/730/... URI."""
    unquoted = urllib.parse.unquote(val.strip())
    m = SHARECODE_REGEX.search(unquoted)
    return m.group(1) if m else None


def is_url(val: str) -> bool:
    """Return True if val is an HTTP(S) URL, a steam:// URI, or contains a CSGO-... match share code."""
    s = val.strip().lower()
    if s.startswith("http://") or s.startswith("https://") or s.startswith("steam://"):
        return True
    return extract_sharecode(val) is not None


def decode_sharecode(code: str) -> Tuple[int, int, int]:
    """
    Decode a CS:GO / CS2 match share code (CSGO-xxxxx-xxxxx-xxxxx-xxxxx-xxxxx)
    into (match_id, outcome_id, token_id).
    """
    clean = code.strip()
    if clean.upper().startswith("CSGO-"):
        clean = clean[5:]
    clean = clean.replace("-", "")
    if len(clean) != 25:
        raise ValueError(f"Некорректная длина кода матча: {code}")

    big = 0
    for ch in reversed(clean):
        idx = SHARECODE_DICTIONARY.find(ch)
        if idx == -1:
            raise ValueError(f"Недопустимый символ '{ch}' в коде матча: {code}")
        big = big * 57 + idx

    b = big.to_bytes(18, byteorder="big")
    match_id = int.from_bytes(b[0:8], "little")
    outcome_id = int.from_bytes(b[8:16], "little")
    token_id = int.from_bytes(b[16:18], "little")
    return match_id, outcome_id, token_id


def _get_cs2_replays_dirs() -> List[Path]:
    """Find local CS2 'csgo/replays' directories where CS2 stores .dem and .dem.info files."""
    dirs: List[Path] = []
    cs2_exe = find_cs2_exe()
    if cs2_exe:
        try:
            # .../Counter-Strike Global Offensive/game/bin/win64/cs2.exe -> parents[2] is .../game
            replays_dir = Path(cs2_exe).resolve().parents[2] / "csgo" / "replays"
            if replays_dir.is_dir():
                dirs.append(replays_dir)
        except Exception:
            pass

    for fallback in [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\replays"),
        Path(r"C:\Program Files\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\replays"),
        Path(r"D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\csgo\replays"),
    ]:
        if fallback.is_dir() and fallback not in dirs:
            dirs.append(fallback)
    return dirs


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    res = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7
    return res, pos


def _parse_protobuf_fields(buf: bytes) -> List[Tuple[int, str, object]]:
    pos = 0
    fields: List[Tuple[int, str, object]] = []
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            val, pos = _read_varint(buf, pos)
            fields.append((field_num, "varint", val))
        elif wire_type == 2:
            ln, pos = _read_varint(buf, pos)
            sub = buf[pos : pos + ln]
            pos += ln
            fields.append((field_num, "bytes", sub))
        elif wire_type == 5:
            if pos + 4 > len(buf):
                break
            val = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
            fields.append((field_num, "fixed32", val))
        elif wire_type == 1:
            if pos + 8 > len(buf):
                break
            val = int.from_bytes(buf[pos : pos + 8], "little")
            pos += 8
            fields.append((field_num, "fixed64", val))
        else:
            break
    return fields


def _inspect_dem_info(info_path: Path) -> Optional[ dict ]:
    """
    Parse a CS2 .dem.info file (CDataGCCStrike15_v2_MatchInfo) and filename
    to extract match_id, outcome_id, tv_port, cluster, cdn_url, and player steam64 IDs.
    """
    try:
        m = re.match(r"match730_(\d+)_(\d+)_(\d+)\.dem\.info$", info_path.name, re.IGNORECASE)
        outcome_from_name = int(m.group(1)) if m else 0
        tv_port_from_name = int(m.group(2)) if m else 0
        cluster_from_name = int(m.group(3)) if m else 0

        raw = info_path.read_bytes()
        top = _parse_protobuf_fields(raw)
        match_id = 0
        cluster = cluster_from_name
        tv_port = tv_port_from_name
        outcome_id = outcome_from_name
        steam64s: Set[str] = set()

        for fnum, ftype, fval in top:
            if fnum == 1 and ftype == "varint":
                match_id = int(fval)
            elif fnum == 3 and ftype == "bytes" and isinstance(fval, bytes):
                wfields = _parse_protobuf_fields(fval)
                for wfnum, wftype, wfval in wfields:
                    if wfnum == 1 and wftype == "varint" and not cluster:
                        cluster = int(wfval)
                    elif wfnum == 2 and wftype == "varint" and not tv_port:
                        tv_port = int(wfval)
            elif fnum == 5 and ftype == "bytes" and isinstance(fval, bytes):
                rfields = _parse_protobuf_fields(fval)
                for rfnum, rftype, rfval in rfields:
                    if rfnum == 1 and rftype == "varint" and int(rfval) > 0:
                        outcome_id = int(rfval)
                    elif rfnum == 2 and rftype == "bytes" and isinstance(rfval, bytes):
                        res_fields = _parse_protobuf_fields(rfval)
                        for afnum, aftype, afval in res_fields:
                            if afnum == 1 and aftype == "varint" and int(afval) > 0:
                                steam64s.add(str(76561197960265728 + int(afval)))

        if outcome_id and tv_port and cluster:
            cdn_url = f"http://replay{cluster}.valve.net/730/{outcome_id:021d}_{tv_port:010d}.dem.bz2"
            return {
                "info_path": info_path,
                "match_id": match_id,
                "outcome_id": outcome_id,
                "tv_port": tv_port,
                "cluster": cluster,
                "cdn_url": cdn_url,
                "steam64s": steam64s,
            }
    except Exception:
        pass
    return None


def _find_local_dem_or_cdn_for_ids(
    match_id: int, outcome_id: int, uploads_dir: Path
) -> Tuple[Optional[Path], Optional[str]]:
    """
    Search uploads_dir and CS2 replays_dirs for an existing .dem file or a .dem.info file
    matching outcome_id or match_id.
    Returns (existing_dem_path, cdn_url).
    """
    outcome_str = f"{outcome_id:021d}"
    replays_dirs = _get_cs2_replays_dirs()

    # 1. Check if an uncompressed .dem already exists in uploads_dir or replays_dirs
    for d in [uploads_dir, *replays_dirs]:
        if not d.is_dir():
            continue
        for dem_file in d.glob(f"*{outcome_str}*.dem"):
            if dem_file.is_file() and dem_file.stat().st_size > 1024 * 1024:
                return dem_file, None

    # 2. Check .dem.info files in replays_dirs for matching outcome_id or match_id
    for d in replays_dirs:
        for info_file in d.glob("*.dem.info"):
            meta = _inspect_dem_info(info_file)
            if not meta:
                continue
            if (outcome_id and meta["outcome_id"] == outcome_id) or (match_id and meta["match_id"] == match_id):
                # Also check if corresponding .dem file is next to .dem.info
                candidate_dem = info_file.with_suffix("")  # removes .info -> .dem
                if candidate_dem.is_file() and candidate_dem.stat().st_size > 1024 * 1024:
                    return candidate_dem, None
                return None, str(meta["cdn_url"])

    return None, None


def _extract_archive_if_needed(downloaded_path: Path, dest_dir: Path) -> Path:
    """
    Given a downloaded file (.dem, .dem.bz2, .dem.gz, .zip), unpack it if compressed
    and return the final .dem Path in dest_dir.
    """
    name_lower = downloaded_path.name.lower()

    # Check magic bytes as well in case URL had no extension
    with open(downloaded_path, "rb") as f:
        magic = f.read(16)

    if magic.startswith(b"HL2DEMO") or magic.startswith(b"PBDEMS2"):
        # Already an uncompressed Source 1 / Source 2 .dem file
        final_name = downloaded_path.name
        if not final_name.lower().endswith(".dem"):
            final_name += ".dem"
        final_path = dest_dir / final_name
        if downloaded_path.resolve() != final_path.resolve():
            shutil.move(str(downloaded_path), str(final_path))
        return final_path

    # BZip2 (.dem.bz2 from Valve replay CDN: magic 'BZh')
    if name_lower.endswith(".bz2") or magic.startswith(b"BZh"):
        stem = re.sub(r"\.bz2$", "", downloaded_path.name, flags=re.IGNORECASE)
        stem = re.sub(r"^_dl_\d+_", "", stem)
        if not stem.lower().endswith(".dem"):
            stem += ".dem"
        final_path = dest_dir / stem
        with bz2.open(downloaded_path, "rb") as src, open(final_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        try:
            downloaded_path.unlink()
        except OSError:
            pass
        return final_path

    # GZip (.dem.gz: magic 1f 8b)
    if name_lower.endswith(".gz") or magic.startswith(b"\x1f\x8b"):
        stem = re.sub(r"\.gz$", "", downloaded_path.name, flags=re.IGNORECASE)
        stem = re.sub(r"^_dl_\d+_", "", stem)
        if not stem.lower().endswith(".dem"):
            stem += ".dem"
        final_path = dest_dir / stem
        with gzip.open(downloaded_path, "rb") as src, open(final_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        try:
            downloaded_path.unlink()
        except OSError:
            pass
        return final_path

    # ZIP archive (.zip: magic 'PK\x03\x04')
    if name_lower.endswith(".zip") or magic.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(downloaded_path, "r") as zf:
            dem_members = [m for m in zf.namelist() if m.lower().endswith(".dem")]
            if not dem_members:
                raise ValueError("В скачанном ZIP-архиве не найден .dem файл.")
            member = dem_members[0]
            final_path = dest_dir / Path(member).name
            with zf.open(member) as src, open(final_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        try:
            downloaded_path.unlink()
        except OSError:
            pass
        return final_path

    raise ValueError(
        f"Скачанный файл ({downloaded_path.name}) не является валидной демкой CS2 (.dem / .dem.bz2)."
    )


def _download_direct_file(url: str, dest_dir: Path) -> Path:
    """Download a direct .dem / .dem.bz2 / .dem.gz / .zip URL and unpack it."""
    parsed = urllib.parse.urlparse(url)
    base_name = Path(urllib.parse.unquote(parsed.path)).name or f"demo_{int(time.time())}.dem.bz2"
    base_name = re.sub(r'[<>:"/\\|?*]', "_", base_name)
    tmp_path = dest_dir / f"_dl_{int(time.time())}_{base_name}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_path, "wb") as out_f:
        shutil.copyfileobj(resp, out_f, length=1024 * 1024)

    return _extract_archive_if_needed(tmp_path, dest_dir)


def _resolve_sharecode(sharecode: str, uploads_dir: Path) -> Path:
    """
    Resolve a CS2 Share Code (CSGO-xxxxx-xxxxx-xxxxx-xxxxx-xxxxx):
    1. Decode sharecode into (match_id, outcome_id, token_id).
    2. Check if the .dem or .dem.info already exists in uploads/ or CS2's csgo/replays/ folder.
    3. If .dem.info exists, build the official Valve CDN URL (http://replay<cluster>.valve.net/730/...)
       and download + unpack .dem.bz2 directly without needing any browser or login.
    4. If .dem.info is not yet in csgo/replays/, trigger steam://rungame/730/.../+csgo_download_match
       so CS2 fetches the 4 KB .dem.info file, then grab the CDN URL from it and download directly.
    """
    match_id, outcome_id, _ = decode_sharecode(sharecode)

    existing_dem, cdn_url = _find_local_dem_or_cdn_for_ids(match_id, outcome_id, uploads_dir)
    if existing_dem:
        return existing_dem
    if cdn_url:
        return _download_direct_file(cdn_url, uploads_dir)

    # If not yet cached in csgo/replays, ask Steam/CS2 to fetch the match info via steam:// protocol
    if os.name == "nt":
        try:
            steam_uri = f"steam://rungame/730/76561202255233023/+csgo_download_match%20{sharecode}"
            os.startfile(steam_uri)
            deadline = time.monotonic() + 35.0
            while time.monotonic() < deadline:
                time.sleep(1.0)
                existing_dem, cdn_url = _find_local_dem_or_cdn_for_ids(match_id, outcome_id, uploads_dir)
                if existing_dem:
                    return existing_dem
                if cdn_url:
                    return _download_direct_file(cdn_url, uploads_dir)
        except Exception:
            pass

    raise FileNotFoundError(
        f"Для кода {sharecode} (MatchID {match_id}) не найден локальный .dem.info в папке csgo/replays. "
        "Откройте вкладку «Ваши матчи» в CS2 (чтобы игра подгрузила список матчей) или скачайте демку в игре."
    )


def _bring_chrome_to_front() -> None:
    """Bring the DrissionPage Chrome window to the foreground when user action (Steam login) is needed."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if ("Steam" in title or "Войти" in title or "Sign In" in title or "csstats.gg" in title) and (
                        "Chrome" in title or "Chromium" in title
                    ):
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.SetForegroundWindow(hwnd)
                        return False
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception:
        pass


def _resolve_via_browser(url: str, dest_dir: Path, profile_dir: Path) -> Path:
    """
    Resolve a csstats.gg (or other Cloudflare/Steam-protected) match/watch URL
    using DrissionPage with a persistent browser profile:
    - Also checks if the match on csstats.gg matches a local .dem.info in csgo/replays
      by comparing player Steam64 IDs on the match page (if so, downloads directly from Valve CDN without Steam login!).
    - Otherwise handles Cloudflare Turnstile and one-time Steam OpenID sign-in.
    """
    try:
        import DrissionPage._functions.web as _dp_web
        _orig_format = _dp_web.format_headers

        def _safe_format_headers(txt):
            if not txt:
                return {}
            if isinstance(txt, dict):
                return _orig_format(txt)
            res = {}
            for line in str(txt).split("\n"):
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    k_clean = k.strip()
                    if k_clean not in (":method", ":scheme", ":authority", ":path"):
                        res[k_clean] = v.strip()
            return res

        _dp_web.format_headers = _safe_format_headers
    except Exception:
        pass

    from DrissionPage import ChromiumOptions, ChromiumPage

    profile_dir.mkdir(parents=True, exist_ok=True)
    dl_temp_dir = Path(tempfile.mkdtemp(prefix="cs2mm_dl_"))

    co = ChromiumOptions()
    co.set_user_data_path(str(profile_dir))
    co.set_download_path(str(dl_temp_dir))
    co.set_local_port(9345)
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--allow-running-insecure-content")
    co.set_argument("--disable-features=InsecureDownloadWarnings")

    page = ChromiumPage(co)
    captured_cdn_url: Optional[str] = None
    completed_browser_file: Optional[Path] = None
    target_watch_url: str = url

    # Pre-load local .dem.info metadata so we can match csstats.gg players without needing Steam login if possible
    local_infos: List[dict] = []
    for rdir in _get_cs2_replays_dirs():
        for info_file in rdir.glob("*.dem.info"):
            meta = _inspect_dem_info(info_file)
            if meta and meta.get("steam64s"):
                local_infos.append(meta)

    try:
        page.set.download_path(str(dl_temp_dir))
        page.listen.start(["watch", "valve.net", ".dem", ".bz2"])

        # If given a csstats.gg/match/<id>/watch/<token> URL and we have local .dem.info files,
        # check the public csstats.gg/match/<id> page first to see if it matches a local .dem.info!
        m_csstats = re.match(r"^(https?://csstats\.gg/match/\d+)/watch/", url.strip(), re.IGNORECASE)
        if m_csstats and local_infos:
            match_page_url = m_csstats.group(1)
            page.get(match_page_url, timeout=15)
            for _ in range(10):
                t = page.title or ""
                if "Один момент" in t or "Just a moment" in t:
                    try:
                        w = page.ele("@id^cf-chl-widget-", timeout=0.5)
                        if w:
                            w.parent().click.at(30, 30)
                    except Exception:
                        pass
                    time.sleep(0.8)
                else:
                    break
            html = page.html or ""
            page_steam64s = set(re.findall(r"\b7656119\d{10}\b", html))
            if len(page_steam64s) >= 5:
                best_meta = None
                best_overlap = 0
                for meta in local_infos:
                    overlap = len(page_steam64s.intersection(meta["steam64s"]))
                    if overlap >= 6 and overlap > best_overlap:
                        best_overlap = overlap
                        best_meta = meta
                if best_meta:
                    candidate_dem = best_meta["info_path"].with_suffix("")
                    try:
                        page.quit()
                    except Exception:
                        pass
                    if candidate_dem.is_file() and candidate_dem.stat().st_size > 1024 * 1024:
                        return candidate_dem
                    return _download_direct_file(str(best_meta["cdn_url"]), dest_dir)

        page.get(url, timeout=15)

        deadline = time.monotonic() + 180.0
        clicked_watch_on_match_page = False
        saw_steam_login = False
        injected_steam_banner = False
        retriggered_after_login = False

        while time.monotonic() < deadline:
            # 1. Check DrissionPage DownloadManager missions first!
            try:
                missions = list(page.browser._dl_mgr.missions.values())
                for m in missions:
                    m_url = (m.url or "").strip()
                    m_low = m_url.lower()
                    if m_url and ("valve.net" in m_low or ".dem" in m_low or ".bz2" in m_low):
                        captured_cdn_url = m_url
                        try:
                            page.browser._dl_mgr.cancel(m)
                        except Exception:
                            pass
                        break
                    if getattr(m, "state", "") == "completed" and getattr(m, "final_path", None):
                        fp = Path(m.final_path)
                        if fp.is_file() and fp.stat().st_size > 1024:
                            completed_browser_file = fp
                            break
            except Exception:
                pass

            if captured_cdn_url or completed_browser_file:
                break

            # 2. Check if a completed file with .dem / .bz2 / .gz / .zip extension appeared in dl_temp_dir
            for f in dl_temp_dir.glob("*"):
                if (
                    f.is_file()
                    and f.name.lower().endswith((".dem", ".bz2", ".gz", ".zip"))
                    and not f.name.endswith(".crdownload")
                    and not f.name.endswith(".tmp")
                    and f.stat().st_size > 1024 * 100
                ):
                    completed_browser_file = f
                    break

            if completed_browser_file:
                break

            # 3. Check network packets for a 302 redirect or direct request to replay*.valve.net / .dem.bz2
            for pkt in page.listen.steps(timeout=0.4):
                loc = ""
                try:
                    loc = str(pkt.response.headers.get("location") or pkt.response.headers.get("Location") or "")
                except Exception:
                    pass
                for candidate in (loc, pkt.url):
                    if not candidate:
                        continue
                    c_low = candidate.lower()
                    if ("valve.net" in c_low or c_low.endswith(".dem.bz2") or c_low.endswith(".dem")) and (
                        c_low.startswith("http://") or c_low.startswith("https://")
                    ):
                        captured_cdn_url = candidate
                        break
                if captured_cdn_url:
                    break

            if captured_cdn_url:
                break

            # 4. Auto-click Cloudflare Turnstile if on challenge page
            title = page.title or ""
            if "Один момент" in title or "Just a moment" in title:
                try:
                    wrapper = page.ele("@id^cf-chl-widget-", timeout=0.3)
                    if wrapper:
                        wrapper.parent().click.at(30, 30)
                except Exception:
                    pass
                time.sleep(0.8)
                continue

            # 5. Handle Steam OpenID login required by csstats.gg
            curr_url = page.url or ""
            if "steamcommunity.com" in curr_url:
                saw_steam_login = True
                if not injected_steam_banner:
                    injected_steam_banner = True
                    try:
                        page.run_js(
                            """
                            if (!document.getElementById('cs2mm-banner')) {
                                const b = document.createElement('div');
                                b.id = 'cs2mm-banner';
                                b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;background:#18181b;color:#f4f4f5;border-bottom:2px solid #2563eb;padding:12px 20px;font-family:system-ui,sans-serif;font-size:14px;text-align:center;box-shadow:0 4px 12px rgba(0,0,0,0.5);';
                                b.innerHTML = '<b>CS2 Movie Maker:</b> сайт <b>csstats.gg</b> требует разовый вход через Steam для скачивания демки. Войдите один раз — сессия сохранится, и скачивание начнётся автоматически.';
                                document.body.prepend(b);
                                document.body.style.paddingTop = '48px';
                            }
                            """
                        )
                    except Exception:
                        pass
                    _bring_chrome_to_front()
                time.sleep(0.6)
                continue

            # If user just completed Steam OpenID login and returned to csstats.gg, re-trigger target_watch_url
            if (
                saw_steam_login
                and not retriggered_after_login
                and "csstats.gg" in curr_url
                and "/login/" not in curr_url
                and "/watch/" not in curr_url
            ):
                retriggered_after_login = True
                page.get(target_watch_url, timeout=12)
                continue

            # 6. If user pasted a csstats.gg/match/<id> URL (without /watch/), find and navigate to /watch/
            if (
                not clicked_watch_on_match_page
                and "csstats.gg/match/" in curr_url
                and "/watch/" not in curr_url
                and "steamcommunity.com" not in curr_url
            ):
                # Check if players on this match page match a local .dem.info first!
                if local_infos:
                    html = page.html or ""
                    page_steam64s = set(re.findall(r"\b7656119\d{10}\b", html))
                    for meta in local_infos:
                        if len(page_steam64s.intersection(meta["steam64s"])) >= 6:
                            captured_cdn_url = str(meta["cdn_url"])
                            break
                    if captured_cdn_url:
                        break

                for a in page.eles("t:a"):
                    href = a.attr("href") or ""
                    if "/watch/" in href:
                        clicked_watch_on_match_page = True
                        full_watch = urllib.parse.urljoin("https://csstats.gg", href)
                        target_watch_url = full_watch
                        page.get(full_watch, timeout=12)
                        break

            # 7. Also check if page HTML contains a direct replay*.valve.net link
            try:
                html = page.html or ""
                m = re.search(r"https?://replay\d+\.valve\.net/730/[^\s\"'<>]+\.dem(?:\.bz2)?", html)
                if m:
                    captured_cdn_url = m.group(0)
                    break
            except Exception:
                pass

            time.sleep(0.5)

        if completed_browser_file and completed_browser_file.exists():
            return _extract_archive_if_needed(completed_browser_file, dest_dir)

        if captured_cdn_url:
            try:
                page.quit()
            except Exception:
                pass
            return _download_direct_file(captured_cdn_url, dest_dir)

        raise TimeoutError(
            "Не удалось получить файл демки со страницы: на csstats.gg требуется разовый вход через Steam в открывшемся окне браузера."
        )
    finally:
        try:
            page.quit()
        except Exception:
            pass
        shutil.rmtree(dl_temp_dir, ignore_errors=True)


def download_demo_from_url(url: str, uploads_dir: Path) -> Path:
    """
    Download and unpack a CS2 demo from a URL or Share Code:
    - Steam match links (steam://rungame/730/.../+csgo_download_match%20CSGO-...) and raw share codes (CSGO-...)
      are decoded and resolved via local csgo/replays (.dem or .dem.info -> direct Valve CDN download).
    - Direct links (.dem, .dem.bz2, .dem.gz, .zip, replay*.valve.net) are downloaded directly.
    - Protected links (https://csstats.gg/match/.../watch/... or https://csstats.gg/match/...)
      are matched against local .dem.info files or resolved via DrissionPage + persistent browser profile.
    """
    clean_url = url.strip()
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # 1. Check if input is a Steam Share Code URI (steam://rungame/730/...) or raw CSGO-... sharecode
    sharecode = extract_sharecode(clean_url)
    if sharecode:
        return _resolve_sharecode(sharecode, uploads_dir)

    low = clean_url.lower()

    # 2. Fast path: direct Valve replay CDN or direct .dem / .bz2 / .gz / .zip URL (not behind csstats.gg)
    if "csstats.gg" not in low and (
        "valve.net" in low
        or low.endswith(".dem")
        or low.endswith(".dem.bz2")
        or low.endswith(".bz2")
        or low.endswith(".dem.gz")
        or low.endswith(".zip")
    ):
        try:
            return _download_direct_file(clean_url, uploads_dir)
        except Exception:
            pass

    # 3. Browser resolver path (csstats.gg, Cloudflare-protected links, etc.)
    profile_dir = uploads_dir.parent / "tools" / "browser_profile"
    return _resolve_via_browser(clean_url, uploads_dir, profile_dir)
