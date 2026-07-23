"""
特徵工程 —— 嚴格避免資料洩漏（leakage）。

核心原則：每一場比賽的特徵，只能用「該場開賽日之前」已完賽的比賽算出來。
做法是把所有比賽依時間排序，用一次線性掃描，維護每隊的歷史戰績；
輪到某場比賽時，先用「當下」的歷史算特徵，算完才把這場結果併回歷史。
這樣未來資訊不可能滲進特徵裡。

對每支球隊，用最近 N 場算滾動特徵：
  - 場均得分 / 場均失分（近況攻防）
  - 勝率
  - 休息天數（距上一場幾天，WNBA 背靠背影響大）
  - 已賽場數（賽季早期樣本少，模型可據此調整信心）
主客場的差異由模型自己從 home/away 兩組對稱特徵學到。
"""
from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
import pandas as pd

ROLL_N = 8          # 滾動視窗場數
LEAGUE_PTS = 82.0   # 早期無歷史時的聯盟基準得分（先驗）

FEATURE_COLS = [
    "h_pts_for", "h_pts_against", "h_win_pct", "h_rest", "h_gp",
    "a_pts_for", "a_pts_against", "a_win_pct", "a_rest", "a_gp",
    "form_diff",      # (主隊淨得分) - (客隊淨得分)
]


def _team_features(hist: deque, last_date, game_date):
    """從一支球隊的近況佇列算特徵。hist 內每筆是 (pts_for, pts_against, win)。"""
    gp = len(hist)
    if gp == 0:
        pts_for = pts_against = LEAGUE_PTS
        win_pct = 0.5
    else:
        arr = np.array(hist, dtype=float)
        pts_for = arr[:, 0].mean()
        pts_against = arr[:, 1].mean()
        win_pct = arr[:, 2].mean()
    if last_date is None:
        rest = 4.0  # 賽季首戰給中性休息值
    else:
        rest = (game_date - last_date).total_seconds() / 86400.0
        rest = float(min(rest, 10.0))  # 蓋掉全明星/賽季間過長的休息
    return pts_for, pts_against, win_pct, rest, gp


def build_feature_table(games: pd.DataFrame) -> pd.DataFrame:
    """
    對所有比賽（含未完賽）產生 pre-game 特徵表。
    回傳 games 加上 FEATURE_COLS，順序與輸入一致（依日期）。
    """
    g = games.sort_values("date").reset_index(drop=True)

    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLL_N))
    last_date: dict[str, object] = defaultdict(lambda: None)

    rows = []
    for _, r in g.iterrows():
        h, a, d = r["home_abbr"], r["away_abbr"], r["date"]
        hf = _team_features(hist[h], last_date[h], d)
        af = _team_features(hist[a], last_date[a], d)
        form_diff = (hf[0] - hf[1]) - (af[0] - af[1])
        rows.append([*hf, *af, form_diff])

        # 算完特徵後，才把「已完賽」的結果併回歷史（未完賽不併）
        if bool(r["completed"]):
            hs, as_ = float(r["home_score"]), float(r["away_score"])
            hist[h].append((hs, as_, 1.0 if hs > as_ else 0.0))
            hist[a].append((as_, hs, 1.0 if as_ > hs else 0.0))
            last_date[h] = d
            last_date[a] = d

    feats = pd.DataFrame(rows, columns=FEATURE_COLS, index=g.index)
    return pd.concat([g, feats], axis=1)


if __name__ == "__main__":
    from fetch import load_games
    g = load_games([2024, 2025, 2026])
    ft = build_feature_table(g)
    print(ft[ft["completed"]].tail(3)[["date", "home_abbr", "away_abbr", *FEATURE_COLS]].to_string())
