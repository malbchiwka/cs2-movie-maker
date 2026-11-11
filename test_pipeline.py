import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from config import check_environment, load_config
from parser import CS2DemoAnalyzer, build_clip_queue
from recorder import CS2MovieRecorder

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DEMO = BASE_DIR / "samples" / "test_demo.dem"


def create_sample_mp3(ffmpeg_exe: str, out_mp3: Path) -> None:
    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=44100:duration=12",
            "-af",
            "volume=0.15",
            str(out_mp3),
        ],
        check=True,
    )


def run_all_tests() -> None:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=== [1/5] Проверка окружения и авто-поиска (CS2, FFmpeg, NVENC) ===")
    cfg = load_config()
    env = check_environment(cfg)
    assert env["cs2_ok"], f"CS2 не найден: {env}"
    assert env["ffmpeg_ok"], f"FFmpeg не найден: {env}"
    print(f"OK: CS2={env['cs2_path']}")
    print(f"OK: FFmpeg={env['ffmpeg_path']} (NVENC={env['nvenc_available']})")

    print("\n=== [2/5] Тест парсера (Этап 2: < 10 сек на 30 раундов) ===")
    analyzer = CS2DemoAnalyzer(SAMPLE_DEMO)
    demo_data = analyzer.parse()
    parse_sec = demo_data["parse_time_seconds"]
    assert parse_sec < 10.0, f"Парсинг занял слишком много времени: {parse_sec} сек"
    assert len(demo_data["players"]) >= 2, "Не найдены игроки в демке"
    print(
        f"OK: Спарсено за {parse_sec} сек! Карта: {demo_data['map_name']}, "
        f"Раундов: {demo_data['rounds_count']}, Игроков: {len(demo_data['players'])}"
    )

    top_player = max(demo_data["players"], key=lambda p: p["kills_count"])
    sid = top_player["steamid"]
    moments = demo_data["moments_by_player"][sid]
    print(
        f"OK: Выбран игрок {top_player['name']} (SteamID={sid}, слот=#{top_player['slot']}): "
        f"Киллов={len(moments['kills'])}, Смертей={len(moments['deaths'])}, Групп={len(moments['groups'])}"
    )

    print("\n=== [3/5] Тест Этапов 3 и 4 (POV игрока на килле + POV убийцы на смерти) ===")
    sel_kills = [{"id": k["id"], "pre_seconds": 2.0, "post_seconds": 2.0} for k in moments["kills"][:5]]
    sel_deaths = [
        {"id": moments["deaths"][0]["id"], "pov_killer": True, "pre_seconds": 4.0, "post_seconds": 1.5},
        {"id": moments["deaths"][1]["id"], "pov_killer": False, "pre_seconds": 4.0, "post_seconds": 1.5},
    ]
    queue = build_clip_queue(
        demo_data=demo_data,
        player_steamid=sid,
        selected_kills=sel_kills,
        selected_deaths=sel_deaths,
        merge_gap_seconds=5.0,
    )
    assert len(queue) >= 3, "Очередь клипов не сформирована"
    death_killer_clips = [c for c in queue if c["type"] == "death" and c["pov_mode"] == "killer"]
    death_victim_clips = [c for c in queue if c["type"] == "death" and c["pov_mode"] == "victim"]
    assert len(death_killer_clips) == 1, "Клип смерти с POV убийцы не найден"
    assert len(death_victim_clips) == 1, "Клип смерти с POV жертвы не найден"

    # Verify HLAE mirv_cmd + mirv_streams script generation
    recorder = CS2MovieRecorder(config=cfg)
    hlae_cfg_text = recorder.generate_hlae_queue_cfg(demo_data, queue, cfg, BASE_DIR / "output" / "_test_work")
    assert "mirv_streams record start" in hlae_cfg_text
    assert f"spec_player {death_killer_clips[0]['spec_slot']}" in hlae_cfg_text
    print(
        f"OK: Сформирована очередь из {len(queue)} клипов (5 киллов с авто-слиянием <5с + 2 смерти). "
        f"POV убийцы переключается на слот #{death_killer_clips[0]['spec_slot']} ({death_killer_clips[0]['spec_name']})."
    )

    print("\n=== [4/5] Тест Этапа 5 и 7 (Рендер 5 киллов + 2 смертей, склейка с кроссфейдом 0.2с и MP3 музыкой) ===")
    sample_mp3 = BASE_DIR / "samples" / "test_bgm.mp3"
    create_sample_mp3(cfg["ffmpeg_path"], sample_mp3)

    res = recorder.render_movie(
        demo_data=demo_data,
        clip_queue=queue,
        render_options={
            "recording_backend": "simulation",
            "crossfade_enabled": True,
            "crossfade_duration": 0.2,
            "music_path": str(sample_mp3),
            "music_volume": 0.35,
            "open_folder_after": False,
            "output_filename": "CS2_Test_5Kills_2Deaths_Movie.mp4",
        },
    )
    out_mp4 = Path(res["output_file"])
    assert out_mp4.is_file() and out_mp4.stat().st_size > 10000, "Итоговый MP4 файл не создан или пуст"
    print(f"OK: Итоговый MP4 создан: {out_mp4} ({out_mp4.stat().st_size // 1024} КБ)")

    print("\n=== [5/5] Тест обработки ошибок (битая демка, несуществующий путь) ===")
    try:
        CS2DemoAnalyzer(BASE_DIR / "non_existent_demo.dem").parse()
        raise AssertionError("Должна была возникнуть ошибка FileNotFoundError")
    except FileNotFoundError as e:
        print(f"OK (Отсутствующая демка): {e}")

    corrupted_path = BASE_DIR / "samples" / "corrupted.dem"
    corrupted_path.write_bytes(b"NOT_A_VALID_CS2_DEMO_HEADER" * 20)
    try:
        CS2DemoAnalyzer(corrupted_path).parse()
        raise AssertionError("Должна была возникнуть ошибка ValueError на битой демке")
    except ValueError as e:
        print(f"OK (Битая демка): {e}")
    finally:
        corrupted_path.unlink(missing_ok=True)

    print("\nВСЕ ТЕСТЫ УСПЕШНО ПРОЙДЕНЫ!")


if __name__ == "__main__":
    run_all_tests()
