"""
每日主控腳本 —— 一支搞定整條流程。實務上用排程一天跑一到兩次即可。

流程：
  1. 下載最新賽程與比分
  2. 重訓模型（資料每天都在變，重訓成本很低）
  3. 回填歷史（僅第一次需要，用來把過去賽事的賽前預測補進 DB）
  4. 對未完賽比賽產生賽前預測快照（已存在的不覆蓋）
  5. 結算已完賽比賽（補上實際結果與誤差）
  6. 產生每日儀表板 HTML

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

from build_dashboard import build_dashboard
from fetch import load_games
from features import build_feature_table
from model import WNBAModel, time_series_eval
from predict import backfill_history, generate_predictions, purge_stale_pending
from settle import settle_results

SEASONS = [2023, 2024, 2025, 2026]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="首次執行：回填本季歷史預測")
    ap.add_argument("--no-download", action="store_true", help="用本地已下載的資料，不重新下載")
    args = ap.parse_args()

    print("1) 下載賽程與比分 ...")
    games = load_games(SEASONS, force_download=not args.no_download)
    print(f"   共 {len(games)} 場，完賽 {games['completed'].sum()}、未完賽 {(~games['completed']).sum()}")

    print("2) 訓練模型 ...")
    ft = build_feature_table(games)
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
    n_bf = backfill_history(games, model, season=latest_season)
    print(f"   補撿 {n_bf} 場")

    print("4) 產生/更新未來賽事預測（每日重算，開賽後不再改）...")
    # refresh=True：每天用最新近況重算所有『尚未開賽』的比賽，讓預測隨時間演化。
    # 已開賽/已結算的比賽不會被覆蓋（SQL 只更新 status='pending' 且 generate 只挑 date>=now），
    # 所以每場比賽在「開賽前最後一次」的預測會自然成為永久紀錄拿去跟實際比對。
    n_pred = generate_predictions(games, model, refresh=True)
    print(f"   更新 {n_pred} 場未來預測")

    print("5) 結算已完賽比賽 ...")
    n_settle = settle_results(games)
    n_purge = purge_stale_pending()
    print(f"   結算 {n_settle} 場，清除殘留延賽 {n_purge} 場")

    print("6) 產生儀表板 ...")
    out = build_dashboard()
    print(f"   -> {out}")


if __name__ == "__main__":
    main()
