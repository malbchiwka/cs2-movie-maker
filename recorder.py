import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import check_nvenc_available, download_hlae, find_ffprobe_exe, load_config, save_config

ProgressCallback = Callable[[Dict[str, Any]], None]


class RenderCancelledError(Exception):
    """Raised when the user cancels the active render job."""


class CS2MovieRecorder:
    """
    Orchestrates CS2 clip recording via HLAE (AfxHookSource2 + -afxFixNetCon -netconport + mirv_cmd + mirv_streams)
    and FFmpeg video concatenation, optional 0.2s crossfade, and background MP3 audio mixing.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        on_progress: Optional[ProgressCallback] = None,
    ):
        self.config = config or load_config()
        self.on_progress = on_progress or (lambda _: None)
        self._cancel_event = threading.Event()
        self._active_processes: List[subprocess.Popen] = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        """Cancel the current render and terminate any child processes (CS2, HLAE, FFmpeg)."""
        self._cancel_event.set()
        self._emit("warn", "Отмена рендера... Завершаем процессы (CS2 / HLAE / FFmpeg).")
        with self._lock:
            for proc in self._active_processes:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                except Exception:
                    pass
        self._kill_existing_game_processes()

    @staticmethod
    def _set_cs2_windows_mute(muted: bool) -> None:
        """
        Mute or unmute cs2.exe in the Windows Audio Mixer (ISimpleAudioVolume::SetMute)
        so the user hears no sound from speakers while CS2 renders in the background,
        while HLAE's internal startMovieWav recording still captures full in-game audio.
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import POINTER, HRESULT, c_float, c_int, c_uint32, c_wchar_p
            from ctypes.wintypes import BOOL, DWORD, LPCWSTR
            import comtypes
            from comtypes import COMMETHOD, GUID, IUnknown

            CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
            IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            IID_IAudioSessionManager2 = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
            IID_IAudioSessionControl2 = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
            IID_ISimpleAudioVolume = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")

            class IMMDevice(IUnknown):
                _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
                _methods_ = [
                    COMMETHOD(
                        [],
                        HRESULT,
                        "Activate",
                        (["in"], POINTER(GUID), "iid"),
                        (["in"], DWORD, "dwClsCtx"),
                        (["in"], ctypes.c_void_p, "pActivationParams"),
                        (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
                    ),
                ]

            class IMMDeviceEnumerator(IUnknown):
                _iid_ = IID_IMMDeviceEnumerator
                _methods_ = [
                    COMMETHOD([], HRESULT, "EnumAudioEndpoints"),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "GetDefaultAudioEndpoint",
                        (["in"], c_uint32, "dataFlow"),
                        (["in"], c_uint32, "role"),
                        (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
                    ),
                ]

            class IAudioSessionControl(IUnknown):
                _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
                _methods_ = [
                    COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
                    COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "pRetVal")),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "SetDisplayName",
                        (["in"], LPCWSTR, "Value"),
                        (["in"], POINTER(GUID), "EventContext"),
                    ),
                    COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "pRetVal")),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "SetIconPath",
                        (["in"], LPCWSTR, "Value"),
                        (["in"], POINTER(GUID), "EventContext"),
                    ),
                    COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "SetGroupingParam",
                        (["in"], POINTER(GUID), "Override"),
                        (["in"], POINTER(GUID), "EventContext"),
                    ),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "RegisterAudioSessionNotification",
                        (["in"], POINTER(IUnknown), "NewNotifications"),
                    ),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "UnregisterAudioSessionNotification",
                        (["in"], POINTER(IUnknown), "NewNotifications"),
                    ),
                ]

            class IAudioSessionControl2(IAudioSessionControl):
                _iid_ = IID_IAudioSessionControl2
                _methods_ = [
                    COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
                    COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
                    COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(DWORD), "pRetVal")),
                    COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
                    COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], BOOL, "optOut")),
                ]

            class ISimpleAudioVolume(IUnknown):
                _iid_ = IID_ISimpleAudioVolume
                _methods_ = [
                    COMMETHOD(
                        [],
                        HRESULT,
                        "SetMasterVolume",
                        (["in"], c_float, "fLevel"),
                        (["in"], POINTER(GUID), "EventContext"),
                    ),
                    COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(c_float), "pfLevel")),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "SetMute",
                        (["in"], BOOL, "bMute"),
                        (["in"], POINTER(GUID), "EventContext"),
                    ),
                    COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
                ]

            class IAudioSessionEnumerator(IUnknown):
                _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
                _methods_ = [
                    COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "GetSession",
                        (["in"], c_int, "SessionCount"),
                        (["out"], POINTER(POINTER(IAudioSessionControl)), "Session"),
                    ),
                ]

            class IAudioSessionManager2(IUnknown):
                _iid_ = IID_IAudioSessionManager2
                _methods_ = [
                    COMMETHOD([], HRESULT, "GetAudioSessionControl"),
                    COMMETHOD([], HRESULT, "GetSimpleAudioVolume"),
                    COMMETHOD(
                        [],
                        HRESULT,
                        "GetSessionEnumerator",
                        (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum"),
                    ),
                ]

            comtypes.CoInitialize()
            enumerator = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
            )
            dev = enumerator.GetDefaultAudioEndpoint(0, 0)
            mgr_unk = dev.Activate(ctypes.byref(IID_IAudioSessionManager2), comtypes.CLSCTX_INPROC_SERVER, None)
            mgr = mgr_unk.QueryInterface(IAudioSessionManager2)
            sess_enum = mgr.GetSessionEnumerator()
            count = sess_enum.GetCount()
            for i in range(count):
                try:
                    ctrl = sess_enum.GetSession(i)
                    ctrl2 = ctrl.QueryInterface(IAudioSessionControl2)
                    ident = (ctrl2.GetSessionIdentifier() or "").lower()
                    if "cs2.exe" in ident:
                        vol = ctrl.QueryInterface(ISimpleAudioVolume)
                        vol.SetMute(1 if muted else 0, None)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _kill_existing_game_processes() -> bool:
        killed = False
        if os.name == "nt":
            for img in ("HLAE.exe", "cs2.exe"):
                try:
                    res = subprocess.run(
                        ["taskkill", "/F", "/IM", img],
                        capture_output=True,
                        timeout=4,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if res.returncode == 0:
                        killed = True
                except Exception:
                    pass
        return killed

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise RenderCancelledError("Рендер отменён пользователем.")

    def _emit(
        self,
        level: str,
        message: str,
        progress: Optional[float] = None,
        clip_index: Optional[int] = None,
        total_clips: Optional[int] = None,
        stage: str = "recording",
        output_file: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "timestamp": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "stage": stage,
        }
        if progress is not None:
            payload["progress"] = round(max(0.0, min(100.0, float(progress))), 1)
        if clip_index is not None:
            payload["clip_index"] = clip_index
        if total_clips is not None:
            payload["total_clips"] = total_clips
        if output_file is not None:
            payload["output_file"] = output_file
        self.on_progress(payload)

    def _run_subprocess(self, args: List[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        self._check_cancelled()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        with self._lock:
            self._active_processes.append(proc)
        try:
            start_t = time.monotonic()
            while True:
                self._check_cancelled()
                ret = proc.poll()
                if ret is not None:
                    stdout, stderr = proc.communicate()
                    return subprocess.CompletedProcess(args, ret, stdout, stderr)
                if timeout and (time.monotonic() - start_t) > timeout:
                    proc.kill()
                    raise TimeoutError(f"Превышен таймаут выполнения команды: {args[0]}")
                time.sleep(0.15)
        finally:
            with self._lock:
                if proc in self._active_processes:
                    self._active_processes.remove(proc)

    def render_movie(
        self,
        demo_data: Dict[str, Any],
        clip_queue: List[Dict[str, Any]],
        render_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute the complete render workflow:
        1. Validate environment (FFmpeg, CS2/HLAE, demo file).
        2. Record all clips in clip_queue in a single CS2 session.
        3. Concatenate clips via FFmpeg (simple concat or 0.2s xfade/acrossfade).
        4. Mix optional background MP3 music with adjustable volume.
        5. Return final MP4 path and open output folder.
        """
        self._cancel_event.clear()
        opts = {**self.config, **(render_options or {})}

        if not clip_queue:
            raise ValueError("Очередь клипов пуста — выберите хотя бы один килл или смерть.")

        ffmpeg_exe = opts.get("ffmpeg_path", "")
        if not ffmpeg_exe or not Path(ffmpeg_exe).is_file():
            raise FileNotFoundError(
                "FFmpeg не найден. Укажите корректный путь к ffmpeg.exe в настройках (config.json)."
            )

        demo_path = Path(demo_data["demo_path"]).resolve()
        if not demo_path.is_file():
            raise FileNotFoundError(f"Файл демки не найден: {demo_path}")

        output_dir = Path(opts.get("output_dir") or (Path(__file__).resolve().parent / "output")).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir = output_dir / f"_temp_{int(time.time())}"
        work_dir.mkdir(parents=True, exist_ok=True)

        total_clips = len(clip_queue)
        self._emit(
            "info",
            f"Старт рендера: выбрано клипов — {total_clips}, разрешение {opts.get('width', 1920)}x{opts.get('height', 1080)} @ {opts.get('fps', 60)} FPS.",
            progress=2.0,
            clip_index=0,
            total_clips=total_clips,
            stage="init",
        )

        backend = opts.get("recording_backend", "auto")
        try:
            if backend == "simulation":
                recorded_files = self._record_batch_simulation(demo_data, clip_queue, opts, work_dir)
            else:
                # Both 'auto', 'hlae', and 'netcon_capture' use HLAE (-afxFixNetCon -netconport) + mirv_streams
                recorded_files = self._record_batch_with_hlae(demo_data, clip_queue, opts, work_dir)

            self._check_cancelled()

            # Step 3: Stitch clips into a single MP4
            self._emit(
                "info",
                f"Склейка {len(recorded_files)} клипов в единый мувик через FFmpeg...",
                progress=84.0,
                clip_index=total_clips,
                total_clips=total_clips,
                stage="stitching",
            )

            safe_map = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in demo_data.get("map_name", "cs2"))
            timestamp_str = time.strftime("%Y%m%d_%H%M%S")
            output_filename = opts.get("output_filename") or f"CS2_Movie_{safe_map}_{timestamp_str}.mp4"
            if not output_filename.lower().endswith(".mp4"):
                output_filename += ".mp4"
            final_mp4 = output_dir / output_filename

            stitched_mp4 = work_dir / "stitched_raw.mp4"
            self.concatenate_clips(
                clip_files=recorded_files,
                output_path=stitched_mp4,
                opts=opts,
            )

            # Step 4: Optional background music mixing
            music_path = str(opts.get("music_path") or "").strip()
            if music_path:
                music_file = Path(music_path)
                if not music_file.is_file():
                    raise FileNotFoundError(f"Музыкальный файл не найден: {music_file}")
                self._emit(
                    "info",
                    f"Наложение фоновой музыки ({music_file.name}, громкость {int(float(opts.get('music_volume', 0.3)) * 100)}%)...",
                    progress=93.0,
                    clip_index=total_clips,
                    total_clips=total_clips,
                    stage="music",
                )
                self.mix_background_music(
                    video_path=stitched_mp4,
                    music_path=music_file,
                    output_path=final_mp4,
                    opts=opts,
                )
            else:
                shutil.move(str(stitched_mp4), str(final_mp4))

            if not opts.get("keep_temp_files", False):
                shutil.rmtree(work_dir, ignore_errors=True)

            self._emit(
                "success",
                f"Готово! Мувик сохранён: {final_mp4}",
                progress=100.0,
                clip_index=total_clips,
                total_clips=total_clips,
                stage="done",
                output_file=str(final_mp4),
            )

            if opts.get("open_folder_after", True) and os.name == "nt":
                try:
                    subprocess.Popen(["explorer", f"/select,{final_mp4}"])
                except Exception:
                    pass

            return {
                "status": "ok",
                "output_file": str(final_mp4),
                "output_dir": str(output_dir),
                "clips_recorded": len(recorded_files),
                "backend_used": backend,
            }
        except RenderCancelledError:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._emit("warn", "Рендер был отменён пользователем.", stage="cancelled")
            raise
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._emit("error", f"Ошибка рендера: {exc}", stage="error")
            raise

    # -------------------------------------------------------------------------
    # PRIMARY CS2 RECORDING ENGINE:
    # HLAE (AfxHookSource2 + -afxFixNetCon -netconport + mirv_cmd + mirv_streams)
    # -------------------------------------------------------------------------
    def _record_batch_with_hlae(
        self,
        demo_data: Dict[str, Any],
        clip_queue: List[Dict[str, Any]],
        opts: Dict[str, Any],
        work_dir: Path,
    ) -> List[Path]:
        cs2_exe_str = opts.get("cs2_path", "")
        if not cs2_exe_str or not Path(cs2_exe_str).is_file():
            raise FileNotFoundError("Исполняемый файл CS2 (cs2.exe) не найден. Проверьте настройки.")
        cs2_exe = Path(cs2_exe_str).resolve()
        ffmpeg_exe = Path(opts["ffmpeg_path"]).resolve()

        hlae_exe_str = opts.get("hlae_path", "")
        if not hlae_exe_str or not Path(hlae_exe_str).is_file():
            self._emit("info", "HLAE не найден — автоматически скачиваем portable HLAE с GitHub...", progress=4.0)
            hlae_exe_str = download_hlae()
            opts["hlae_path"] = hlae_exe_str
            cfg = load_config()
            cfg["hlae_path"] = hlae_exe_str
            save_config(cfg)

        hlae_exe = Path(hlae_exe_str).resolve()
        hook_dll = hlae_exe.parent / "x64" / "AfxHookSource2.dll"
        if not hook_dll.is_file():
            raise FileNotFoundError(f"AfxHookSource2.dll не найден по пути: {hook_dll}")

        # Register ffmpeg.ini for HLAE mirv_streams
        hlae_ffmpeg_dir = hlae_exe.parent / "ffmpeg"
        hlae_ffmpeg_dir.mkdir(parents=True, exist_ok=True)
        (hlae_ffmpeg_dir / "ffmpeg.ini").write_text(f"[Ffmpeg]\nPath={ffmpeg_exe}\n", encoding="utf-8")

        # Kill any stale CS2/HLAE instance before launching so -netconport binds cleanly
        if self._kill_existing_game_processes():
            time.sleep(2.0)

        csgo_dir = cs2_exe.parent.parent.parent / "csgo"
        demo_src = Path(demo_data["demo_path"]).resolve()
        temp_demo_copy: Optional[Path] = None
        try:
            demo_src.relative_to(csgo_dir)
            playdemo_arg = str(demo_src.relative_to(csgo_dir)).replace("\\", "/")
        except ValueError:
            temp_demo_copy = csgo_dir / "_cs2mm_active.dem"
            if not temp_demo_copy.exists() or temp_demo_copy.stat().st_size != demo_src.stat().st_size:
                shutil.copy2(demo_src, temp_demo_copy)
            playdemo_arg = temp_demo_copy.name

        width = int(opts.get("width", 1920))
        height = int(opts.get("height", 1080))
        fps = int(opts.get("fps", 60))
        port = int(opts.get("netcon_port", 2121))
        tickrate = int(round(float(demo_data.get("tickrate", 64.0))))
        total_clips = len(clip_queue)

        # Launch CS2 into main menu first so we can set hidden cvar demo_ui_mode=0 before playdemo
        cs2_cmdline = (
            f"-insecure -novid -sw -width {width} -height {height} "
            f"-afxFixNetCon -netconport {port}"
        )

        hlae_args = [
            str(hlae_exe),
            "-noGui",
            "-autoStart",
            "-noConfig",
            "-afxDisableSteamStorage",
            "-customLoader",
            "-hookDllPath",
            str(hook_dll),
            "-programPath",
            str(cs2_exe),
            "-cmdLine",
            cs2_cmdline,
        ]

        self._emit(
            "info",
            f"Запуск CS2 через HLAE (-insecure, {width}x{height} @ {fps} FPS) для записи {total_clips} клипов за один запуск...",
            progress=6.0,
            clip_index=0,
            total_clips=total_clips,
        )

        # IMPORTANT: Do NOT pipe stdout/stderr without draining or CS2 will block when pipe buffer fills
        hlae_proc = subprocess.Popen(
            hlae_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with self._lock:
            self._active_processes.append(hlae_proc)

        sock: Optional[socket.socket] = None
        try:
            sock = self._wait_for_netcon(port, timeout=45.0)
            self._wait_for_mainmenu(sock, timeout=18.0)

            # Mute CS2 in the Windows Audio Mixer so the user hears no background sound while rendering,
            # while HLAE's startMovieWav 1 continues capturing internal engine audio into the MP4.
            self._set_cs2_windows_mute(True)

            # Disable CS2 bottom demo playback bar (demo_ui_mode = 0) and end-of-match cutscene BEFORE playdemo
            hide_demoui_js = (
                'mirv_script_exec "const idx = AdvancedfxCVar.getIndexFromName(\'demo_ui_mode\'); '
                'if (idx !== undefined) { const c = new AdvancedfxCVar(idx); c.value = 0; }"'
            )
            self._send_netcon(sock, hide_demoui_js)
            self._send_netcon(sock, "demo_ui_mode 0")
            self._send_netcon(sock, "mirv_endofmatch 0")
            self._send_netcon(sock, "cl_trueview_show_status 0")
            time.sleep(0.35)
            self._send_netcon(sock, f'playdemo "{playdemo_arg}"')

            self._emit(
                "info",
                f"Подключено к консоли CS2 (порт {port}). Загружаем карту '{demo_data.get('map_name', '')}' (звук игры в микшере отключён)...",
                progress=10.0,
                clip_index=0,
                total_clips=total_clips,
            )

            self._wait_for_demo_ingame(sock, timeout=60.0)
            self._set_cs2_windows_mute(True)
            time.sleep(1.5)

            hud_enabled = bool(opts.get("hud_enabled", False))
            xray_enabled = bool(opts.get("xray_enabled", False))
            encoder = self._resolve_encoder(opts)
            crf = int(opts.get("crf", 18))
            out_width = int(opts.get("out_width") or width)
            out_height = int(opts.get("out_height") or height)

            vf_arg = ""
            if out_width != width or out_height != height:
                vf_arg = f"-vf scale={out_width}:{out_height},setsar=1:1 "

            if encoder == "h264_nvenc":
                enc_params = f"{vf_arg}-c:v h264_nvenc -preset p4 -cq {crf} -pix_fmt yuv420p"
            else:
                enc_params = f"{vf_arg}-c:v libx264 -preset fast -crf {crf} -pix_fmt yuv420p"

            init_cmds = [
                hide_demoui_js,
                "demo_ui_mode 0",
                "mirv_endofmatch 0",
                "sv_cheats 1",
                "snd_mute_losefocus 0",
                f"volume {float(opts.get('game_volume', 0.85))}",
                "cl_demo_predict 0",
                "cl_trueview_show_status 0",
                "mirv_panorama panelstyle panelId=trueview_row opacity=0",
                "cl_hud_telemetry_frametime_show 0",
                "cl_hud_telemetry_net_misdelivery_show 0",
                "cl_hud_telemetry_ping_show 0",
                "cl_hud_telemetry_serverrecvmargin_graph_show 0",
                "r_show_build_info 0",
                f"cl_draw_only_deathnotices {0 if hud_enabled else 1}",
                f"spec_show_xray {1 if xray_enabled else 0}",
                "tv_listen_voice_indices 0",
                "tv_listen_voice_indices_h 0",
                "mirv_streams record screen enabled 1",
                "mirv_streams record startMovieWav 1",
                f"mirv_streams record fps {fps}",
            ]
            for c in init_cmds:
                self._send_netcon(sock, c)

            output_clips: List[Path] = []

            for idx, clip in enumerate(clip_queue):
                self._check_cancelled()
                self._set_cs2_windows_mute(True)
                clip_num = idx + 1
                start_tick = int(clip["start_tick"])
                end_tick = int(clip["end_tick"])
                setup_tick = max(96, start_tick - tickrate)
                spec_slot = int(clip.get("spec_slot", 1))
                spec_sid = str(clip.get("spec_steamid", ""))
                highlight_sid = str(clip.get("highlight_steamid") or spec_sid)
                duration_sec = float(clip.get("duration_sec", 4.0))

                clip_raw_dir = (work_dir / clip["clip_id"]).resolve()
                clip_raw_dir.mkdir(parents=True, exist_ok=True)
                clip_raw_unix = str(clip_raw_dir).replace("\\", "/")
                preset_name = f"cs2mmPreset{clip_num}"

                pct_base = 12.0 + (idx / total_clips) * 70.0
                self._emit(
                    "info",
                    f"[{clip_num}/{total_clips}] Подготовка клипа: {clip['title']} (тики {start_tick}..{end_tick}, камера #{spec_slot}: {clip['spec_name']})",
                    progress=pct_base,
                    clip_index=clip_num,
                    total_clips=total_clips,
                )

                # Configure stream output for this clip
                setup_cmds = [
                    "mirv_cmd clear",
                    f'mirv_streams record name "{clip_raw_unix}"',
                    f'mirv_streams settings add ffmpeg {preset_name} "{enc_params} {{QUOTE}}{clip_raw_unix}\\\\video.mp4{{QUOTE}}"',
                    f"mirv_streams record screen settings {preset_name}",
                ]
                for c in setup_cmds:
                    self._send_netcon(sock, c)

                # Jump to setup_tick (0.5s before start_tick) and wait for Demo Skipping to finish
                setup_tick = max(64, start_tick - max(24, tickrate // 2))
                self._drain_socket(sock)
                self._send_netcon(sock, f"demo_gototick {setup_tick}")
                self._wait_for_skip_done(sock, target_tick=setup_tick, timeout=15.0)

                # Switch camera POV (player slot, victim replay slot, or killer slot) after skip completes
                self._send_netcon(sock, "spec_mode 1")
                time.sleep(0.12)
                self._send_netcon(sock, f"spec_player {spec_slot}")
                self._send_netcon(sock, f"spec_show_xray {1 if xray_enabled else 0}")
                self._send_netcon(sock, "mirv_deathmsg clear")
                self._send_netcon(sock, "mirv_deathmsg filter clear")
                if highlight_sid:
                    self._send_netcon(
                        sock,
                        f"mirv_deathmsg filter add attackerMatch=x{highlight_sid} attackerIsLocal=1",
                    )
                self._send_netcon(sock, f'mirv_cmd addAtTick {start_tick} "mirv_streams record start"')
                self._send_netcon(sock, f'mirv_cmd addAtTick {end_tick} "mirv_streams record end"')
                self._send_netcon(sock, "demo_resume")

                # Wait for mirv_streams record start -> record end (Finishing movie ... done.)
                rec_timeout = max(18.0, duration_sec * 3.0 + 12.0)
                self._wait_for_recording_done(
                    sock,
                    clip_raw_dir,
                    duration_sec=duration_sec,
                    timeout=rec_timeout,
                )

                # Mux video.mp4 + take0000/audio.wav into clip_XX.mp4
                mp4s = list(clip_raw_dir.rglob("video.mp4"))
                wavs = list(clip_raw_dir.rglob("*.wav"))
                if not mp4s or mp4s[0].stat().st_size < 512:
                    raise RuntimeError(
                        f"Не найден записанный видеофайл для клипа #{clip_num} ({clip['title']})."
                    )

                muxed_clip_path = work_dir / f"{clip['clip_id']}.mp4"
                if wavs and wavs[0].stat().st_size > 100:
                    self._mux_video_and_wav(mp4s[0], wavs[0], muxed_clip_path, opts)
                else:
                    shutil.copy2(mp4s[0], muxed_clip_path)

                output_clips.append(muxed_clip_path)
                pct_done = 12.0 + ((idx + 1) / total_clips) * 70.0
                self._emit(
                    "info",
                    f"[{clip_num}/{total_clips}] Записан клип {clip['clip_id']}.mp4 ({duration_sec:.1f} сек, {muxed_clip_path.stat().st_size // 1024} КБ).",
                    progress=pct_done,
                    clip_index=clip_num,
                    total_clips=total_clips,
                )

            self._set_cs2_windows_mute(False)
            self._send_netcon(sock, "quit")
            return output_clips
        finally:
            self._set_cs2_windows_mute(False)
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            self._kill_existing_game_processes()
            if temp_demo_copy and temp_demo_copy.exists():
                try:
                    temp_demo_copy.unlink()
                except OSError:
                    pass

    def _wait_for_netcon(self, port: int, timeout: float = 45.0) -> socket.socket:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=1.0)
                s.settimeout(0.4)
                return s
            except OSError:
                time.sleep(0.4)
        raise TimeoutError(f"Не удалось подключиться к консоли CS2 на порту {port} за {int(timeout)} сек.")

    def _wait_for_mainmenu(self, sock: socket.socket, timeout: float = 18.0) -> None:
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                chunk = sock.recv(8192).decode("utf-8", errors="ignore")
                if chunk:
                    buf += chunk
                    if "CSGO_GAME_UI_STATE_MAINMENU" in buf or "loopback:1" in buf:
                        time.sleep(0.3)
                        return
            except socket.timeout:
                pass
            except OSError:
                break
        time.sleep(0.5)

    def _wait_for_demo_ingame(self, sock: socket.socket, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        buf = ""
        saw_ingame = False
        ingame_at = 0.0
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                chunk = sock.recv(8192).decode("utf-8", errors="ignore")
                if chunk:
                    buf += chunk
                    if not saw_ingame and (
                        "CSGO_GAME_UI_STATE_LOADINGSCREEN -> CSGO_GAME_UI_STATE_INGAME" in buf
                        or "Host activate: Playing Demo" in buf
                    ):
                        saw_ingame = True
                        ingame_at = time.monotonic()
                    if "Demo Skipping finished at tick 0" in buf or "CGameRules - unpaused on tick" in buf:
                        time.sleep(0.3)
                        return
            except socket.timeout:
                if saw_ingame and (time.monotonic() - ingame_at) > 4.0:
                    return
            except OSError:
                break
        raise TimeoutError("Таймаут ожидания загрузки карты демки в CS2.")

    def _wait_for_skip_done(
        self, sock: socket.socket, target_tick: Optional[int] = None, timeout: float = 15.0
    ) -> None:
        deadline = time.monotonic() + timeout
        buf = ""
        target_str = (
            f"Demo Skipping finished at tick {target_tick}"
            if target_tick is not None
            else "Demo Skipping finished at tick"
        )
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                chunk = sock.recv(8192).decode("utf-8", errors="ignore")
                if chunk:
                    buf += chunk
                    if target_str in buf or "Demo Skipping finished at tick" in buf:
                        time.sleep(0.15)
                        return
            except socket.timeout:
                pass
            except OSError:
                break
        time.sleep(0.5)

    def _wait_for_recording_done(
        self,
        sock: socket.socket,
        clip_raw_dir: Path,
        duration_sec: float = 4.0,
        timeout: float = 25.0,
    ) -> None:
        start_wall = time.monotonic()
        deadline = start_wall + timeout
        saw_starting = False
        saw_finishing = False
        manual_stop_at: Optional[float] = None
        buf = ""

        while time.monotonic() < deadline:
            self._check_cancelled()
            now = time.monotonic()
            if not saw_starting and (now - start_wall) > 1.8:
                # Fallback if mirv_cmd addAtTick did not trigger start
                self._send_netcon(sock, "mirv_streams record start")
                saw_starting = True
                # CS2 renders ~2x real-time on NVENC during mirv_streams record
                manual_stop_at = now + max(1.5, duration_sec * 0.55)

            if manual_stop_at is not None and not saw_finishing and now >= manual_stop_at:
                self._send_netcon(sock, "mirv_streams record end")
                manual_stop_at = None

            try:
                chunk = sock.recv(8192).decode("utf-8", errors="ignore")
                if chunk:
                    buf += chunk
                    if "Starting recording" in buf or "Started recording movie" in buf:
                        saw_starting = True
                    if "Finishing movie:" in buf or "Finishing recording" in buf:
                        saw_finishing = True
                    if saw_finishing and "done." in chunk:
                        time.sleep(0.25)
                        return
            except socket.timeout:
                if saw_finishing:
                    mp4s = list(clip_raw_dir.rglob("video.mp4"))
                    if mp4s and mp4s[0].stat().st_size > 1024:
                        return
            except OSError:
                break

        # Ensure recording is stopped if timeout reached
        self._send_netcon(sock, "mirv_streams record end")
        time.sleep(0.8)

    @staticmethod
    def _drain_socket(sock: socket.socket) -> None:
        sock.setblocking(False)
        try:
            while True:
                data = sock.recv(16384)
                if not data:
                    break
        except Exception:
            pass
        finally:
            sock.settimeout(0.4)

    @staticmethod
    def _send_netcon(sock: socket.socket, cmd: str) -> None:
        try:
            sock.sendall((cmd.strip() + "\n").encode("utf-8"))
        except OSError:
            pass

    def generate_hlae_queue_cfg(
        self,
        demo_data: Dict[str, Any],
        clip_queue: List[Dict[str, Any]],
        opts: Dict[str, Any],
        work_dir: Path,
    ) -> str:
        """Generate standalone HLAE mirv_cmd + mirv_streams script for reference/CLI."""
        tickrate = int(round(float(demo_data.get("tickrate", 64.0))))
        fps = int(opts.get("fps", 60))
        hud_enabled = bool(opts.get("hud_enabled", False))
        xray_enabled = bool(opts.get("xray_enabled", False))
        encoder_arg = self._resolve_encoder(opts)
        crf = int(opts.get("crf", 18))

        lines = [
            "// Auto-generated by CS2 Movie Maker",
            "sv_cheats 1",
            f"volume {float(opts.get('game_volume', 0.85))}",
            "cl_demo_predict 0",
            "cl_trueview_show_status 0",
            f"cl_draw_only_deathnotices {0 if hud_enabled else 1}",
            f"spec_show_xray {1 if xray_enabled else 0}",
            "mirv_streams record screen enabled 1",
            "mirv_streams record startMovieWav 1",
            f"mirv_streams record fps {fps}",
            "mirv_cmd clear",
        ]

        if encoder_arg == "h264_nvenc":
            enc_params = f"-c:v h264_nvenc -preset p4 -cq {crf} -pix_fmt yuv420p"
        else:
            enc_params = f"-c:v libx264 -preset fast -crf {crf} -pix_fmt yuv420p"

        for idx, clip in enumerate(clip_queue):
            start_tick = int(clip["start_tick"])
            end_tick = int(clip["end_tick"])
            setup_tick = max(96, start_tick - tickrate)
            spec_slot = int(clip.get("spec_slot", 1))
            clip_out_dir = str((work_dir / clip["clip_id"]).resolve()).replace("\\", "/")
            preset_name = f"cs2mmPreset{idx + 1}"

            lines.append(f'mirv_cmd addAtTick {setup_tick} "mirv_streams record name \\"{clip_out_dir}\\""')
            lines.append(
                f'mirv_cmd addAtTick {setup_tick} "mirv_streams settings add ffmpeg {preset_name} '
                f'\\"{enc_params} {{QUOTE}}{clip_out_dir}/video.mp4{{QUOTE}}\\""'
            )
            lines.append(f'mirv_cmd addAtTick {setup_tick} "mirv_streams record screen settings {preset_name}"')
            lines.append(f'mirv_cmd addAtTick {setup_tick + 2} "spec_mode 1; spec_player {spec_slot}"')
            lines.append(f'mirv_cmd addAtTick {start_tick} "mirv_streams record start"')
            lines.append(f'mirv_cmd addAtTick {end_tick} "mirv_streams record end"')

        return "\n".join(lines) + "\n"

    # -------------------------------------------------------------------------
    # BACKEND C: Fast Preview / Simulation Mode (for testing without launching CS2)
    # -------------------------------------------------------------------------
    def _record_batch_simulation(
        self,
        demo_data: Dict[str, Any],
        clip_queue: List[Dict[str, Any]],
        opts: Dict[str, Any],
        work_dir: Path,
    ) -> List[Path]:
        ffmpeg_exe = opts["ffmpeg_path"]
        width = int(opts.get("width", 1920))
        height = int(opts.get("height", 1080))
        fps = int(opts.get("fps", 60))
        encoder = self._resolve_encoder(opts)
        crf = int(opts.get("crf", 18))
        enc_flags = self._build_encoder_flags(encoder, crf, str(opts.get("bitrate", "20M")))

        total_clips = len(clip_queue)
        recorded_files: List[Path] = []

        for idx, clip in enumerate(clip_queue):
            self._check_cancelled()
            clip_num = idx + 1
            duration = max(1.0, float(clip.get("duration_sec", 3.5)))
            pct_start = 10.0 + (idx / total_clips) * 68.0

            pov_desc = (
                f"POV убийцы ({clip['spec_name']}, слот #{clip['spec_slot']})"
                if clip.get("pov_mode") == "killer"
                else f"POV игрока ({clip['spec_name']}, слот #{clip['spec_slot']})"
            )
            self._emit(
                "info",
                f"[{clip_num}/{total_clips}] Запись клипа: {clip['title']} | Тики {clip['start_tick']}..{clip['end_tick']} ({duration:.1f}с) | {pov_desc}",
                progress=pct_start,
                clip_index=clip_num,
                total_clips=total_clips,
            )

            bg_color = "0x111927" if clip["type"] == "kill" else "0x271217"
            freq = 440 + (idx * 55)
            out_clip_path = work_dir / f"{clip['clip_id']}.mp4"

            args = [
                ffmpeg_exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={bg_color}:s={width}x{height}:r={fps}:d={duration:.2f}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={freq}:sample_rate=48000:duration={duration:.2f}",
                "-af",
                "volume=0.08",
                "-vf",
                "format=yuv420p",
                *enc_flags,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(out_clip_path),
            ]
            res = self._run_subprocess(args, timeout=60.0)
            if res.returncode != 0:
                raise RuntimeError(f"FFmpeg ошибка при генерации клипа #{clip_num}: {res.stderr}")

            recorded_files.append(out_clip_path)
            pct_done = 10.0 + ((idx + 1) / total_clips) * 68.0
            self._emit(
                "info",
                f"[{clip_num}/{total_clips}] Клип {clip['clip_id']}.mp4 готов ({duration:.1f} сек).",
                progress=pct_done,
                clip_index=clip_num,
                total_clips=total_clips,
            )

        return recorded_files

    # -------------------------------------------------------------------------
    # FFmpeg Muxing, Concatenation (Direct / Crossfade), and Music Mixing
    # -------------------------------------------------------------------------
    def _mux_video_and_wav(
        self, video_path: Path, wav_path: Path, out_path: Path, opts: Dict[str, Any]
    ) -> None:
        ffmpeg_exe = opts["ffmpeg_path"]
        args = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(wav_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out_path),
        ]
        res = self._run_subprocess(args, timeout=60.0)
        if res.returncode != 0:
            raise RuntimeError(f"Ошибка сведения звука клипа в FFmpeg: {res.stderr}")

    def concatenate_clips(
        self,
        clip_files: List[Path],
        output_path: Path,
        opts: Dict[str, Any],
    ) -> None:
        self._check_cancelled()
        if not clip_files:
            raise ValueError("Нет записанных клипов для склейки.")

        if len(clip_files) == 1:
            shutil.copy2(clip_files[0], output_path)
            return

        ffmpeg_exe = opts["ffmpeg_path"]
        crossfade_enabled = bool(opts.get("crossfade_enabled", False))
        xfade_dur = max(0.05, min(1.5, float(opts.get("crossfade_duration", 0.2))))

        if crossfade_enabled:
            durations = [self._probe_duration(f, ffmpeg_exe) for f in clip_files]
            if all(d > xfade_dur * 2 for d in durations):
                self._concatenate_with_xfade(clip_files, durations, xfade_dur, output_path, opts)
                return

        concat_list_path = output_path.parent / "concat_list.txt"
        lines = []
        for f in clip_files:
            escaped = str(f.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat_list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        args = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(output_path),
        ]
        res = self._run_subprocess(args, timeout=120.0)
        if res.returncode != 0:
            encoder = self._resolve_encoder(opts)
            enc_flags = self._build_encoder_flags(encoder, int(opts.get("crf", 18)), str(opts.get("bitrate", "20M")))
            args_reenc = [
                ffmpeg_exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                *enc_flags,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(output_path),
            ]
            res2 = self._run_subprocess(args_reenc, timeout=240.0)
            if res2.returncode != 0:
                raise RuntimeError(f"Ошибка склейки клипов в FFmpeg: {res2.stderr}")

    def _concatenate_with_xfade(
        self,
        clip_files: List[Path],
        durations: List[float],
        xfade_dur: float,
        output_path: Path,
        opts: Dict[str, Any],
    ) -> None:
        ffmpeg_exe = opts["ffmpeg_path"]
        encoder = self._resolve_encoder(opts)
        enc_flags = self._build_encoder_flags(encoder, int(opts.get("crf", 18)), str(opts.get("bitrate", "20M")))

        inputs: List[str] = []
        for f in clip_files:
            inputs.extend(["-i", str(f)])

        v_filters: List[str] = []
        a_filters: List[str] = []
        cumulative_offset = 0.0

        for i in range(len(clip_files) - 1):
            in_v1 = "[0:v]" if i == 0 else f"[vfade{i}]"
            in_v2 = f"[{i + 1}:v]"
            out_v = f"[vfade{i + 1}]"

            in_a1 = "[0:a]" if i == 0 else f"[afade{i}]"
            in_a2 = f"[{i + 1}:a]"
            out_a = f"[afade{i + 1}]"

            cumulative_offset += durations[i] - xfade_dur
            offset_val = max(0.05, round(cumulative_offset, 3))

            v_filters.append(
                f"{in_v1}{in_v2}xfade=transition=fade:duration={xfade_dur}:offset={offset_val}{out_v}"
            )
            a_filters.append(f"{in_a1}{in_a2}acrossfade=d={xfade_dur}{out_a}")

        last_idx = len(clip_files) - 1
        filter_complex = ";".join(v_filters + a_filters)

        args = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            f"[vfade{last_idx}]",
            "-map",
            f"[afade{last_idx}]",
            *enc_flags,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
        res = self._run_subprocess(args, timeout=300.0)
        if res.returncode != 0:
            raise RuntimeError(f"Ошибка кроссфейда FFmpeg: {res.stderr}")

    def mix_background_music(
        self,
        video_path: Path,
        music_path: Path,
        output_path: Path,
        opts: Dict[str, Any],
    ) -> None:
        ffmpeg_exe = opts["ffmpeg_path"]
        music_vol = max(0.0, min(2.0, float(opts.get("music_volume", 0.3))))
        game_vol = max(0.0, min(2.0, float(opts.get("game_volume", 0.85))))

        filter_complex = (
            f"[0:a]volume={game_vol}[game_a];"
            f"[1:a]volume={music_vol}[bgm_a];"
            f"[game_a][bgm_a]amix=inputs=2:duration=first:dropout_transition=2[out_a]"
        )

        args = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-stream_loop",
            "-1",
            "-i",
            str(music_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v",
            "-map",
            "[out_a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_path),
        ]
        res = self._run_subprocess(args, timeout=180.0)
        if res.returncode != 0:
            raise RuntimeError(f"Ошибка наложения музыки в FFmpeg: {res.stderr}")

    def _probe_duration(self, file_path: Path, ffmpeg_exe: str) -> float:
        ffprobe_exe = find_ffprobe_exe(ffmpeg_exe)
        if ffprobe_exe:
            try:
                res = self._run_subprocess(
                    [
                        ffprobe_exe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(file_path),
                    ],
                    timeout=10.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return float(res.stdout.strip())
            except Exception:
                pass
        return 4.0

    def _resolve_encoder(self, opts: Dict[str, Any]) -> str:
        requested = str(opts.get("encoder", "auto")).lower()
        ffmpeg_exe = opts.get("ffmpeg_path", "")
        if requested in ("nvenc", "h264_nvenc", "auto"):
            if check_nvenc_available(ffmpeg_exe):
                return "h264_nvenc"
            return "libx264"
        return "libx264"

    @staticmethod
    def _build_encoder_flags(encoder: str, crf: int, bitrate: str) -> List[str]:
        if encoder == "h264_nvenc":
            flags = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(crf), "-pix_fmt", "yuv420p"]
            if bitrate:
                flags.extend(["-b:v", bitrate])
            return flags
        return ["-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
