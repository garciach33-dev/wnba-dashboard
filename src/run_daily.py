"""
每日主控腳本 —— 一支搞定整條流程。實務上用排程一天跑一到兩次即可。

流程：
  1. 下載最新賽程與比分
  1.5 更新球員層級資料與陣容強度（歷史用事實、未來用傷兵名單）
  2. 重訓模型（資料每天都在變，重訓成本很低）
  3. 回填歷史（僅第一次需要，用來把過去賽事的賽前預測補進 DB）
  4. 對未完賽比賽產生賽前預測快照（已存在的不覆蓋）
  5. 結算已完賽比賽（補上實際結果與誤差）
  6. 產生每日儀表板 HTML

第 1.5 步第一次跑會很慢：要回補七百多場的球員 boxscore。
所以刻意設了單次上限（PLAYER_SYNC_LIMIT），分好幾次排程慢慢補完，
不會一次打爆上游、也不會讓單次排程跑到逾時。補到一半照樣能用——
沒有陣容資料的比賽會吃中性填值，等於退回舊行為，不會壞掉。

排程建議：
  早上跑一次（產生今日/未來預測）＋ 每晚跑一次（結算當天結果）。
  用法： python src/run_daily.py            # 每日更新
        python src/run_daily.py --backfill # 首次執行，含歷史回填
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import players
from build_dashboard import build_dashboard
from db import connect
from fetch import load_games
from features import build_feature_table
from market import settle_market, update_market
from model import WNBAModel, time_series_eval
from predict import backfill_history, generate_predictions, purge_stale_pending
from settle import settle_results

SEASONS = [2023, 2024, 2025, 2026]
PLAYER_SYNC_LIMIT = 300      # 單次排程最多回補幾場球員資料
UPCOMING_INJURY_GAMES = 40   # 只對最近幾場去問即時傷兵（太遠的沒參考價值）


def update_player_data() -> dict:
    """
    回補球員 boxscore → 用「當時之前」的評分重算歷史陣容強度
    → 用「今天的評分＋今天的傷兵名單」算未來比賽的陣容強度。

    任何一段掛掉都不讓整條流程停：陣容強度是加分項，
    拿不到就退回中性填值，網站照樣出得來。
    """
    out = {}
    conn = connect()
    try:
        out["sync"] = players.sync_player_games(
            conn, limit=PLAYER_SYNC_LIMIT, history_seasons=SEASONS)
        out["history"] = players.build_history(conn)
    finally:
        conn.close()
    return out


def strength_table() -> dict:
    conn = connect()
    try:
        return players.strength_map(conn)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="首次執行：回填本季歷史預測")
    ap.add_argument("--no-download", action="store_true", help="用本地已下載的資料，不重新下載")
    args = ap.parse_args()

    print("1) 下載賽程與比分 ...")
    games = load_games(SEASONS, force_download=not args.no_download)
    print(f"   共 {len(games)} 場，完賽 {games['completed'].sum()}、未完賽 {(~games['completed']).sum()}")
    ep = games.attrs.get("espn_patch") or {}
    if ep.get("dates_queried"):
        print(f"   上游 parquet 缺 {ep['dates_queried']} 天的比分 → 問 ESPN 補回 {ep['patched']} 場"
              + ("" if ep["patched"] else "  ⚠ 一場都沒補到，ESPN 那邊可能也有問題"))

    print("1.5) 更新球員資料與歷史陣容強度 ...")
    try:
        info = update_player_data()
        s = info["sync"]
        print(f"   球員資料：新增 {s['added']} 場（累計 {s['total_games']} 場）、"
              f"上游未發布 {s['not_published']}、失敗 {s['failed']}"
              + (f"、已放棄不再重試 {s['skipped']}" if s.get("skipped") else ""))
        print(f"   歷史陣容強度 {info['history']} 場")
    except Exception as e:
        print(f"   ⚠ 失敗（{type(e).__name__}: {e}）—— 陣容特徵吃中性填值，其餘流程照跑")

    # 整條流程共用同一份陣容強度表，避免每次建特徵都重讀一次資料庫
    strength = strength_table()
    print(f"   目前可用陣容強度：{len(strength)} 場")

    print("2) 訓練模型 ...")
    ft = build_feature_table(games, strength)
    metrics = time_series_eval(ft)
    print(f"   勝負命中率 {metrics['acc']:.3f}（naive 主場基準 {metrics['home_baseline_acc']:.3f}）"
          f"、Brier {metrics['brier']:.3f}、總分 MAE {metrics['total_mae']:.2f}"
          f"（基準 {metrics['total_baseline_mae']:.2f}）")
    model = WNBAModel().fit(ft)
    model.save()

    # 每次都做「補撿」：把已完賽、卻還沒進資料庫的本季比賽用 walk-forward 補上歷史。
    # 這關掉了「部署當下已打完的比賽會漏接」的邊界漏洞——不管什麼原因漏掉，
    # 下一次排程就會自動補回來。backfill_history 只寫『尚未存在』的比賽，所以是冪等的、
    # 不會覆蓋任何已經存在的真實賽前快照。
    latest_season = int(games["season"].max())
    print(f"3) 補撿本季({latest_season})已完賽但未記錄的比賽 ...")
    n_bf = backfill_history(games, model, season=latest_season, strength=strength)
    print(f"   補撿 {n_bf} 場")

    print("4) 產生/更新未來賽事預測（每日重算，開賽後不再改）...")
    # refresh=True：每天用最新近況重算所有『尚未開賽』的比賽，讓預測隨時間演化。
    # 已開賽/已結算的比賽不會被覆蓋（SQL 只更新 status='pending' 且 generate 只挑 date>=now），
    # 所以每場比賽在「開賽前最後一次」的預測會自然成為永久紀錄拿去跟實際比對。
    n_pred = generate_predictions(games, model, refresh=True, strength=strength)
    print(f"   更新 {n_pred} 場未來預測")

    # 賽前陣容強度要等 predictions 有了 pending 列才算得出來（它是照那張表跑的），
    # 所以放在第 4 步之後；算完再重跑一次預測，讓今天的傷兵名單當天就生效，
    # 而不是拖到下一次排程。第二次是純計算，不會重訓，成本可以忽略。
    print("4.2) 用今天的傷兵名單算賽前陣容強度 ...")
    try:
        conn = connect()
        try:
            up = players.compute_upcoming(conn, max_games=UPCOMING_INJURY_GAMES)
        finally:
            conn.close()
        print(f"   {up['upcoming']} 場已算，其中 {up['with_injury_data']} 場拿到傷兵名單、"
              f"{up.get('roster_only', 0)} 場只用名冊（太遠，不問傷兵）")
        print(f"   當下名冊（交易修正）：{up.get('roster_ok', 0)}/{up.get('roster_teams', 0)} 隊抓到"
              + ("" if up.get("roster_ok") else "  ⚠ 全部沒抓到 → 名單退回『近 10 場上場過的人』"))
        if up["upcoming"]:
            strength = strength_table()
            n_pred2 = generate_predictions(games, model, refresh=True, strength=strength)
            print(f"   併入傷兵資訊後重算 {n_pred2} 場")
    except Exception as e:
        print(f"   ⚠ 失敗（{type(e).__name__}: {e}）—— 沿用上一步的預測")

    print("4.5) 撈市場盤口、算 edge、記紙上下注 ...")
    mk = update_market(model)
    print(f"   對上 {mk['matched']} 場，自動記注：獨贏 {mk['flagged_ml']}、大小分 {mk['flagged_total']}")

    print("5) 結算已完賽比賽 ...")
    n_settle = settle_results(games)
    n_clv = settle_market()
    pg = purge_stale_pending()
    print(f"   結算 {n_settle} 場、算 CLV/損益 {n_clv} 場，清除殘留延賽 {pg['deleted']} 場")
    if pg["protected"]:
        print(f"   ⚠ 有 {pg['protected']} 場早就開打卻等不到比分，但已抓到盤口 → 保留不刪。"
              f"\n     這通常代表上游賽程資料停更了；等它補上就會自動結算。")

    print("6) 產生儀表板 ...")
    out = build_dashboard()
    print(f"   -> {out}")


if __name__ == "__main__":
    main()
