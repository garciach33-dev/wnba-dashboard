"""
一次性的搶救腳本：把被舊版 purge_stale_pending 誤刪的賽前快照與下注紀錄
從 git 歷史裡撈回來。

背景
────
上游 wehoop 的賽程 parquet 在 2026/8/2 之後停更十天。系統因此看到一堆
「開賽時間過了、卻仍未完賽」的比賽，被舊規則（12 小時就刪）當成延賽刪掉。
盤口與紙上下注跟預測是 predictions 表的同一列，所以一起沒了——
那是 CLV 的原始證據。

但 data/wnba.db 每天被 commit 進 repo 兩次，所以那些列還躺在 git 歷史裡。
這支腳本把歷史上每個版本的 db 取出來，找出「當時有、現在缺」的東西補回去。

三種情況分別處理
────────────────
  1. 現在完全沒有這一列        → 整列補回來
  2. 現在有，但是 backfilled=1 → 那是事後 walk-forward 重建的，
                                 用歷史上真正的賽前快照取代（賽前的才算數）
  3. 現在有真快照但缺盤口欄位  → 只把 market_* / paper_* 補上，預測欄位不動

補回來的列一律寫成 status='pending' 並清掉結算欄位，讓下一次排程重新結算
——這樣 CLV 是由現行程式算出來的，不是我這支腳本硬塞的。

安全性
──────
  * 預設是試跑，只印報告不寫入。確認沒問題再加 --apply。
  * --apply 會先把 data/wnba.db 複製一份到 data/wnba.db.bak-<時間戳>。
  * 只新增與補欄位，永遠不刪除、不覆蓋已經結算好的真實資料。

用法
────
    python recover_bets.py                 # 試跑，看看會撈回什麼
    python recover_bets.py --apply         # 真的寫入
    python recover_bets.py --since 60      # 往回找 60 天的 commit（預設 40）

跑完把報告貼出來即可。確認結果沒問題之後這支腳本就可以刪掉了。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
DB = REPO / "data" / "wnba.db"
DB_IN_REPO = "data/wnba.db"

# 賽前快照欄位：這些是「模型當初說了什麼」，補回來的重點
SNAPSHOT_COLS = [
    "game_date", "season", "home_abbr", "away_abbr", "home_name", "away_name",
    "predicted_at", "p_home_win", "pred_winner", "pred_home_score",
    "pred_away_score", "pred_total", "pred_margin", "confidence",
]
# 盤口與紙上下注欄位：CLV 的原始證據
MARKET_COLS = [
    "market_captured_at", "market_n_books", "market_p_home", "market_dec_home",
    "market_dec_away", "market_total_line", "market_dec_over", "market_dec_under",
    "market_p_over", "closing_p_home", "closing_p_over", "closing_total_line",
    "edge_ml", "edge_total", "entry_p_home_model", "entry_pred_total",
    "paper_ml_side", "paper_total_side",
]


def sh(*args: str) -> str:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                          check=True).stdout


def commits_touching_db(days: int) -> list[tuple[str, str]]:
    """回傳 [(sha, 日期)]，由新到舊。"""
    out = sh("git", "log", f"--since={days}.days", "--format=%H\t%cI",
             "--", DB_IN_REPO)
    rows = []
    for line in out.strip().splitlines():
        if "\t" in line:
            sha, when = line.split("\t", 1)
            rows.append((sha, when))
    return rows


def rows_from_commit(sha: str) -> dict[str, dict]:
    """把某個 commit 的 wnba.db 取出來，讀出所有『真賽前快照』的列。"""
    blob = subprocess.run(["git", "show", f"{sha}:{DB_IN_REPO}"],
                          cwd=REPO, capture_output=True, check=True).stdout
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(blob)
        tmp = f.name
    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        have = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
        # 舊版 db 可能還沒有某些欄位，缺的就當 None
        cols = ["game_id", "backfilled"] + \
               [c for c in SNAPSHOT_COLS + MARKET_COLS if c in have]
        out = {}
        for r in conn.execute(
                f"SELECT {','.join(cols)} FROM predictions WHERE backfilled=0"):
            d = dict(r)
            out[str(d["game_id"])] = d
        conn.close()
        return out
    except sqlite3.DatabaseError:
        return {}                    # 那個 commit 的 db 壞了/還沒建好，跳過
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的寫入（預設只試跑）")
    ap.add_argument("--since", type=int, default=40, help="往回找幾天的 commit")
    args = ap.parse_args()

    if not DB.exists():
        print(f"找不到 {DB}", file=sys.stderr)
        return 1

    commits = commits_touching_db(args.since)
    print(f"近 {args.since} 天有 {len(commits)} 個 commit 動過 {DB_IN_REPO}")
    if not commits:
        print("沒有歷史可撈——這支腳本要在 repo 目錄裡跑，而且要有完整的 git 歷史"
              "（GitHub Actions 的 checkout 預設只抓 1 層，本機 clone 才有全部）。")
        return 1

    # 由舊到新掃過去，同一場以「最新的版本」為準（收盤線每天會刷新）
    history: dict[str, dict] = {}
    for sha, when in reversed(commits):
        try:
            got = rows_from_commit(sha)
        except subprocess.CalledProcessError:
            continue
        for gid, row in got.items():
            old = history.get(gid)
            # 有盤口的優先；同樣有的話取比較晚的版本
            if old is None or (row.get("market_captured_at") and
                               not old.get("market_captured_at")):
                history[gid] = row
            elif row.get("market_captured_at") and old.get("market_captured_at"):
                history[gid] = row
    print(f"歷史上總共出現過 {len(history)} 場『真賽前快照』")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur_cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    current = {str(r["game_id"]): dict(r)
               for r in conn.execute("SELECT * FROM predictions")}

    missing, replace_backfill, fill_market = [], [], []
    for gid, row in history.items():
        cur = current.get(gid)
        if cur is None:
            missing.append(gid)
        elif cur.get("backfilled") == 1:
            replace_backfill.append(gid)
        elif not cur.get("market_captured_at") and row.get("market_captured_at"):
            fill_market.append(gid)

    def show(title, ids):
        print(f"\n{title}：{len(ids)} 場")
        for gid in sorted(ids, key=lambda g: history[g].get("game_date") or "")[:40]:
            h = history[gid]
            bet = h.get("paper_ml_side") or h.get("paper_total_side") or "-"
            mk = "有盤口" if h.get("market_captured_at") else "無盤口"
            print(f"   {(h.get('game_date') or '')[:10]}  {gid}  "
                  f"{h.get('away_abbr')}@{h.get('home_abbr')}  {mk}  下注邊={bet}")
        if len(ids) > 40:
            print(f"   ...（其餘 {len(ids)-40} 場略）")

    show("① 現在完全缺這一列，會整列補回", missing)
    show("② 現在是事後重建(backfilled=1)，會用真快照取代", replace_backfill)
    show("③ 有真快照但缺盤口，只補盤口欄位", fill_market)

    n_bets = sum(1 for g in missing + replace_backfill + fill_market
                 if history[g].get("market_captured_at"))
    print(f"\n其中帶有盤口資料（＝能算出 CLV）的共 {n_bets} 場")

    if not args.apply:
        print("\n這是試跑，沒有寫入任何東西。確認上面沒問題後加 --apply 再跑一次。")
        conn.close()
        return 0

    bak = DB.with_suffix(f".db.bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
    shutil.copy(DB, bak)
    print(f"\n已備份到 {bak.name}")

    def payload(row, cols):
        return {c: row.get(c) for c in cols if c in cur_cols and c in row}

    n1 = n2 = n3 = 0
    for gid in missing + replace_backfill:
        h = history[gid]
        data = payload(h, SNAPSHOT_COLS + MARKET_COLS)
        # 一律回到 pending 並清空結算欄位，讓下一次排程用現行程式重新結算、重算 CLV
        data.update({"game_id": gid, "status": "pending", "backfilled": 0,
                     "actual_home_score": None, "actual_away_score": None,
                     "actual_winner": None, "actual_total": None, "settled_at": None,
                     "winner_correct": None, "total_abs_error": None,
                     "margin_abs_error": None, "paper_ml_result": None,
                     "clv_ml": None})
        if "paper_total_result" in cur_cols:
            data["paper_total_result"] = None
        if "clv_total" in cur_cols:
            data["clv_total"] = None
        keys = list(data)
        conn.execute(
            f"INSERT OR REPLACE INTO predictions ({','.join(keys)}) "
            f"VALUES ({','.join('?' * len(keys))})", [data[k] for k in keys])
        if gid in missing:
            n1 += 1
        else:
            n2 += 1

    for gid in fill_market:
        h = history[gid]
        data = payload(h, MARKET_COLS)
        if not data:
            continue
        sets = ",".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE predictions SET {sets} WHERE game_id=?",
                     list(data.values()) + [gid])
        n3 += 1

    conn.commit()
    conn.close()
    print(f"寫入完成：整列補回 {n1}、取代重建 {n2}、補盤口欄位 {n3}")
    print("接下來跑一次 python src/run_daily.py，第 5 步會把這些重新結算並算出 CLV。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
