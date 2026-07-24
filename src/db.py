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


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn


def existing_game_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT game_id FROM predictions")}
