"""
產生「未完賽」比賽的預測快照，寫入 predictions 表。

關鍵規則：只對「資料庫裡還沒有的比賽」寫入預測。
已經預測過的比賽不會被覆蓋，保住賽前快照的誠實性。
（若賽程有變動想重算，可用 --refresh 明確覆蓋尚未結算的 pending。）
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db import connect, existing_game_ids
from features import build_feature_table, load_strength
from model import WNBAModel


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_rows(subset: pd.DataFrame, preds: pd.DataFrame, backfilled: int,
                refresh: bool) -> int:
    conn = connect()
    have = existing_game_ids(conn)
    ts = now_iso()
    written = 0
    for gid, row in subset.iterrows():
        if gid in have and not refresh:
            continue
        p = preds.loc[gid]
        # 回填的歷史，predicted_at 記成賽程日期以示「賽前」；真正上線快照記當下時間
        predicted_at = row["date"].isoformat() if backfilled else ts
        conn.execute(
            """
            INSERT INTO predictions
              (game_id, game_date, season, home_abbr, away_abbr, home_name, away_name,
               predicted_at, p_home_win, pred_winner, pred_home_score, pred_away_score,
               pred_total, pred_margin, confidence, status, backfilled)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)
            ON CONFLICT(game_id) DO UPDATE SET
               game_date=excluded.game_date, p_home_win=excluded.p_home_win,
               pred_winner=excluded.pred_winner, pred_home_score=excluded.pred_home_score,
               pred_away_score=excluded.pred_away_score, pred_total=excluded.pred_total,
               pred_margin=excluded.pred_margin, confidence=excluded.confidence,
               predicted_at=excluded.predicted_at
               WHERE predictions.status='pending'
            """,
            (
                gid, row["date"].isoformat(), int(row["season"]),
                row["home_abbr"], row["away_abbr"], row["home_name"], row["away_name"],
                predicted_at, float(p["p_home_win"]), p["pred_winner"],
                int(p["pred_home_score"]), int(p["pred_away_score"]),
                float(p["pred_total"]), float(p["pred_margin"]), int(p["confidence"]),
                backfilled,
            ),
        )
        written += 1
    conn.commit()
    conn.close()
    return written


def purge_stale_pending(grace_days: int = 14) -> dict:
    """
    清掉「早就過了開賽時間、卻永遠等不到結算」的 pending 列。

    原本這裡是 12 小時就刪，那是個會吃掉真實資料的設計。上游 wehoop 的賽程
    parquet 一旦停更（2026 年 8 月就發生過，整整十天沒有新的完賽紀錄），
    這些比賽在我們眼裡就是「過去了但沒完賽」，於是每天被當成延賽刪掉一批。
    刪掉的不只是預測快照，還包含同一列上的盤口與紙上下注（market_* 與
    paper_* 欄位都在 predictions 這張表裡）——也就是 CLV 的原始證據。
    等上游補上資料，backfill_history 只能用 walk-forward「重建」，
    那已經不是當初賽前真的說過什麼了。

    所以現在的規則是：
      1. 有抓到盤口的（market_captured_at 不是 NULL）—— 永遠不刪。
         那是證據，寧可讓它一直掛在 pending 也不能弄丟。
      2. 其餘的等 grace_days 天再刪。延賽的比賽多留兩週只是佔一點空間，
         誤刪真實快照卻是救不回來的。

    回傳 {"deleted": n, "protected": m}，m 是「本來會被舊邏輯刪掉、
    現在保住」的筆數——這個數字長期不是 0 就代表上游又出問題了。
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).isoformat()
    soft = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    conn = connect()
    protected = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE status='pending' AND backfilled=0 "
        "AND game_date < ? AND market_captured_at IS NOT NULL", (soft,)
    ).fetchone()[0]
    cur = conn.execute(
        "DELETE FROM predictions WHERE status='pending' AND backfilled=0 "
        "AND game_date < ? AND market_captured_at IS NULL",
        (cutoff,),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"deleted": n, "protected": protected}


def generate_predictions(games: pd.DataFrame, model: WNBAModel, refresh: bool = False,
                         strength: dict | None = None) -> int:
    """對『未來、未完賽』比賽產生賽前預測快照並寫入 DB。回傳新寫入筆數。"""
    feat = build_feature_table(games, strength if strength is not None else load_strength())
    now = pd.Timestamp.now(tz="UTC")
    upcoming = feat[(~feat["completed"]) & (feat["date"] >= now)].copy()
    if upcoming.empty:
        return 0
    preds = model.predict_games(upcoming).set_index("game_id")
    return _write_rows(upcoming.set_index("game_id"), preds, backfilled=0, refresh=refresh)


def backfill_history(games: pd.DataFrame, model: WNBAModel, season: int,
                     strength: dict | None = None) -> int:
    """
    回填：對某賽季「已完賽」比賽產生『逐日 walk-forward 樣本外』賽前預測
    （標記 backfilled=1），好讓歷史回顧一開始就有『誠實』的資料。
    注意：這裡刻意不用傳入的全資料 model（那會考自己出過的題、灌水命中率），
    而是對每一天只用該日之前的資料重訓後預測。之後由 settle 補上實際結果。
    """
    from model import walk_forward_predictions
    feat = build_feature_table(games, strength if strength is not None else load_strength())
    preds = walk_forward_predictions(feat, season)
    if preds.empty:
        return 0
    done = feat[feat["completed"] & (feat["season"] == season)].copy()
    done = done[done["game_id"].isin(preds["game_id"])].set_index("game_id")
    return _write_rows(done, preds.set_index("game_id"), backfilled=1, refresh=False)
