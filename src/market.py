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

# 開賽前幾小時之內才「開倉」（凍結進場線、決定要不要記注）。
#
# 這條原本沒有，後果很嚴重：進場線是「第一次看到就凍結」，而盤口在開賽前
# 兩三個星期就撈得到，所以實測 130 注裡有 118 注的進場線是開賽七天以上前記的，
# 中位數 19 天。那不是你真的下得到的價，只是一個很早期、很稀薄的報價。
#
# 拿那種線去算 CLV 會得到假的好消息：早期線本來就偏離真值，收盤線會往真值收斂，
# 所以只要模型「大致正確」，線就會有 68% 的機率往你押的方向走。實測平均 +3.14 分，
# 看起來像天大的 edge，其實只是在贏一條沒人會掛太久的爛線。
#
# 48 小時是折衷：夠近，盤口已經成熟、也是實際會下注的時點；又夠遠，
# 留得住每天兩到三次排程的容錯空間。
ENTRY_MAX_HOURS = 48


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


def _hours_to_tip(game_date: str) -> float | None:
    """距離開賽還有幾小時。算不出來就回 None（呼叫端會直接跳過，不亂猜）。"""
    try:
        d = datetime.fromisoformat(game_date)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - datetime.now(timezone.utc)).total_seconds() / 3600


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

        # ---- 進場線：只在「開賽前 ENTRY_MAX_HOURS 小時內」第一次看到時凍結 ----
        lead = _hours_to_tip(r["game_date"])
        if r["market_captured_at"] is None and lead is not None and 0 < lead <= ENTRY_MAX_HOURS:
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
            model_total_entry = r["pred_total"]
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
                   edge_ml=?, edge_total=?, paper_ml_side=?, paper_total_side=?,
                   entry_p_home_model=?, entry_pred_total=?, entry_lead_hours=?
                   WHERE game_id=?""",
                (_now(), m.get("n_books"),
                 mkt_p_home, m.get("dec_home"), m.get("dec_away"),
                 mkt_line, m.get("dec_over"), m.get("dec_under"), mkt_p_over,
                 edge_ml, edge_total, paper_ml, paper_total,
                 model_p_home, model_total_entry, lead, gid),
            )
    conn.commit()
    conn.close()
    return {"matched": matched, "flagged_ml": flagged_ml, "flagged_total": flagged_total}


# 一分盤口線值多少機率。用常態近似：dp/d線 = φ(0)/σ，WNBA 總分 σ≈19
# → 0.399/19 ≈ 0.021。這只是換算尺度，不影響正負號與相對大小。
TOTAL_PTS_TO_PROB = 0.021


def _total_clv(side: str, entry_line, close_line, entry_p, close_p):
    """
    大小分的 CLV。回傳 (機率版, 分數版)。

    原本這裡是直接比「進場的 p_over」與「收盤的 p_over」，那是錯的：
    去水錢後的 p_over 在任何盤口線上都貼著 50%，所以兩者相減只是雜訊，
    而真正的價值全在「線移動了幾分」。實測資料裡看得很清楚——
    例如押小分 177 收在 170.5（線掉了 6.5 分，對小分方是大賺），
    舊算法卻報 −2.8pp，連正負號都相反。

    所以規則改成：
      線有動 → 以線的移動為準（押大分希望線往上，押小分希望線往下）
      線沒動 → 價值全在價格，就用去水錢後的機率差
    線動了的時候不把價格差加進來，因為不同盤口線上的機率本來就不可比。
    """
    if entry_line is None or close_line is None:
        if entry_p is None or close_p is None:
            return None, None
        p = close_p if side == "over" else (1 - close_p)
        q = entry_p if side == "over" else (1 - entry_p)
        return p - q, None

    move = close_line - entry_line
    pts = move if side == "over" else -move
    if abs(move) < 1e-9:
        if entry_p is None or close_p is None:
            return None, 0.0
        p = close_p if side == "over" else (1 - close_p)
        q = entry_p if side == "over" else (1 - entry_p)
        return p - q, 0.0
    return pts * TOTAL_PTS_TO_PROB, pts


def settle_market(recompute: bool = True) -> int:
    """
    對已結算、有紙上下注邊、但還沒算損益的比賽，算 CLV 與紙上損益。

    recompute=True 時，順便把「已經算過、但用的是舊版大小分 CLV」的列重算一遍
    （認法：clv_total 有值但 clv_total_pts 是 NULL）。這樣舊資料會自動修正，
    不需要另外跑什麼一次性腳本。修完之後這一段自然就不再命中任何列。
    """
    conn = connect()
    rows = conn.execute(
        """SELECT * FROM predictions WHERE status='final'
           AND market_captured_at IS NOT NULL
           AND (paper_ml_result IS NULL AND paper_total_result IS NULL)"""
    ).fetchall()
    if recompute:
        stale = conn.execute(
            """SELECT * FROM predictions WHERE status='final'
               AND paper_total_side IS NOT NULL
               AND clv_total IS NOT NULL AND clv_total_pts IS NULL"""
        ).fetchall()
        for r in stale:
            clv, pts = _total_clv(r["paper_total_side"], r["market_total_line"],
                                  r["closing_total_line"], r["market_p_over"],
                                  r["closing_p_over"])
            conn.execute("UPDATE predictions SET clv_total=?, clv_total_pts=? WHERE game_id=?",
                         (clv, pts, r["game_id"]))
        # 舊資料沒有 entry_lead_hours，從既有欄位回推補上，讓儀表板標得出來
        for r in conn.execute(
                """SELECT game_id, game_date, market_captured_at FROM predictions
                   WHERE market_captured_at IS NOT NULL AND entry_lead_hours IS NULL"""
        ).fetchall():
            try:
                a = datetime.fromisoformat(r["game_date"])
                b = datetime.fromisoformat(r["market_captured_at"])
            except (TypeError, ValueError):
                continue
            if a.tzinfo is None:
                a = a.replace(tzinfo=timezone.utc)
            if b.tzinfo is None:
                b = b.replace(tzinfo=timezone.utc)
            conn.execute("UPDATE predictions SET entry_lead_hours=? WHERE game_id=?",
                         ((a - b).total_seconds() / 3600, r["game_id"]))
        conn.commit()
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
            clv, pts = _total_clv(side, r["market_total_line"], r["closing_total_line"],
                                  r["market_p_over"], r["closing_p_over"])
            if clv is not None:
                upd["clv_total"] = clv
                upd["clv_total_pts"] = pts

        if upd:
            sets = ", ".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE predictions SET {sets} WHERE game_id=?",
                         (*upd.values(), r["game_id"]))
            n += 1
    conn.commit()
    conn.close()
    return n
