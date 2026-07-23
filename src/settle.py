"""
賽後結算：把已完賽比賽的實際結果補進 predictions 表，並計算誤差。

只更新結果與誤差欄位，絕不碰賽前的預測欄位。
狀態由 'pending' 轉為 'final'。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db import connect


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def settle_results(games: pd.DataFrame) -> int:
    """對已完賽、且 DB 中仍為 pending 的比賽做結算。回傳結算筆數。"""
    completed = games[games["completed"]].copy()
    if completed.empty:
        return 0

    conn = connect()
    pending = {r[0] for r in conn.execute("SELECT game_id FROM predictions WHERE status='pending'")}
    ts = now_iso()
    settled = 0
    for _, r in completed.iterrows():
        gid = r["game_id"]
        if gid not in pending:
            continue
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        actual_winner = "home" if hs > as_ else "away"
        actual_total = hs + as_
        actual_margin = hs - as_

        pr = conn.execute(
            "SELECT pred_winner, pred_total, pred_margin FROM predictions WHERE game_id=?",
            (gid,),
        ).fetchone()
        winner_correct = 1 if pr["pred_winner"] == actual_winner else 0
        total_abs_error = abs(pr["pred_total"] - actual_total)
        margin_abs_error = abs(pr["pred_margin"] - actual_margin)

        conn.execute(
            """
            UPDATE predictions SET
               status='final', actual_home_score=?, actual_away_score=?,
               actual_winner=?, actual_total=?, settled_at=?,
               winner_correct=?, total_abs_error=?, margin_abs_error=?
            WHERE game_id=?
            """,
            (hs, as_, actual_winner, actual_total, ts,
             winner_correct, total_abs_error, margin_abs_error, gid),
        )
        settled += 1
    conn.commit()
    conn.close()
    return settled
