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
import json
import urllib.request
from pathlib import Path

import pandas as pd

RAW_BASE = (
    "https://raw.githubusercontent.com/sportsdataverse/"
    "wehoop-wnba-raw/main/wnba/schedules/parquet"
)

# ---- ESPN 備援 ---------------------------------------------------------
# wehoop 的 parquet 是排程重建的，會停更。2026 年 8 月就整整十天沒有新的
# 完賽紀錄（比賽照打，只是上游沒發布）。那十天裡整條流程等於停擺：
# 結算 0 場 → CLV 算不出來 → 球員資料同步不了 → 模型吃十天前的資料。
#
# 所以對「時間已經過了、parquet 卻還說沒完賽」的比賽，直接去問 ESPN 拿比分。
# ESPN 的 event id 跟 wehoop 的 game_id 是同一組（傷兵與名冊那兩支 API
# 已經用同一組 id 驗證過），所以可以直接對上。
#
# 這是備援不是主源：parquet 有的就用 parquet，ESPN 只補洞。
# 任何一步失敗都靜靜跳過，讓系統退回「就是沒這場資料」，不會比原本更糟。
ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/"
                   "basketball/wnba/scoreboard")
ESPN_LOOKBACK_DAYS = 21   # 只回補近三週；再舊的等 parquet 自己補
ESPN_MAX_DATES = 25       # 單次最多問幾天，避免上游長期停擺時請求爆量
ESPN_SETTLE_HOURS = 4     # 開賽後幾小時才視為「該有比分了」

LAST_ESPN_PATCH: dict = {"patched": 0, "dates_queried": 0}

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


def _espn_scores_for_date(daystr: str) -> dict[str, tuple[int, int]]:
    """問 ESPN 某一天的比分，回傳 {game_id: (主隊得分, 客隊得分)}，只收已完賽的。"""
    url = f"{ESPN_SCOREBOARD}?dates={daystr}&limit=100"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        doc = json.load(resp)

    out: dict[str, tuple[int, int]] = {}
    for ev in doc.get("events") or []:
        gid = str(ev.get("id") or "")
        if not gid:
            continue
        for comp in ev.get("competitions") or []:
            status = (comp.get("status") or {}).get("type") or {}
            if not status.get("completed"):
                continue          # 進行中、延賽、取消 —— 一律不收
            home = away = None
            for c in comp.get("competitors") or []:
                try:
                    score = int(float(c.get("score")))
                except (TypeError, ValueError):
                    continue
                if c.get("homeAway") == "home":
                    home = score
                elif c.get("homeAway") == "away":
                    away = score
            if home is not None and away is not None:
                out[gid] = (home, away)
    return out


def patch_missing_scores(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """
    把「早就開賽、parquet 卻仍標未完賽」的比賽拿 ESPN 的比分補上。
    回傳 (df, 補了幾場, 問了幾天)。任何失敗都不拋例外——備援不該弄壞主流程。
    """
    now = pd.Timestamp.now(tz="UTC")
    stale = df[(~df["completed"])
               & (df["date"] < now - pd.Timedelta(hours=ESPN_SETTLE_HOURS))
               & (df["date"] > now - pd.Timedelta(days=ESPN_LOOKBACK_DAYS))]
    if stale.empty:
        return df, 0, 0

    # ESPN 的 dates 參數吃美東日期，所以要先換算過去
    days = sorted({d.strftime("%Y%m%d")
                   for d in stale["date"].dt.tz_convert("America/New_York")})
    days = days[-ESPN_MAX_DATES:]      # 留最近的幾天，舊的優先讓 parquet 去補

    found: dict[str, tuple[int, int]] = {}
    for d in days:
        try:
            found.update(_espn_scores_for_date(d))
        except Exception:
            continue                   # 某一天失敗就跳過，其他天照補

    patched = 0
    for i in stale.index:
        hit = found.get(str(df.at[i, "game_id"]))
        if not hit:
            continue
        df.at[i, "home_score"], df.at[i, "away_score"] = float(hit[0]), float(hit[1])
        df.at[i, "completed"] = True
        patched += 1
    return df, patched, len(days)


def load_games(seasons: list[int], force_download: bool = False,
               espn_fallback: bool = True) -> pd.DataFrame:
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

    # 上游停更時的備援：拿 ESPN 的比分補上 parquet 缺的那幾天
    patched = dates_q = 0
    if espn_fallback:
        try:
            df, patched, dates_q = patch_missing_scores(df)
        except Exception:
            pass
    LAST_ESPN_PATCH.update({"patched": patched, "dates_queried": dates_q})

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
    df.attrs["espn_patch"] = dict(LAST_ESPN_PATCH)
    return df


if __name__ == "__main__":
    import sys
    seasons = [int(x) for x in sys.argv[1:]] or [2024, 2025, 2026]
    g = load_games(seasons, force_download=True)
    print(f"loaded {len(g)} games across seasons {seasons}")
    print(f"completed: {g['completed'].sum()}, upcoming: {(~g['completed']).sum()}")
    print(g.tail(3)[["date", "away_abbr", "home_abbr", "away_score", "home_score", "winner"]].to_string())
