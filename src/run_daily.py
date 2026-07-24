"""
每日主控腳本 —— 一支搞定整條流程。實務上用排程一天跑一到兩次即可。

流程：
  1. 下載最新賽程與比分
  2. 重訓模型
  3. 補撿：把已完賽卻還沒記錄的本季比賽補進歷史（關掉邊界漏接漏洞）
  4. 對未來比賽產生/更新賽前預測快照
  5. 結算已完賽比賽
  6. 產生每日儀表板 HTML
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
    ap.add_argument("--backfill", action="store_true", help="（保留參數，現在每次都會自動補撿）")
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
    # 這關掉了「部署當下已打完的比賽會漏接」的邊界漏洞。backfill_history 只寫『尚未存在』
    # 的比賽，所以是冪等的、不會覆蓋任何已存在的真實賽前快照。
    latest_season = int(games["season"].max())
    print(f"3) 補撿本季({latest_season})已完賽但未記錄的比賽 ...")
    n_bf = backfill_history(games, model, season=latest_season)
    print(f"   補撿 {n_bf} 場")

    print("4) 產生/更新未來賽事預測（每日重算，開賽後不再改）...")
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
