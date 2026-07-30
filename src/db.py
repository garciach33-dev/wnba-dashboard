"""
SQLite 儲存層。單檔資料庫，零維護；資料長大再換 PostgreSQL 也只要改這支。

predictions 表是整個系統的核心：預測「賽前」寫入快照，
一旦寫入就不再更動預測欄位（p_home_win / pred_total / ...），
賽後只補上實際結果與誤差。這樣歷史回顧才誠實 —— 你看到的永遠是
模型當初「開賽前」真正說了什麼，而不是事後諸葛。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "wnba.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    game_id           TEXT PRIMARY KEY,
    game_date         TEXT NOT NULL,       -- UTC ISO
    season            INTEGER,
    home_abbr         TEXT,
    away_abbr         TEXT,
    home_name         TEXT,
    away_name         TEXT,

    -- 賽前快照（寫入後不再變動）
    predicted_at      TEXT NOT NULL,       -- 產生預測的時間 (UTC ISO)
    p_home_win        REAL,
    pred_winner       TEXT,                -- 'home' / 'away'
    pred_home_score   INTEGER,
    pred_away_score   INTEGER,
    pred_total        REAL,
    pred_margin       REAL,
    confidence        INTEGER,

    -- 賽後結算
    status            TEXT NOT NULL,       -- 'pending' / 'final'
    actual_home_score INTEGER,
    actual_away_score INTEGER,
    actual_winner     TEXT,
    actual_total      INTEGER,
    settled_at        TEXT,

    -- 誤差指標
    winner_correct    INTEGER,             -- 1/0
    total_abs_error   REAL,
    margin_abs_error  REAL,

    -- 1 = 回填的歷史（賽後才產生，但用的是賽前無洩漏特徵）；0 = 真正賽前產生的快照
    backfilled        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(game_date);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);
"""

# 盤口 / edge / CLV / 紙上下注欄位（後加）。用 ALTER TABLE 逐欄補上，
# 讓既有資料庫能就地升級、不必砍掉重建。
MARKET_COLUMNS = {
    # 進場盤口（第一次看到就凍結，代表「我們下注時拿到的線」）
    "market_captured_at": "TEXT",
    "market_n_books": "INTEGER",
    "market_p_home": "REAL",      # 去水錢後市場主隊勝率
    "market_dec_home": "REAL",    # 主隊十進位賠率
    "market_dec_away": "REAL",
    "market_total_line": "REAL",  # 大小分盤口線
    "market_dec_over": "REAL",
    "market_dec_under": "REAL",
    "market_p_over": "REAL",      # 去水錢後市場「過盤」機率
    # 收盤線（每次排程對未開賽比賽刷新，開賽即凍結）→ 用來算 CLV
    "closing_p_home": "REAL",
    "closing_p_over": "REAL",
    "closing_total_line": "REAL",
    # 模型 vs 市場的 edge（進場當下）
    "edge_ml": "REAL",
    "edge_total": "REAL",
    # 進場當下模型的主隊勝率／預測總分（凍結）。
    # 未來賽事的 p_home_win 每天都會更新，若拿「今天的模型」去配「當初凍結的盤口」，
    # 表格會出現「下注邊的模型機率反而低於市場」這種自相矛盾的畫面。
    "entry_p_home_model": "REAL",
    "entry_pred_total": "REAL",
    # 自動紙上下注（edge 超門檻才記）與結算結果
    "paper_ml_side": "TEXT",      # 'home'/'away'，模型看好且有 edge 的一邊
    "paper_ml_result": "REAL",    # 單位淨利（+賠率-1 / -1）
    "clv_ml": "REAL",             # 收盤機率 - 進場機率（同側），>0 = 贏過收盤線
    "paper_total_side": "TEXT",   # 'over'/'under'
    "paper_total_result": "REAL",
    "clv_total": "REAL",
}


def _ensure_columns(conn: sqlite3.Connection):
    have = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    for col, typ in MARKET_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
    conn.commit()
    _backfill_entry_model(conn)


def _backfill_entry_model(conn: sqlite3.Connection):
    """
    舊資料沒有 entry_p_home_model，但可以精確還原：
      edge_ml 是「進場當下、下注邊」的模型機率減市場機率，
      所以 side=home → entry = market_p_home + edge_ml
          side=away → entry = market_p_home − edge_ml
    同理，大小分的進場模型總分可由 edge_total 反推回去（常態分佈反函數）。
    只補得回來的列（有記紙上下注、方向明確者），跑幾次都一樣（冪等）。
    """
    conn.execute(
        """UPDATE predictions SET entry_p_home_model =
               CASE WHEN paper_ml_side='away' THEN market_p_home - edge_ml
                    ELSE market_p_home + edge_ml END
           WHERE entry_p_home_model IS NULL
             AND paper_ml_side IS NOT NULL
             AND edge_ml IS NOT NULL AND market_p_home IS NOT NULL"""
    )

    from statistics import NormalDist          # 標準庫，免額外相依
    TOTAL_SIGMA = 15.0                          # 與 model.WNBAModel.total_sigma 一致
    nd = NormalDist()
    rows = conn.execute(
        """SELECT game_id, paper_total_side, market_p_over, edge_total, market_total_line
           FROM predictions
           WHERE entry_pred_total IS NULL AND paper_total_side IS NOT NULL
             AND edge_total IS NOT NULL AND market_p_over IS NOT NULL
             AND market_total_line IS NOT NULL"""
    ).fetchall()
    for r in rows:
        p = (r["market_p_over"] + r["edge_total"]) if r["paper_total_side"] == "over" \
            else (r["market_p_over"] - r["edge_total"])
        p = min(max(p, 1e-6), 1 - 1e-6)
        conn.execute(
            "UPDATE predictions SET entry_pred_total=? WHERE game_id=?",
            (r["market_total_line"] + TOTAL_SIGMA * nd.inv_cdf(p), r["game_id"]),
        )
    conn.commit()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn


def existing_game_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT game_id FROM predictions")}
