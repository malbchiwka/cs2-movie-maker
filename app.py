import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import (
    BASE_DIR,
    check_environment,
    download_hlae,
    load_config,
    save_config,
)
from downloader import _extract_archive_if_needed, download_demo_from_url, is_url
from parser import CS2DemoAnalyzer, build_clip_queue
from recorder import CS2MovieRecorder, RenderCancelledError

STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = BASE_DIR / "uploads"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="CS2 Movie Maker", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AppState:
    def __init__(self) -> None:
        self.current_demo: Optional[Dict[str, Any]] = None
        self.active_recorder: Optional[CS2MovieRecorder] = None
        self.render_status: Dict[str, Any] = {
            "running": False,
            "stage": "idle",
            "progress": 0.0,
            "clip_index": 0,
            "total_clips": 0,
            "output_file": None,
            "error": None,
            "logs": [],
        }
        self.lock = threading.Lock()

    def push_log(self, evt: Dict[str, Any]) -> None:
        with self.lock:
            self.render_status["logs"].append(evt)
            if "progress" in evt:
                self.render_status["progress"] = evt["progress"]
            if "clip_index" in evt:
                self.render_status["clip_index"] = evt["clip_index"]
            if "total_clips" in evt:
                self.render_status["total_clips"] = evt["total_clips"]
            if "stage" in evt:
                self.render_status["stage"] = evt["stage"]
            if evt.get("output_file"):
                self.render_status["output_file"] = evt["output_file"]
            if evt.get("level") == "error":
                self.render_status["error"] = evt.get("message")


    def ensure_demo_loaded(self, preferred_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if preferred_path:
            p = Path(preferred_path)
            if p.is_file():
                if self.current_demo is None or str(p.resolve()) != str(self.current_demo.get("demo_path", "")):
                    try:
                        analyzer = CS2DemoAnalyzer(p)
                        self.current_demo = analyzer.parse()
                    except Exception:
                        pass
        if self.current_demo is not None:
            return self.current_demo
        # Auto-load most recently uploaded .dem if present
        dem_files = sorted(
            UPLOADS_DIR.glob("*.dem"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if dem_files:
            try:
                analyzer = CS2DemoAnalyzer(dem_files[0])
                self.current_demo = analyzer.parse()
            except Exception:
                pass
        return self.current_demo


state = AppState()


class LoadDemoPathRequest(BaseModel):
    path: str


class PreviewQueueRequest(BaseModel):
    demo_path: Optional[str] = None
    player_steamid: str
    selected_kills: List[Dict[str, Any]]
    selected_deaths: List[Dict[str, Any]]
    merge_gap_seconds: float = 5.0
    default_kill_pre: float = 2.0
    default_kill_post: float = 2.0
    default_death_pre: float = 4.0
    default_death_post: float = 1.5
    default_victim_replay: bool = False


class StartRenderRequest(BaseModel):
    demo_path: Optional[str] = None
    player_steamid: str
    selected_kills: List[Dict[str, Any]]
    selected_deaths: List[Dict[str, Any]]
    merge_gap_seconds: float = 5.0
    default_victim_replay: bool = False
    render_options: Dict[str, Any] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/env")
def get_env_status() -> Dict[str, Any]:
    cfg = load_config()
    env = check_environment(cfg)
    sample_demo = BASE_DIR / "samples" / "test_demo.dem"
    return {
        "config": cfg,
        "environment": env,
        "has_sample_demo": sample_demo.is_file(),
        "sample_demo_path": str(sample_demo) if sample_demo.is_file() else "",
    }


@app.post("/api/config")
def update_config_endpoint(new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    current = load_config()
    current.update(new_cfg)
    saved = save_config(current)
    return {"config": saved, "environment": check_environment(saved)}


@app.post("/api/hlae/download")
def download_hlae_endpoint() -> Dict[str, Any]:
    try:
        hlae_exe = download_hlae()
        cfg = load_config()
        cfg["hlae_path"] = hlae_exe
        cfg["recording_backend"] = "hlae"
        save_config(cfg)
        return {"status": "ok", "hlae_path": hlae_exe, "environment": check_environment(cfg)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Ошибка скачивания HLAE: {exc}") from exc


@app.post("/api/demo/load-path")
def load_demo_by_path(req: LoadDemoPathRequest) -> Dict[str, Any]:
    raw_path = req.path.strip().strip('"')
    if not raw_path:
        raise HTTPException(status_code=400, detail="Путь или ссылка на демку не указаны.")
    try:
        if is_url(raw_path):
            resolved_dem = download_demo_from_url(raw_path, UPLOADS_DIR)
        else:
            p = Path(raw_path)
            if not p.is_file():
                if (UPLOADS_DIR / raw_path).is_file():
                    p = UPLOADS_DIR / raw_path
                else:
                    from downloader import _get_cs2_replays_dirs
                    for rdir in _get_cs2_replays_dirs():
                        if (rdir / raw_path).is_file():
                            p = rdir / raw_path
                            break
            if not p.is_file():
                raise FileNotFoundError(f"Файл не найден: {raw_path}")
            if p.name.lower().endswith((".bz2", ".gz", ".zip")):
                resolved_dem = _extract_archive_if_needed(p, UPLOADS_DIR)
            else:
                resolved_dem = p

        analyzer = CS2DemoAnalyzer(resolved_dem)
        data = analyzer.parse()
        state.current_demo = data
        return data
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=400,
            detail=f"Ошибка загрузки/чтения демки: {exc}",
        ) from exc


@app.post("/api/demo/upload")
async def upload_demo_file(file: UploadFile = File(...)) -> Dict[str, Any]:
    filename = file.filename or "uploaded.dem"
    low_name = filename.lower()
    if not low_name.endswith((".dem", ".bz2", ".gz", ".zip")):
        raise HTTPException(
            status_code=400,
            detail="Пожалуйста, выберите файл .dem или архив демки (.dem.bz2, .dem.gz, .zip)",
        )

    dest_path = UPLOADS_DIR / filename
    try:
        with open(dest_path, "wb") as out_f:
            shutil.copyfileobj(file.file, out_f)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить файл демки: {exc}") from exc

    try:
        resolved_dem = _extract_archive_if_needed(dest_path, UPLOADS_DIR)
        analyzer = CS2DemoAnalyzer(resolved_dem)
        data = analyzer.parse()
        state.current_demo = data
        return data
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Ошибка парсинга демки '{filename}': {exc}",
        ) from exc


@app.post("/api/music/upload")
async def upload_music_file(file: UploadFile = File(...)) -> Dict[str, Any]:
    filename = file.filename or "music.mp3"
    dest_path = UPLOADS_DIR / filename
    try:
        with open(dest_path, "wb") as out_f:
            shutil.copyfileobj(file.file, out_f)
        cfg = load_config()
        cfg["music_path"] = str(dest_path.resolve())
        save_config(cfg)
        return {"status": "ok", "music_path": str(dest_path.resolve()), "filename": filename}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить MP3 файл: {exc}") from exc


@app.get("/api/demo/current")
def get_current_demo() -> Dict[str, Any]:
    demo = state.ensure_demo_loaded()
    if not demo:
        return {"loaded": False, "demo": None}
    return {"loaded": True, "demo": demo}


@app.post("/api/queue/preview")
def preview_clip_queue(req: PreviewQueueRequest) -> Dict[str, Any]:
    demo = state.ensure_demo_loaded(req.demo_path)
    if not demo:
        raise HTTPException(status_code=400, detail="Сначала загрузите .dem файл.")
    queue = build_clip_queue(
        demo_data=demo,
        player_steamid=req.player_steamid,
        selected_kills=req.selected_kills,
        selected_deaths=req.selected_deaths,
        merge_gap_seconds=req.merge_gap_seconds,
        default_kill_pre=req.default_kill_pre,
        default_kill_post=req.default_kill_post,
        default_death_pre=req.default_death_pre,
        default_death_post=req.default_death_post,
        default_victim_replay=req.default_victim_replay,
    )
    total_duration = round(sum(c["duration_sec"] for c in queue), 1)
    return {"queue": queue, "total_clips": len(queue), "total_duration_sec": total_duration}


@app.post("/api/render/start")
def start_render_endpoint(req: StartRenderRequest) -> Dict[str, Any]:
    demo = state.ensure_demo_loaded(req.demo_path)
    if not demo:
        raise HTTPException(status_code=400, detail="Сначала загрузите демку (.dem).")

    with state.lock:
        if state.render_status["running"]:
            raise HTTPException(status_code=409, detail="Рендер уже выполняется.")

    cfg = load_config()
    merged_opts = {**cfg, **(req.render_options or {})}

    queue = build_clip_queue(
        demo_data=state.current_demo,
        player_steamid=req.player_steamid,
        selected_kills=req.selected_kills,
        selected_deaths=req.selected_deaths,
        merge_gap_seconds=float(req.merge_gap_seconds),
        default_kill_pre=float(merged_opts.get("kill_pre_seconds", 2.0)),
        default_kill_post=float(merged_opts.get("kill_post_seconds", 2.0)),
        default_death_pre=float(merged_opts.get("death_pre_seconds", 4.0)),
        default_death_post=float(merged_opts.get("death_post_seconds", 1.5)),
        default_victim_replay=bool(req.default_victim_replay),
    )

    if not queue:
        raise HTTPException(status_code=400, detail="Не выбран ни один момент для рендера.")

    with state.lock:
        state.render_status = {
            "running": True,
            "stage": "starting",
            "progress": 1.0,
            "clip_index": 0,
            "total_clips": len(queue),
            "output_file": None,
            "error": None,
            "logs": [],
            "queue": queue,
        }

    recorder = CS2MovieRecorder(config=merged_opts, on_progress=state.push_log)
    state.active_recorder = recorder
    demo_snapshot = state.current_demo

    def worker() -> None:
        try:
            res = recorder.render_movie(
                demo_data=demo_snapshot,
                clip_queue=queue,
                render_options=merged_opts,
            )
            with state.lock:
                state.render_status["running"] = False
                state.render_status["stage"] = "done"
                state.render_status["progress"] = 100.0
                state.render_status["output_file"] = res.get("output_file")
        except RenderCancelledError as exc:
            with state.lock:
                state.render_status["running"] = False
                state.render_status["stage"] = "cancelled"
                state.render_status["error"] = str(exc)
        except Exception as exc:
            with state.lock:
                state.render_status["running"] = False
                state.render_status["stage"] = "error"
                state.render_status["error"] = str(exc)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return {"status": "started", "total_clips": len(queue), "queue": queue}


@app.post("/api/render/cancel")
def cancel_render_endpoint() -> Dict[str, Any]:
    if state.active_recorder:
        state.active_recorder.cancel()
    with state.lock:
        state.render_status["running"] = False
        state.render_status["stage"] = "cancelled"
    return {"status": "cancelled"}


@app.get("/api/render/status")
def get_render_status_endpoint() -> Dict[str, Any]:
    with state.lock:
        return dict(state.render_status)


@app.post("/api/open-folder")
def open_output_folder_endpoint(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = load_config()
    file_path = (payload or {}).get("file_path") or state.render_status.get("output_file")
    out_dir = Path(cfg.get("output_dir") or (BASE_DIR / "output")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        opened = False
        if file_path and Path(file_path).exists():
            try:
                subprocess.Popen(f'explorer.exe /select,"{Path(file_path).resolve()}"', shell=True)
                opened = True
            except Exception:
                pass
        if not opened:
            try:
                os.startfile(str(out_dir))
            except Exception:
                try:
                    subprocess.Popen(["explorer.exe", str(out_dir)])
                except Exception:
                    pass
    return {"status": "ok", "folder": str(out_dir)}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    port = int(os.environ.get("CS2MM_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    print(f"CS2 Movie Maker запущен: {url}")

    # Open desktop window via pywebview if installed, otherwise default browser in app mode
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
