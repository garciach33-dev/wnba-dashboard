"""
從 sportsdataverse (wehoop) 的 GitHub raw 資料倉庫下載 WNBA 賽程與比分。

資料來源: https://github.com/sportsdataverse/wehoop-wnba-raw
這是免費、開源、專為女籃整理的資料，底層來自 ESPN。
每一季一個 parquet，含每場比賽的主客隊、最終比分、勝負、完賽狀態。

之後若要換成即時來源（ESPN scoreboard API），只要改這支檔案，
讓它回傳同樣欄位的 DataFrame，其他模組都不用動。
"""
from __future__ import annotations

import io
import urllib.request
from pathlib import Path

import pandas as pd

RAW_BASE = (
    "https://raw.githubusercontent.com/sportsdataverse/"
    "wehoop-wnba-raw/main/wnba/schedules/parquet"
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# 現役 WNBA 球隊縮寫（排除明星賽隊伍如 COOP / SPO）
VALID_TEAMS = {
    "ATL", "CHI", "CON", "DAL", "GS", "IND", "LA", "LV",
    "MIN", "NY", "PHX", "SEA", "WSH", "TOR", "POR",
}


def download_season(season: int, force: bool = False) -> Path:
    """下載單一賽季的 schedule parquet，存到 data/raw/。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / f"wnba_schedule_{season}.parquet"
    if dest.exists() and not force:
        return dest
    url = f"{RAW_BASE}/wnba_schedule_{season}.parquet"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return dest


def load_games(seasons: list[int], force_download: bool = False) -> pd.DataFrame:
    """
    下載並合併多個賽季，回傳乾淨的 games DataFrame。

    欄位:
      game_id, date (UTC, tz-aware), season, season_type,
      home_abbr, away_abbr, home_name, away_name,
      home_score, away_score, completed (bool),
      winner ('home'/'away'/None), total, margin (home-away)
    """
    frames = []
    for s in seasons:
        p = download_season(s, force=force_download)
        frames.append(pd.read_parquet(p))
    raw = pd.concat(frames, ignore_index=True)

    df = pd.DataFrame({
        "game_id": raw["game_id"].astype(str),
        "date": pd.to_datetime(raw["date"], utc=True),
        "season": raw["season"].astype(int),
        "season_type": raw["season_type"].astype(int),
        "home_abbr": raw["home_abbreviation"],
        "away_abbr": raw["away_abbreviation"],
        "home_name": raw["home_display_name"],
        "away_name": raw["away_display_name"],
        "home_score": pd.to_numeric(raw["home_score"], errors="coerce"),
        "away_score": pd.to_numeric(raw["away_score"], errors="coerce"),
        "completed": raw["status_type_completed"].fillna(False).astype(bool),
    })

    # 只留例行賽/季後賽(2,3) 且雙方都是現役球隊的比賽
    df = df[df["season_type"].isin([2, 3])]
    df = df[df["home_abbr"].isin(VALID_TEAMS) & df["away_abbr"].isin(VALID_TEAMS)]
    df = df.dropna(subset=["home_abbr", "away_abbr", "date"])
    df = df.drop_duplicates(subset=["game_id"]).sort_values("date").reset_index(drop=True)

    # 衍生欄位（只有完賽才有意義）
    comp = df["completed"] & df["home_score"].notna() & df["away_score"].notna()
    df["winner"] = None
    df.loc[comp, "winner"] = df.loc[comp].apply(
        lambda r: "home" if r["home_score"] > r["away_score"] else "away", axis=1
    )
    df["total"] = (df["home_score"] + df["away_score"]).where(comp)
    df["margin"] = (df["home_score"] - df["away_score"]).where(comp)
    # 未完賽把比分清成 NA，避免 0:0 汙染
    df.loc[~comp, ["home_score", "away_score"]] = pd.NA
    df["completed"] = comp
    return df


if __name__ == "__main__":
    import sys
    seasons = [int(x) for x in sys.argv[1:]] or [2024, 2025, 2026]
    g = load_games(seasons, force_download=True)
    print(f"loaded {len(g)} games across seasons {seasons}")
    print(f"completed: {g['completed'].sum()}, upcoming: {(~g['completed']).sum()}")
    print(g.tail(3)[["date", "away_abbr", "home_abbr", "away_score", "home_score", "winner"]].to_string())
