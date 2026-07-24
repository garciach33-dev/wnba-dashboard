"""
把市場盤口接上模型：算 edge、每日更新收盤線、自動記紙上下注、結算算 CLV 與損益。

核心概念（誠實版）：
  · edge  = 模型機率 − 市場（去水錢後）機率。>0 代表模型認為這邊被低估。
  · 只有 edge 超過門檻才「自動記一注」（紙上，不是真的下）。
  · CLV（收盤線價值）= 收盤機率 − 進場機率（同一邊）。>0 = 你拿到的線贏過收盤，
    這是「長期有沒有 edge」最早、最可信的訊號——比損益早很多就會說話。
  · 進場線第一次看到就凍結（代表你下注當下的線）；收盤線每次排程刷新、開賽即定格。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db import connect
from fetch_odds import fetch_wnba_odds

EDGE_THRESHOLD = 0.03   # edge ≥ 3 個百分點才自動記一注


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_odds(game_row, odds_list):
    """
    用主客隊 + 開賽時間精準對上「同一場」，並校正主客方向。
    回傳 (market_p_home, dec_home, dec_away, total_line, dec_over, dec_under, p_over, n_books) 對齊到 game 的主隊視角，
    對不上回傳 None。
    """
    gh, ga = game_row["home_abbr"], game_row["away_abbr"]
    try:
        gd = pd.Timestamp(game_row["game_date"]).tz_convert("UTC")
    except Exception:
        gd = None
    best, best_dt = None, None
    for m in odds_list:
        pair = {m["home_abbr"], m["away_abbr"]}
        if pair != {gh, ga}:
            continue
        if gd is not None and m.get("commence_time"):
            try:
                dt = abs((pd.Timestamp(m["commence_time"]).tz_convert("UTC") - gd).total_seconds())
            except Exception:
                dt = 0
            if dt > 2 * 86400:      # 只認 2 天內的同對戰，避免同兩隊不同日撞號
                continue
        else:
            dt = 0
        if best is None or dt < best_dt:
            best, best_dt = m, dt
    if best is None:
        return None
    # 校正主客：若盤口的主隊剛好是我們的客隊，就把主隊機率與賠率對調
    flip = best["home_abbr"] != gh
    mp = best.get("market_p_home")
    dh, da = best.get("dec_home"), best.get("dec_away")
    if flip and mp is not None:
        mp = 1 - mp
        dh, da = da, dh
    return {
        "market_p_home": mp, "dec_home": dh, "dec_away": da,
        "total_line": best.get("total_line"), "dec_over": best.get("dec_over"),
        "dec_under": best.get("dec_under"), "p_over": best.get("p_over"),
        "n_books": best.get("n_books"),
    }


def update_market(model, threshold: float = EDGE_THRESHOLD, api_key: str | None = None) -> dict:
    """對未結算比賽附上市場盤口、算 edge、記紙上下注邊、刷新收盤線。"""
    odds = fetch_wnba_odds(api_key)
    if not odds:
        return {"matched": 0, "flagged_ml": 0, "flagged_total": 0}

    conn = connect()
    pend = conn.execute(
        "SELECT * FROM predictions WHERE status='pending'"
    ).fetchall()

    matched = flagged_ml = flagged_total = 0
    for r in pend:
        m = _match_odds(r, odds)
        if not m:
            continue
        matched += 1

        gid = r["game_id"]
        model_p_home = r["p_home_win"]
        mkt_p_home = m.get("market_p_home")
        mkt_line = m.get("total_line")
        mkt_p_over = m.get("p_over")

        # ---- 收盤線：每次都刷新（開賽後不會再進這裡，等於定格在最後一次）----
        conn.execute(
            "UPDATE predictions SET closing_p_home=?, closing_p_over=?, closing_total_line=? WHERE game_id=?",
            (mkt_p_home, mkt_p_over, mkt_line, gid),
        )

        # ---- 進場線：只在第一次設定，之後凍結 ----
        if r["market_captured_at"] is None:
            edge_ml = paper_ml = None
            if mkt_p_home is not None and model_p_home is not None:
                if model_p_home >= mkt_p_home:
                    edge_ml = model_p_home - mkt_p_home
                    ml_side = "home"
                else:
                    edge_ml = mkt_p_home - model_p_home
                    ml_side = "away"
                paper_ml = ml_side if edge_ml >= threshold else None
                if paper_ml:
                    flagged_ml += 1

            edge_total = paper_total = None
            if mkt_line is not None and mkt_p_over is not None:
                model_p_over = float(model.prob_over(r["pred_total"], mkt_line))
                if model_p_over >= mkt_p_over:
                    edge_total = model_p_over - mkt_p_over
                    tot_side = "over"
                else:
                    edge_total = mkt_p_over - model_p_over
                    tot_side = "under"
                paper_total = tot_side if edge_total >= threshold else None
                if paper_total:
                    flagged_total += 1

            conn.execute(
                """UPDATE predictions SET
                   market_captured_at=?, market_n_books=?,
                   market_p_home=?, market_dec_home=?, market_dec_away=?,
                   market_total_line=?, market_dec_over=?, market_dec_under=?, market_p_over=?,
                   edge_ml=?, edge_total=?, paper_ml_side=?, paper_total_side=?
                   WHERE game_id=?""",
                (_now(), m.get("n_books"),
                 mkt_p_home, m.get("dec_home"), m.get("dec_away"),
                 mkt_line, m.get("dec_over"), m.get("dec_under"), mkt_p_over,
                 edge_ml, edge_total, paper_ml, paper_total, gid),
            )
    conn.commit()
    conn.close()
    return {"matched": matched, "flagged_ml": flagged_ml, "flagged_total": flagged_total}


def settle_market() -> int:
    """對已結算、有紙上下注邊、但還沒算損益的比賽，算 CLV 與紙上損益。"""
    conn = connect()
    rows = conn.execute(
        """SELECT * FROM predictions WHERE status='final'
           AND market_captured_at IS NOT NULL
           AND (paper_ml_result IS NULL AND paper_total_result IS NULL)"""
    ).fetchall()
    n = 0
    for r in rows:
        upd = {}
        # ---- 獨贏 ----
        if r["paper_ml_side"]:
            side = r["paper_ml_side"]
            won = (side == r["actual_winner"])
            dec = r["market_dec_home"] if side == "home" else r["market_dec_away"]
            upd["paper_ml_result"] = (dec - 1.0) if won else -1.0
            entry_p = r["market_p_home"] if side == "home" else (1 - r["market_p_home"])
            close_p = r["closing_p_home"] if side == "home" else (1 - r["closing_p_home"])
            if r["closing_p_home"] is not None:
                upd["clv_ml"] = close_p - entry_p
        # ---- 大小分 ----
        if r["paper_total_side"] and r["market_total_line"] is not None:
            side = r["paper_total_side"]
            actual_total = r["actual_total"]
            line = r["market_total_line"]
            if actual_total == line:
                upd["paper_total_result"] = 0.0            # push 退注
            else:
                over_won = actual_total > line
                won = (side == "over" and over_won) or (side == "under" and not over_won)
                dec = r["market_dec_over"] if side == "over" else r["market_dec_under"]
                upd["paper_total_result"] = (dec - 1.0) if won else -1.0
            entry_p = r["market_p_over"] if side == "over" else (1 - r["market_p_over"])
            if r["closing_p_over"] is not None:
                close_p = r["closing_p_over"] if side == "over" else (1 - r["closing_p_over"])
                upd["clv_total"] = close_p - entry_p

        if upd:
            sets = ", ".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE predictions SET {sets} WHERE game_id=?",
                         (*upd.values(), r["game_id"]))
            n += 1
    conn.commit()
    conn.close()
    return n
