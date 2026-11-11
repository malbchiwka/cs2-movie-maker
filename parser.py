import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from arrow_bridge import install_arrow_polars_shim

install_arrow_polars_shim()
from demoparser2 import DemoParser

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _safe_bool(val: Any) -> bool:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return int(val) != 0
    s = str(val).strip().lower()
    return s in ("true", "1", "yes")


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_str(val: Any, default: str = "") -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    s = str(val).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s if s and s.lower() != "nan" else default


def _format_round_time(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    mins = sec // 60
    rem = sec % 60
    return f"{mins:02d}:{rem:02d}"


def _team_label(team_num: Any) -> str:
    t = _safe_int(team_num, 0)
    if t == 2:
        return "T"
    if t == 3:
        return "CT"
    return "SPEC"


class CS2DemoAnalyzer:
    """Fast CS2 .dem parser built on demoparser2."""

    def __init__(self, demo_path: str | Path):
        self.demo_path = Path(demo_path).resolve()
        if not self.demo_path.exists() or not self.demo_path.is_file():
            raise FileNotFoundError(f"Файл демки не найден: {self.demo_path}")
        if self.demo_path.stat().st_size < 128:
            raise ValueError(f"Файл демки повреждён или пуст: {self.demo_path.name}")

    def _get_cache_file(self) -> Path:
        st = self.demo_path.stat()
        safe_stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", self.demo_path.stem)[:64]
        sig = f"{self.demo_path.name}|{st.st_size}|{st.st_mtime_ns}"
        digest = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
        return CACHE_DIR / f"{safe_stem}_{st.st_size}_{int(st.st_mtime)}_{digest}.json"

    def parse(self) -> Dict[str, Any]:
        cache_file = self._get_cache_file()
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and "players" in cached and "moments_by_player" in cached:
                    cached["demo_path"] = str(self.demo_path)
                    cached["demo_name"] = self.demo_path.name
                    cached["from_cache"] = True
                    return cached
            except Exception:
                pass

        t0 = time.perf_counter()
        try:
            parser = DemoParser(str(self.demo_path))
            header = parser.parse_header() or {}
        except Exception as exc:
            raise ValueError(f"Не удалось открыть демку '{self.demo_path.name}': {exc}") from exc

        # Determine tickrate (CS2 standard is 64 ticks/sec)
        tickrate = 64.0
        total_ticks = _safe_int(header.get("playback_ticks"), 0)
        playback_time = 0.0
        try:
            playback_time = float(header.get("playback_time", 0.0) or 0.0)
        except (ValueError, TypeError):
            playback_time = 0.0
        if total_ticks > 0 and playback_time > 0:
            computed_tr = round(total_ticks / playback_time)
            if computed_tr in (64, 128):
                tickrate = float(computed_tr)

        # Parse player info
        try:
            player_info_df = parser.parse_player_info()
        except Exception:
            player_info_df = pd.DataFrame()

        # Parse round boundaries and match-end cutscene markers
        round_starts = self._parse_event_safe(parser, "round_start")
        round_freeze_ends = self._parse_event_safe(parser, "round_freeze_end")
        round_ends = self._parse_event_safe(parser, "round_end")
        round_prestarts = self._parse_event_safe(parser, "round_prestart")
        win_panel_match = self._parse_event_safe(parser, "cs_win_panel_match")

        # Determine exact match end tick to strip post-match victory/defeat cutscene & world despawn deaths
        last_round_end_tick = 0
        if isinstance(round_ends, pd.DataFrame) and not round_ends.empty and "tick" in round_ends.columns:
            re_ticks = [_safe_int(t) for t in round_ends["tick"].tolist() if _safe_int(t) > 0]
            if re_ticks:
                last_round_end_tick = max(re_ticks)

        win_panel_tick = 0
        if isinstance(win_panel_match, pd.DataFrame) and not win_panel_match.empty and "tick" in win_panel_match.columns:
            wp_ticks = [_safe_int(t) for t in win_panel_match["tick"].tolist() if _safe_int(t) > 0]
            if wp_ticks:
                win_panel_tick = max(wp_ticks)

        # Parse player_death with player and game properties
        try:
            deaths_df = parser.parse_event(
                "player_death",
                player=["team_num", "user_id", "velocity_Z", "m_fFlags"],
                other=["total_rounds_played", "is_warmup_period"],
            )
        except Exception:
            try:
                deaths_df = parser.parse_event(
                    "player_death",
                    player=["team_num"],
                    other=["total_rounds_played", "is_warmup_period"],
                )
            except Exception:
                deaths_df = self._parse_event_safe(parser, "player_death")

        # Filter out post-match despawn deaths (e.g. 'world' deaths emitted during victory/defeat cutscene)
        if isinstance(deaths_df, pd.DataFrame) and not deaths_df.empty and "tick" in deaths_df.columns:
            if win_panel_tick > 0:
                deaths_df = deaths_df[deaths_df["tick"].apply(lambda t: _safe_int(t) < win_panel_tick)]
            if last_round_end_tick > 0 and "weapon" in deaths_df.columns:
                deaths_df = deaths_df[
                    ~(
                        (deaths_df["tick"].apply(lambda t: _safe_int(t) > last_round_end_tick))
                        & (deaths_df["weapon"].apply(lambda w: _safe_str(w).lower() == "world"))
                    )
                ]

        # Build player registry (name, steamid, team, slot for spec_player)
        players_map: Dict[str, Dict[str, Any]] = {}

        if isinstance(player_info_df, pd.DataFrame) and not player_info_df.empty:
            for idx, row in player_info_df.iterrows():
                sid = _safe_str(row.get("steamid"))
                if not sid or sid == "0":
                    continue
                name = _safe_str(row.get("name"), f"Player_{idx + 1}")
                team_num = _safe_int(row.get("team_number"), 0)
                # In CS2 demos, parse_player_info rows are ordered by player slot (0-based index -> slot = idx + 1)
                slot = _safe_int(row.get("user_id"), int(idx)) + 1
                players_map[sid] = {
                    "steamid": sid,
                    "name": name,
                    "team": _team_label(team_num),
                    "team_num": team_num,
                    "slot": slot,
                    "kills_count": 0,
                    "deaths_count": 0,
                    "hs_count": 0,
                    "multikills_count": 0,
                    "clutches_count": 0,
                    "agent_def_index": 0,
                    "has_custom_agent": False,
                    "agent_name": "",
                    "agent_image": f"/static/agents/default_{'t' if _team_label(team_num) == 'T' else 'ct'}.png",
                }

        # Supplement player slots & names from tick data or deaths_df if needed
        self._enrich_players_from_deaths(deaths_df, players_map)
        self._enrich_player_slots_from_ticks(parser, deaths_df, players_map)
        self._enrich_players_agent_skins(parser, deaths_df, round_starts, players_map)

        # Build round intervals: list of (round_num, start_tick, freeze_end_tick, end_tick, winner_team)
        rounds_meta = self._build_rounds_meta(
            round_starts, round_freeze_ends, round_ends, round_prestarts, deaths_df, total_ticks
        )

        # Process all non-warmup deaths
        all_death_events = self._extract_death_events(deaths_df, rounds_meta, tickrate, players_map)

        # Update max tick if header had 0
        if total_ticks <= 0 and all_death_events:
            total_ticks = max(ev["tick"] for ev in all_death_events) + int(tickrate * 10)

        # Compute safe match_end_tick right before the victory/defeat scene appears
        match_end_tick = 0
        if win_panel_tick > 0 and last_round_end_tick > 0:
            match_end_tick = min(win_panel_tick - int(tickrate * 0.4), last_round_end_tick + int(tickrate * 1.0))
        elif win_panel_tick > 0:
            match_end_tick = win_panel_tick - int(tickrate * 0.4)
        elif last_round_end_tick > 0:
            match_end_tick = last_round_end_tick + int(tickrate * 1.0)
        elif all_death_events:
            match_end_tick = max(ev["tick"] for ev in all_death_events) + int(tickrate * 1.0)

        # Detect clutches per round using kill timeline and alive counts
        clutch_rounds_by_player = self._detect_clutches(all_death_events, players_map, rounds_meta)

        # Build per-player kills, deaths, multikill groups, clutch groups
        moments_by_player: Dict[str, Dict[str, Any]] = {}
        for sid, pinfo in players_map.items():
            p_kills = [ev for ev in all_death_events if ev["attacker_steamid"] == sid and not ev["is_teamkill"]]
            p_deaths = [ev for ev in all_death_events if ev["victim_steamid"] == sid]

            pinfo["kills_count"] = len(p_kills)
            pinfo["deaths_count"] = len(p_deaths)
            pinfo["hs_count"] = sum(1 for k in p_kills if k["headshot"])

            groups = self._build_groups_for_player(
                sid, p_kills, clutch_rounds_by_player.get(sid, {}), tickrate
            )
            pinfo["multikills_count"] = sum(1 for g in groups if g["type"] == "multikill")
            pinfo["clutches_count"] = sum(1 for g in groups if g["type"] == "clutch")

            moments_by_player[sid] = {
                "kills": p_kills,
                "deaths": p_deaths,
                "groups": groups,
            }

        elapsed = round(time.perf_counter() - t0, 3)
        players_list = sorted(
            players_map.values(),
            key=lambda p: (0 if p["team"] == "CT" else (1 if p["team"] == "T" else 2), -p["kills_count"], p["name"]),
        )

        result = {
            "demo_path": str(self.demo_path),
            "demo_name": self.demo_path.name,
            "map_name": _safe_str(header.get("map_name"), "unknown_map"),
            "server_name": _safe_str(header.get("server_name"), ""),
            "tickrate": tickrate,
            "total_ticks": total_ticks,
            "match_end_tick": match_end_tick,
            "duration_seconds": round(total_ticks / tickrate, 1) if total_ticks > 0 else round(playback_time, 1),
            "rounds_count": len(rounds_meta),
            "parse_time_seconds": elapsed,
            "players": players_list,
            "moments_by_player": moments_by_player,
        }
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_file.with_suffix(".json.tmp")
            tmp_cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            tmp_cache.replace(cache_file)
        except Exception:
            pass
        return result

    @staticmethod
    def _parse_event_safe(parser: DemoParser, event_name: str) -> pd.DataFrame:
        try:
            df = parser.parse_event(event_name)
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    def _enrich_players_from_deaths(
        self, deaths_df: pd.DataFrame, players_map: Dict[str, Dict[str, Any]]
    ) -> None:
        if not isinstance(deaths_df, pd.DataFrame) or deaths_df.empty:
            return

        for _, row in deaths_df.iterrows():
            for prefix in ("attacker", "user"):
                sid = _safe_str(row.get(f"{prefix}_steamid"))
                if not sid or sid == "0":
                    continue
                name = _safe_str(row.get(f"{prefix}_name"), f"Player_{sid[-4:]}")
                team_num = _safe_int(row.get(f"{prefix}_team_num"), 0)
                uid = _safe_int(row.get(f"{prefix}_user_id"), -1)
                if sid not in players_map:
                    players_map[sid] = {
                        "steamid": sid,
                        "name": name,
                        "team": _team_label(team_num),
                        "team_num": team_num,
                        "slot": (uid + 1) if uid >= 0 else len(players_map) + 1,
                        "kills_count": 0,
                        "deaths_count": 0,
                        "hs_count": 0,
                        "multikills_count": 0,
                        "clutches_count": 0,
                        "agent_def_index": 0,
                        "has_custom_agent": False,
                        "agent_name": "",
                        "agent_image": f"/static/agents/default_{'t' if _team_label(team_num) == 'T' else 'ct'}.png",
                    }
                else:
                    if players_map[sid]["team"] == "SPEC" and team_num in (2, 3):
                        players_map[sid]["team"] = _team_label(team_num)
                        players_map[sid]["team_num"] = team_num
                    if uid >= 0:
                        players_map[sid]["slot"] = uid + 1

    def _enrich_player_slots_from_ticks(
        self,
        parser: DemoParser,
        deaths_df: pd.DataFrame,
        players_map: Dict[str, Dict[str, Any]],
    ) -> None:
        """Ensure accurate spec_player slot numbers (1..64) for CS2."""
        try:
            sample_tick = 1000
            if isinstance(deaths_df, pd.DataFrame) and not deaths_df.empty and "tick" in deaths_df.columns:
                median_tick = int(deaths_df["tick"].median())
                if median_tick > 0:
                    sample_tick = median_tick
            ticks_df = parser.parse_ticks(["user_id", "team_num"], ticks=[sample_tick])
            if isinstance(ticks_df, pd.DataFrame) and not ticks_df.empty:
                for _, row in ticks_df.iterrows():
                    sid = _safe_str(row.get("steamid"))
                    if sid in players_map:
                        uid = _safe_int(row.get("user_id"), -1)
                        if uid >= 0:
                            # In CS2, spec_player uses slot = (uid & 0xFF) + 1 or user_id + 1
                            slot_candidate = (uid & 0xFF) + 1 if uid > 64 else uid + 1
                            if 1 <= slot_candidate <= 64:
                                players_map[sid]["slot"] = slot_candidate
        except Exception:
            pass

    def _enrich_players_agent_skins(
        self,
        parser: DemoParser,
        deaths_df: pd.DataFrame,
        round_starts: pd.DataFrame,
        players_map: Dict[str, Dict[str, Any]],
    ) -> None:
        """Enrich players with their equipped character model (agent skin) and defindex."""
        sample_ticks = [2000]
        if isinstance(deaths_df, pd.DataFrame) and not deaths_df.empty and "tick" in deaths_df.columns:
            med_tick = int(deaths_df["tick"].median())
            if med_tick > 0 and med_tick not in sample_ticks:
                sample_ticks.append(med_tick)
        elif isinstance(round_starts, pd.DataFrame) and not round_starts.empty and "tick" in round_starts.columns:
            st_list = [_safe_int(t) for t in round_starts["tick"].tolist() if _safe_int(t) > 0]
            if st_list:
                sample_ticks = [st_list[0] + 64]

        player_skins: Dict[str, int] = {}
        try:
            ticks_df = parser.parse_ticks(
                ["steamid", "team_num", "CCSPlayerController.m_nPawnCharacterDefIndex"],
                ticks=sample_ticks,
            )
            if isinstance(ticks_df, pd.DataFrame) and not ticks_df.empty:
                for _, row in ticks_df.iterrows():
                    sid = _safe_str(row.get("steamid"))
                    if not sid or sid == "0":
                        continue
                    def_idx = _safe_int(row.get("CCSPlayerController.m_nPawnCharacterDefIndex"), 0)
                    if def_idx > 0:
                        if sid not in player_skins or player_skins[sid] in (0, 5036, 5037):
                            player_skins[sid] = def_idx
        except Exception:
            pass

        # Load agents_data.json
        agents_map: Dict[str, Any] = {}
        agents_json_path = Path(__file__).resolve().parent / "static" / "agents" / "agents_data.json"
        if agents_json_path.is_file():
            try:
                agents_map = json.loads(agents_json_path.read_text(encoding="utf-8"))
            except Exception:
                agents_map = {}

        agents_dir = Path(__file__).resolve().parent / "static" / "agents"

        for sid, pinfo in players_map.items():
            team = pinfo.get("team", "CT")
            def_idx = player_skins.get(sid, 0)
            pinfo["agent_def_index"] = def_idx

            default_img = f"/static/agents/default_{'t' if team == 'T' else 'ct'}.png"

            # Check if player has custom agent skin (not 0 and not default 5036/5037)
            is_custom = def_idx > 0 and def_idx not in (5036, 5037)
            if is_custom:
                agent_info = agents_map.get(str(def_idx))
                agent_png = agents_dir / f"{def_idx}.png"
                if agent_png.is_file():
                    pinfo["has_custom_agent"] = True
                    pinfo["agent_name"] = agent_info.get("name", "Custom Agent") if agent_info else "Custom Agent"
                    pinfo["agent_image"] = f"/static/agents/{def_idx}.png"
                else:
                    pinfo["has_custom_agent"] = False
                    pinfo["agent_name"] = ""
                    pinfo["agent_image"] = default_img
            else:
                pinfo["has_custom_agent"] = False
                pinfo["agent_name"] = ""
                pinfo["agent_image"] = default_img

    def _build_rounds_meta(
        self,
        round_starts: pd.DataFrame,
        round_freeze_ends: pd.DataFrame,
        round_ends: pd.DataFrame,
        round_prestarts: pd.DataFrame,
        deaths_df: pd.DataFrame,
        total_ticks: int,
    ) -> List[Dict[str, Any]]:
        """Construct clean, non-warmup round intervals sorted by tick."""
        if not isinstance(deaths_df, pd.DataFrame) or deaths_df.empty:
            return []

        # Filter out warmup deaths if column exists
        valid_deaths = deaths_df
        if "is_warmup_period" in deaths_df.columns:
            non_warmup = deaths_df[deaths_df["is_warmup_period"].apply(lambda x: not _safe_bool(x))]
            if not non_warmup.empty:
                valid_deaths = non_warmup

        # Prefer total_rounds_played from death events if available, as it is the ground truth in Source 2
        if "total_rounds_played" in valid_deaths.columns:
            rounds_dict: Dict[int, Dict[str, Any]] = {}
            freeze_ticks = (
                sorted(_safe_int(t) for t in round_freeze_ends["tick"].tolist() if _safe_int(t) > 0)
                if isinstance(round_freeze_ends, pd.DataFrame)
                and not round_freeze_ends.empty
                and "tick" in round_freeze_ends.columns
                else []
            )
            start_ticks = (
                sorted(_safe_int(t) for t in round_starts["tick"].tolist() if _safe_int(t) > 0)
                if isinstance(round_starts, pd.DataFrame)
                and not round_starts.empty
                and "tick" in round_starts.columns
                else []
            )

            for _, row in valid_deaths.iterrows():
                r_num = _safe_int(row.get("total_rounds_played"), 0) + 1
                tick = _safe_int(row.get("tick"), 0)
                if r_num not in rounds_dict:
                    rounds_dict[r_num] = {
                        "round": r_num,
                        "first_death_tick": tick,
                        "last_death_tick": tick,
                    }
                else:
                    rounds_dict[r_num]["first_death_tick"] = min(rounds_dict[r_num]["first_death_tick"], tick)
                    rounds_dict[r_num]["last_death_tick"] = max(rounds_dict[r_num]["last_death_tick"], tick)

            rounds_list: List[Dict[str, Any]] = []
            for r_num in sorted(rounds_dict.keys()):
                info = rounds_dict[r_num]
                first_dt = info["first_death_tick"]
                # Find latest freeze_end or round_start before first_death_tick
                prior_freeze = [t for t in freeze_ticks if t <= first_dt]
                prior_start = [t for t in start_ticks if t <= first_dt]
                if prior_freeze:
                    r_start_tick = prior_freeze[-1]
                elif prior_start:
                    r_start_tick = prior_start[-1]
                else:
                    r_start_tick = max(1, first_dt - 64 * 15)

                rounds_list.append(
                    {
                        "round": r_num,
                        "start_tick": r_start_tick,
                        "end_tick": info["last_death_tick"] + 64 * 5,
                    }
                )
            return rounds_list

        # Fallback: use round_start ticks directly
        start_ticks = []
        if isinstance(round_starts, pd.DataFrame) and not round_starts.empty and "tick" in round_starts.columns:
            start_ticks = sorted(set(_safe_int(t) for t in round_starts["tick"].tolist() if _safe_int(t) > 0))
        if not start_ticks:
            min_t = int(valid_deaths["tick"].min()) if "tick" in valid_deaths.columns else 1
            max_t = int(valid_deaths["tick"].max()) if "tick" in valid_deaths.columns else max(1, total_ticks)
            return [{"round": 1, "start_tick": max(1, min_t - 640), "end_tick": max_t + 640}]

        rounds_list = []
        for idx, st in enumerate(start_ticks):
            et = start_ticks[idx + 1] - 1 if idx + 1 < len(start_ticks) else max(st + 6400, total_ticks)
            rounds_list.append({"round": idx + 1, "start_tick": st, "end_tick": et})
        return rounds_list

    def _extract_death_events(
        self,
        deaths_df: pd.DataFrame,
        rounds_meta: List[Dict[str, Any]],
        tickrate: float,
        players_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(deaths_df, pd.DataFrame) or deaths_df.empty:
            return []

        rounds_by_num = {r["round"]: r for r in rounds_meta}
        events: List[Dict[str, Any]] = []

        # Sort by tick ascending
        sorted_df = deaths_df.sort_values(by="tick", kind="mergesort") if "tick" in deaths_df.columns else deaths_df

        for idx, (_, row) in enumerate(sorted_df.iterrows()):
            if "is_warmup_period" in row and _safe_bool(row.get("is_warmup_period")):
                continue

            tick = _safe_int(row.get("tick"), 0)
            if tick <= 0:
                continue

            attacker_sid = _safe_str(row.get("attacker_steamid"))
            victim_sid = _safe_str(row.get("user_steamid"))
            if not victim_sid:
                continue

            # Determine round number and time in round
            if "total_rounds_played" in row and row.get("total_rounds_played") is not None:
                round_num = _safe_int(row.get("total_rounds_played"), 0) + 1
            else:
                round_num = 1
                for r in rounds_meta:
                    if r["start_tick"] <= tick <= r["end_tick"]:
                        round_num = r["round"]
                        break

            r_meta = rounds_by_num.get(round_num)
            r_start_tick = r_meta["start_tick"] if r_meta else max(1, tick - int(tickrate * 20))
            time_in_round_sec = max(0.0, round((tick - r_start_tick) / tickrate, 1))

            attacker_info = players_map.get(attacker_sid, {})
            victim_info = players_map.get(victim_sid, {})

            attacker_name = _safe_str(
                row.get("attacker_name"), attacker_info.get("name", "World" if not attacker_sid else attacker_sid)
            )
            victim_name = _safe_str(row.get("user_name"), victim_info.get("name", victim_sid))

            attacker_team = _team_label(row.get("attacker_team_num", attacker_info.get("team_num", 0)))
            victim_team = _team_label(row.get("user_team_num", victim_info.get("team_num", 0)))
            is_teamkill = bool(attacker_sid and attacker_sid == victim_sid) or (
                attacker_team in ("CT", "T") and attacker_team == victim_team and attacker_sid != ""
            )

            weapon = _safe_str(row.get("weapon"), "unknown")
            headshot = _safe_bool(row.get("headshot"))
            thrusmoke = _safe_bool(row.get("thrusmoke"))
            noscope = _safe_bool(row.get("noscope"))
            attackerblind = _safe_bool(row.get("attackerblind"))
            assistedflash = _safe_bool(row.get("assistedflash"))
            penetrated = _safe_int(row.get("penetrated"), 0)

            # Jump / airborne detection: check attackerinair event field, m_fFlags (FL_ONGROUND = 1), or velocity_Z
            airborne = _safe_bool(row.get("attackerinair"))
            if not airborne:
                vel_z = row.get("attacker_velocity_Z")
                flags = row.get("attacker_m_fFlags")
                if flags is not None and not (isinstance(flags, float) and math.isnan(flags)):
                    airborne = (_safe_int(flags, 1) & 1) == 0
                elif vel_z is not None and not (isinstance(vel_z, float) and math.isnan(vel_z)):
                    try:
                        airborne = abs(float(vel_z)) > 15.0
                    except (ValueError, TypeError):
                        airborne = False

            tags: List[str] = []
            if headshot:
                tags.append("HS")
            if thrusmoke:
                tags.append("SMOKE")
            if attackerblind:
                tags.append("BLIND")
            if noscope:
                tags.append("NOSCOPE")
            if airborne:
                tags.append("JUMP")
            if penetrated > 0:
                tags.append("WALLBANG")

            events.append(
                {
                    "id": f"ev_{idx + 1}_{tick}",
                    "tick": tick,
                    "round": round_num,
                    "round_start_tick": r_start_tick,
                    "time_in_round_sec": time_in_round_sec,
                    "time_in_round": _format_round_time(time_in_round_sec),
                    "demo_time_sec": round(tick / tickrate, 2),
                    "attacker_steamid": attacker_sid,
                    "attacker_name": attacker_name,
                    "attacker_team": attacker_team,
                    "attacker_slot": attacker_info.get("slot", 1),
                    "victim_steamid": victim_sid,
                    "victim_name": victim_name,
                    "victim_team": victim_team,
                    "victim_slot": victim_info.get("slot", 1),
                    "weapon": weapon,
                    "headshot": headshot,
                    "thrusmoke": thrusmoke,
                    "noscope": noscope,
                    "attackerblind": attackerblind,
                    "assistedflash": assistedflash,
                    "airborne": airborne,
                    "wallbang": penetrated > 0,
                    "penetrated": penetrated,
                    "is_teamkill": is_teamkill,
                    "tags": tags,
                }
            )

        return events

    def _detect_clutches(
        self,
        all_death_events: List[Dict[str, Any]],
        players_map: Dict[str, Dict[str, Any]],
        rounds_meta: List[Dict[str, Any]],
    ) -> Dict[str, Dict[int, Dict[str, Any]]]:
        """
        Detect clutch situations (1vN won by a player):
        A player is the last surviving member of their team while >= 1 enemies are alive,
        and subsequently eliminates all remaining enemies (or survives & wins the round with >= 1 kill in 1vN).
        """
        clutches_by_player: Dict[str, Dict[int, Dict[str, Any]]] = {sid: {} for sid in players_map}

        events_by_round: Dict[int, List[Dict[str, Any]]] = {}
        for ev in all_death_events:
            events_by_round.setdefault(ev["round"], []).append(ev)

        for r_num, r_events in events_by_round.items():
            # Determine participants per team in this round
            team_rosters: Dict[str, set] = {"CT": set(), "T": set()}
            for ev in r_events:
                if ev["attacker_steamid"] and ev["attacker_team"] in ("CT", "T"):
                    team_rosters[ev["attacker_team"]].add(ev["attacker_steamid"])
                if ev["victim_steamid"] and ev["victim_team"] in ("CT", "T"):
                    team_rosters[ev["victim_team"]].add(ev["victim_steamid"])

            # Fallback to global team rosters if fewer than 2 players spotted in events
            for sid, p in players_map.items():
                if p["team"] in ("CT", "T") and len(team_rosters[p["team"]]) < 5:
                    team_rosters[p["team"]].add(sid)

            alive_ct = set(team_rosters["CT"])
            alive_t = set(team_rosters["T"])

            # Track when a player enters a 1vX state: sid -> {"enemies_count": X, "kills_in_clutch": []}
            active_clutch: Dict[str, Dict[str, Any]] = {}

            for ev in r_events:
                v_sid = ev["victim_steamid"]
                a_sid = ev["attacker_steamid"]

                # If attacker is currently in a 1vX clutch and gets an enemy kill, record it
                if a_sid in active_clutch and not ev["is_teamkill"]:
                    active_clutch[a_sid]["kills"].append(ev)

                # Remove victim from alive set
                if v_sid in alive_ct:
                    alive_ct.discard(v_sid)
                if v_sid in alive_t:
                    alive_t.discard(v_sid)

                # If a clutching player died, their clutch failed
                if v_sid in active_clutch:
                    active_clutch[v_sid]["failed"] = True

                # Check if CT now has exactly 1 alive player and T has >= 1
                if len(alive_ct) == 1 and len(alive_t) >= 1:
                    last_ct = next(iter(alive_ct))
                    if last_ct not in active_clutch:
                        active_clutch[last_ct] = {
                            "enemies_count": len(alive_t),
                            "kills": [],
                            "failed": False,
                        }
                # Check if T now has exactly 1 alive player and CT has >= 1
                if len(alive_t) == 1 and len(alive_ct) >= 1:
                    last_t = next(iter(alive_t))
                    if last_t not in active_clutch:
                        active_clutch[last_t] = {
                            "enemies_count": len(alive_ct),
                            "kills": [],
                            "failed": False,
                        }

            # Evaluate successful clutches at round end
            for sid, cdata in active_clutch.items():
                if not cdata["failed"] and len(cdata["kills"]) >= max(1, min(cdata["enemies_count"], 1)):
                    # Verify they either killed all remaining enemies or got at least 2 kills in 1vX / won 1v1
                    if len(cdata["kills"]) >= cdata["enemies_count"] or len(cdata["kills"]) >= 2:
                        clutches_by_player.setdefault(sid, {})[r_num] = {
                            "round": r_num,
                            "versus": cdata["enemies_count"],
                            "kills_count": len(cdata["kills"]),
                            "kill_ids": [k["id"] for k in cdata["kills"]],
                        }

        return clutches_by_player

    def _build_groups_for_player(
        self,
        sid: str,
        player_kills: List[Dict[str, Any]],
        player_clutches: Dict[int, Dict[str, Any]],
        tickrate: float,
    ) -> List[Dict[str, Any]]:
        """Build 1-click selectable groups for multikills (2k/3k/4k/Ace), fast streaks, and clutches."""
        groups: List[Dict[str, Any]] = []
        kills_by_round: Dict[int, List[Dict[str, Any]]] = {}
        for k in player_kills:
            kills_by_round.setdefault(k["round"], []).append(k)

        for r_num in sorted(kills_by_round.keys()):
            rkills = kills_by_round[r_num]
            count = len(rkills)
            if count >= 2:
                mk_name = "ACE (5K)" if count >= 5 else f"{count}K"
                weapons = list(dict.fromkeys(k["weapon"] for k in rkills))
                hs_cnt = sum(1 for k in rkills if k["headshot"])
                groups.append(
                    {
                        "id": f"mk_r{r_num}_{sid[-4:]}",
                        "type": "multikill",
                        "badge": "ACE" if count >= 5 else f"{count}K",
                        "label": f"{mk_name} — Раунд {r_num} ({', '.join(weapons[:2])})",
                        "round": r_num,
                        "kills_count": count,
                        "hs_count": hs_cnt,
                        "kill_ids": [k["id"] for k in rkills],
                        "first_tick": rkills[0]["tick"],
                        "last_tick": rkills[-1]["tick"],
                        "span_seconds": round((rkills[-1]["tick"] - rkills[0]["tick"]) / tickrate, 1),
                    }
                )

            if r_num in player_clutches:
                cinfo = player_clutches[r_num]
                v_cnt = cinfo["versus"]
                groups.append(
                    {
                        "id": f"clutch_r{r_num}_{sid[-4:]}",
                        "type": "clutch",
                        "badge": f"1v{v_cnt}",
                        "label": f"Клатч 1v{v_cnt} — Раунд {r_num} ({len(rkills)} килл.)",
                        "round": r_num,
                        "versus": v_cnt,
                        "kills_count": len(rkills),
                        "kill_ids": [k["id"] for k in rkills],
                        "first_tick": rkills[0]["tick"],
                        "last_tick": rkills[-1]["tick"],
                        "span_seconds": round((rkills[-1]["tick"] - rkills[0]["tick"]) / tickrate, 1),
                    }
                )

        return groups


def build_clip_queue(
    demo_data: Dict[str, Any],
    player_steamid: str,
    selected_kills: List[Dict[str, Any]],
    selected_deaths: List[Dict[str, Any]],
    merge_gap_seconds: float = 5.0,
    default_kill_pre: float = 2.0,
    default_kill_post: float = 2.0,
    default_death_pre: float = 4.0,
    default_death_post: float = 1.5,
    default_victim_replay: bool = False,
    victim_replay_pre: float = 2.5,
    victim_replay_post: float = 1.2,
) -> List[Dict[str, Any]]:
    """
    Build the ordered queue of video clips to record from CS2:
    - Neighboring kills with pause < merge_gap_seconds (default 5.0s) in the same round are automatically merged
      into a single clip: from (first_kill.tick - pre_seconds) to (last_kill.tick + post_seconds).
    - When pov_victim_replay is enabled for a kill, a replay clip from the victim's POV (spec_player <victim_slot>)
      is queued immediately after the player's own POV kill clip.
    - Deaths with pov_killer=True record from killer's POV (spec_player <attacker_slot>)
      from (death.tick - N sec) to (death.tick + M sec).
    - Deaths with pov_killer=False record from player's own POV (spec_player <victim_slot>).
    """
    tickrate = float(demo_data.get("tickrate", 64.0))
    total_ticks = int(demo_data.get("total_ticks", 0))
    match_end_tick = int(demo_data.get("match_end_tick", 0))
    players_by_sid = {p["steamid"]: p for p in demo_data.get("players", [])}
    main_player = players_by_sid.get(player_steamid, {})
    main_slot = int(main_player.get("slot", 1))
    main_name = main_player.get("name", player_steamid)

    moments = demo_data.get("moments_by_player", {}).get(player_steamid, {})
    kills_lookup = {k["id"]: k for k in moments.get("kills", [])}
    deaths_lookup = {d["id"]: d for d in moments.get("deaths", [])}

    # 1. Process and merge selected kills
    resolved_kills: List[Dict[str, Any]] = []
    for item in selected_kills:
        kid = item.get("id")
        base_k = kills_lookup.get(kid)
        if not base_k:
            continue
        pre_s = float(item.get("pre_seconds", default_kill_pre))
        post_s = float(item.get("post_seconds", default_kill_post))
        pov_victim = bool(item.get("pov_victim_replay", default_victim_replay))
        resolved_kills.append(
            {
                **base_k,
                "pre_seconds": pre_s,
                "post_seconds": post_s,
                "pov_victim_replay": pov_victim,
            }
        )

    resolved_kills.sort(key=lambda k: k["tick"])

    merged_kill_clusters: List[List[Dict[str, Any]]] = []
    for k in resolved_kills:
        if not merged_kill_clusters:
            merged_kill_clusters.append([k])
            continue
        prev_cluster = merged_kill_clusters[-1]
        prev_k = prev_cluster[-1]
        gap_sec = (k["tick"] - prev_k["tick"]) / tickrate
        if k["round"] == prev_k["round"] and gap_sec <= merge_gap_seconds:
            prev_cluster.append(k)
        else:
            merged_kill_clusters.append([k])

    clip_queue: List[Dict[str, Any]] = []

    for cluster in merged_kill_clusters:
        first_k = cluster[0]
        last_k = cluster[-1]
        pre_s = first_k["pre_seconds"]
        post_s = last_k["post_seconds"]
        start_tick = max(96, int(round(first_k["tick"] - pre_s * tickrate)))
        end_tick = int(round(last_k["tick"] + post_s * tickrate))
        if match_end_tick > 0 and last_k["tick"] <= match_end_tick:
            end_tick = min(end_tick, max(last_k["tick"] + int(tickrate * 0.7), match_end_tick))
        if total_ticks > 0:
            end_tick = min(total_ticks - 32, end_tick)
        if end_tick <= start_tick:
            end_tick = start_tick + int(tickrate * 3)

        victims = [k["victim_name"] for k in cluster]
        weapons = list(dict.fromkeys(k["weapon"] for k in cluster))
        count = len(cluster)
        label_prefix = f"{count}K" if count > 1 else "Килл"

        clip_queue.append(
            {
                "type": "kill",
                "round": first_k["round"],
                "start_tick": start_tick,
                "end_tick": end_tick,
                "_sort_tick": start_tick,
                "_sort_sub": 0,
                "duration_sec": round((end_tick - start_tick) / tickrate, 2),
                "spec_slot": main_slot,
                "spec_steamid": player_steamid,
                "highlight_steamid": player_steamid,
                "spec_name": main_name,
                "pov_mode": "self",
                "kills_count": count,
                "weapons": weapons,
                "victims": victims,
                "event_ids": [k["id"] for k in cluster],
                "title": f"Раунд {first_k['round']} | {label_prefix} ({', '.join(weapons)}) vs {', '.join(victims)}",
            }
        )

        # Append victim POV replay clip(s) immediately after the main kill clip when enabled
        for sub_idx, k_item in enumerate(cluster, start=1):
            if not k_item.get("pov_victim_replay"):
                continue
            v_sid = str(k_item.get("victim_steamid") or "")
            if not v_sid:
                continue
            v_slot = int(k_item.get("victim_slot") or players_by_sid.get(v_sid, {}).get("slot", 1))
            v_name = str(k_item.get("victim_name") or players_by_sid.get(v_sid, {}).get("name", v_sid))
            v_start = max(96, int(round(k_item["tick"] - victim_replay_pre * tickrate)))
            v_end = int(round(k_item["tick"] + victim_replay_post * tickrate))
            if match_end_tick > 0 and k_item["tick"] <= match_end_tick:
                v_end = min(v_end, max(k_item["tick"] + int(tickrate * 0.7), match_end_tick))
            if total_ticks > 0:
                v_end = min(total_ticks - 32, v_end)
            if v_end <= v_start:
                v_end = v_start + int(tickrate * 2.5)

            clip_queue.append(
                {
                    "type": "victim_replay",
                    "round": k_item["round"],
                    "start_tick": v_start,
                    "end_tick": v_end,
                    "_sort_tick": start_tick,
                    "_sort_sub": sub_idx,
                    "duration_sec": round((v_end - v_start) / tickrate, 2),
                    "spec_slot": v_slot,
                    "spec_steamid": v_sid,
                    "highlight_steamid": player_steamid,
                    "spec_name": v_name,
                    "pov_mode": "victim_replay",
                    "kills_count": 1,
                    "weapons": [k_item["weapon"]],
                    "victims": [v_name],
                    "event_ids": [k_item["id"]],
                    "title": f"Раунд {k_item['round']} | Повтор от лица жертвы: {v_name} ({k_item['weapon']})",
                }
            )

    # 2. Process selected deaths
    for item in selected_deaths:
        did = item.get("id")
        base_d = deaths_lookup.get(did)
        if not base_d:
            continue
        pov_killer = bool(item.get("pov_killer", True))
        pre_s = float(item.get("pre_seconds", default_death_pre))
        post_s = float(item.get("post_seconds", default_death_post))

        start_tick = max(96, int(round(base_d["tick"] - pre_s * tickrate)))
        end_tick = int(round(base_d["tick"] + post_s * tickrate))
        if match_end_tick > 0 and base_d["tick"] <= match_end_tick:
            end_tick = min(end_tick, max(base_d["tick"] + int(tickrate * 0.7), match_end_tick))
        if total_ticks > 0:
            end_tick = min(total_ticks - 32, end_tick)
        if end_tick <= start_tick:
            end_tick = start_tick + int(tickrate * 3)

        if pov_killer and base_d.get("attacker_steamid"):
            spec_slot = int(base_d.get("attacker_slot", 1))
            spec_steamid = base_d["attacker_steamid"]
            spec_name = base_d["attacker_name"]
            pov_mode = "killer"
            pov_label = f"POV убийцы: {spec_name}"
        else:
            spec_slot = main_slot
            spec_steamid = player_steamid
            spec_name = main_name
            pov_mode = "victim"
            pov_label = f"POV жертвы: {main_name}"

        clip_queue.append(
            {
                "type": "death",
                "round": base_d["round"],
                "start_tick": start_tick,
                "end_tick": end_tick,
                "_sort_tick": start_tick,
                "_sort_sub": 0,
                "duration_sec": round((end_tick - start_tick) / tickrate, 2),
                "spec_slot": spec_slot,
                "spec_steamid": spec_steamid,
                "highlight_steamid": spec_steamid,
                "spec_name": spec_name,
                "pov_mode": pov_mode,
                "pov_killer": pov_killer,
                "weapons": [base_d["weapon"]],
                "attacker_name": base_d["attacker_name"],
                "headshot": base_d["headshot"],
                "event_ids": [base_d["id"]],
                "title": f"Раунд {base_d['round']} | Смерть от {base_d['attacker_name']} ({base_d['weapon']}) [{pov_label}]",
            }
        )

    # Sort queue chronologically by anchor start_tick, keeping victim_replay right after its parent kill clip
    clip_queue.sort(key=lambda c: (c.get("_sort_tick", c["start_tick"]), c.get("_sort_sub", 0)))
    for idx, clip in enumerate(clip_queue):
        clip.pop("_sort_tick", None)
        clip.pop("_sort_sub", None)
        clip["index"] = idx + 1
        clip["clip_id"] = f"clip_{idx + 1:02d}"

    return clip_queue
