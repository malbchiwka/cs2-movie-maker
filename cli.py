import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from config import check_environment, load_config
from parser import CS2DemoAnalyzer, build_clip_queue
from recorder import CS2MovieRecorder


def print_demo_summary(data: Dict[str, Any], target_player: str = "") -> None:
    print("=" * 92)
    print(
        f"Демка: {data['demo_name']} | Карта: {data['map_name']} | "
        f"Тикрейт: {int(data['tickrate'])} | Раундов: {data['rounds_count']} | "
        f"Время парсинга: {data['parse_time_seconds']:.3f} сек"
    )
    print("=" * 92)
    print("\n[ СПИСОК ИГРОКОВ ]")
    print(f"{'№':<3} {'Ник':<22} {'SteamID':<19} {'Команда':<7} {'Слот':<5} {'Киллы':<7} {'Смерти':<7} {'HS':<5} {'Мультикиллы':<12} {'Клатчи'}")
    print("-" * 92)

    players: List[Dict[str, Any]] = data.get("players", [])
    selected_sid = ""
    for idx, p in enumerate(players, 1):
        print(
            f"{idx:<3} {p['name'][:20]:<22} {p['steamid']:<19} {p['team']:<7} "
            f"#{p['slot']:<4} {p['kills_count']:<7} {p['deaths_count']:<7} "
            f"{p['hs_count']:<5} {p['multikills_count']:<12} {p['clutches_count']}"
        )
        if target_player and (
            target_player.lower() in p["name"].lower() or target_player == p["steamid"] or target_player == str(idx)
        ):
            selected_sid = p["steamid"]

    if not selected_sid and players:
        # Pick top fragger by default
        top_p = max(players, key=lambda x: x["kills_count"])
        selected_sid = top_p["steamid"]

    if not selected_sid:
        print("\nИгроки в демке не найдены.")
        return

    player_obj = next((p for p in players if p["steamid"] == selected_sid), players[0])
    moments = data["moments_by_player"].get(selected_sid, {})
    kills = moments.get("kills", [])
    deaths = moments.get("deaths", [])
    groups = moments.get("groups", [])

    print("\n" + "=" * 92)
    print(
        f"МОМЕНТЫ ИГРОКА: {player_obj['name']} (SteamID: {player_obj['steamid']}, "
        f"Команда: {player_obj['team']}, Слот камеры: #{player_obj['slot']})"
    )
    print("=" * 92)

    print(f"\n1) АВТО-ГРУППИРОВКА МУЛЬТИКИЛЛОВ И КЛАТЧЕЙ ({len(groups)} шт.):")
    if groups:
        for g in groups:
            print(
                f"  [x] [{g['badge']:<4}] {g['label']:<42} | "
                f"Тики: {g['first_tick']}..{g['last_tick']} ({g['span_seconds']}с) | "
                f"Киллы: {', '.join(g['kill_ids'])}"
            )
    else:
        print("  (Нет мультикиллов или клатчей)")

    print(f"\n2) ВСЕ КИЛЛЫ ИГРОКА ({len(kills)} шт.):")
    print(f"{'ID':<14} {'Раунд':<6} {'Время':<7} {'Тик':<8} {'Оружие':<12} {'Жертва':<20} {'Модификаторы (HS/Smoke/Blind/NoScope/Jump)'}")
    print("-" * 92)
    for k in kills:
        mods = ", ".join(k["tags"]) if k["tags"] else "-"
        print(
            f"{k['id']:<14} R{k['round']:<5} {k['time_in_round']:<7} {k['tick']:<8} "
            f"{k['weapon']:<12} {k['victim_name'][:18]:<20} {mods}"
        )

    print(f"\n3) ВСЕ СМЕРТИ ИГРОКА ({len(deaths)} шт.):")
    print(f"{'ID':<14} {'Раунд':<6} {'Время':<7} {'Тик':<8} {'Убийца (Слот POV)':<26} {'Оружие':<12} {'Хедшот'}")
    print("-" * 92)
    for d in deaths:
        killer_str = f"{d['attacker_name'][:18]} (#{d['attacker_slot']})"
        hs_str = "ДА (HS)" if d["headshot"] else "Нет"
        print(
            f"{d['id']:<14} R{d['round']:<5} {d['time_in_round']:<7} {d['tick']:<8} "
            f"{killer_str:<26} {d['weapon']:<12} {hs_str}"
        )


def main() -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="CS2 Movie Maker CLI")
    ap.add_argument("demo", nargs="?", help="Путь к .dem файлу CS2")
    ap.add_argument("--player", "-p", default="", help="Ник, SteamID или номер игрока")
    ap.add_argument("--check-env", action="store_true", help="Проверить пути к CS2, FFmpeg, HLAE")
    ap.add_argument("--render-test", action="store_true", help="Собрать тестовый мувик (5 киллов + 2 смерти, 1 от лица убийцы)")
    ap.add_argument("--backend", default="auto", choices=["auto", "hlae", "netcon_capture", "simulation"], help="Режим записи")
    ap.add_argument("--crossfade", action="store_true", help="Включить кроссфейд 0.2 сек при склейке")
    ap.add_argument("--music", default="", help="Путь к MP3 файлу для фоновой музыки")
    args = ap.parse_args()

    if args.check_env:
        env_info = check_environment()
        print(json.dumps(env_info, indent=2, ensure_ascii=False))
        return 0

    if not args.demo:
        ap.print_help()
        return 1

    analyzer = CS2DemoAnalyzer(args.demo)
    data = analyzer.parse()
    print_demo_summary(data, args.player)

    if args.render_test:
        players = data.get("players", [])
        if not players:
            print("Ошибка: нет игроков для рендера.")
            return 1

        target_p = max(players, key=lambda x: x["kills_count"])
        if args.player:
            for p in players:
                if args.player.lower() in p["name"].lower() or args.player == p["steamid"]:
                    target_p = p
                    break

        sid = target_p["steamid"]
        moments = data["moments_by_player"][sid]
        # Select 5 kills and 2 deaths (1 with POV killer = True, 1 with POV killer = False)
        sel_kills = [
            {"id": k["id"], "pre_seconds": 2.0, "post_seconds": 2.0}
            for k in moments["kills"][:5]
        ]
        sel_deaths = []
        for idx, d in enumerate(moments["deaths"][:2]):
            sel_deaths.append(
                {
                    "id": d["id"],
                    "pov_killer": (idx == 0),  # First death from killer POV, second from victim POV
                    "pre_seconds": 4.0,
                    "post_seconds": 1.5,
                }
            )

        queue = build_clip_queue(
            demo_data=data,
            player_steamid=sid,
            selected_kills=sel_kills,
            selected_deaths=sel_deaths,
            merge_gap_seconds=5.0,
        )

        print("\n" + "=" * 92)
        print(f"ОЧЕРЕДЬ РЕНДЕРА ({len(queue)} клипов после слияния соседних киллов <5с):")
        for c in queue:
            print(
                f"  #{c['index']} [{c['clip_id']}] {c['title']} | "
                f"Тики: {c['start_tick']}..{c['end_tick']} ({c['duration_sec']}с) | "
                f"spec_player {c['spec_slot']} ({c['spec_name']})"
            )
        print("=" * 92)

        def log_cb(evt: Dict[str, Any]) -> None:
            pct_str = f"{evt.get('progress', 0):5.1f}%" if "progress" in evt else "  -  "
            print(f"[{evt['timestamp']}] [{pct_str}] {evt['message']}")

        cfg = load_config()
        recorder = CS2MovieRecorder(config=cfg, on_progress=log_cb)
        res = recorder.render_movie(
            demo_data=data,
            clip_queue=queue,
            render_options={
                "recording_backend": args.backend,
                "crossfade_enabled": args.crossfade,
                "music_path": args.music,
                "open_folder_after": False,
            },
        )
        print(f"\nУспешно создан файл: {res['output_file']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
