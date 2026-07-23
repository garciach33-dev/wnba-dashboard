"""
Baseline 模型：勝負 + 總分（+ 分差，用來湊出預測比分）。

刻意選穩健、不易過擬合的線性模型（樣本量只有數百場）：
  - 勝負：LogisticRegression → 輸出主隊獲勝機率
  - 總分：Ridge 回歸
  - 分差：Ridge 回歸（home - away），用來把總分拆成雙方預測分數

用「時間序」評估：拿賽季後段當測試集，模擬真實上線時「只能用過去預測未來」。
評估指標：勝負命中率、Brier score（機率校準）、總分 MAE。

之後要升級（加傷兵、球員數據、換 XGBoost），只要換這支檔案的模型即可，
輸入輸出介面（fit / predict_games）保持不變，其他模組不受影響。
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, brier_score_loss, mean_absolute_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from features import FEATURE_COLS, build_feature_table

MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "model.pkl"


class WNBAModel:
    def __init__(self):
        self.clf = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=0.5))
        self.reg_total = make_pipeline(StandardScaler(), Ridge(alpha=5.0))
        self.reg_margin = make_pipeline(StandardScaler(), Ridge(alpha=5.0))
        self.baseline_total = 160.0  # 無資料時的退路

    def fit(self, feat_df: pd.DataFrame):
        d = feat_df[feat_df["completed"]].dropna(subset=FEATURE_COLS + ["winner", "total", "margin"])
        X = d[FEATURE_COLS].values
        y_win = (d["winner"] == "home").astype(int).values
        self.clf.fit(X, y_win)
        self.reg_total.fit(X, d["total"].values)
        self.reg_margin.fit(X, d["margin"].values)
        self.baseline_total = float(d["total"].mean())
        return self

    def predict_games(self, feat_df: pd.DataFrame) -> pd.DataFrame:
        d = feat_df.copy()
        X = d[FEATURE_COLS].values
        p_home = self.clf.predict_proba(X)[:, 1]
        total = self.reg_total.predict(X)
        margin = self.reg_margin.predict(X)
        out = pd.DataFrame({
            "game_id": d["game_id"].values,
            "p_home_win": p_home,
            "pred_total": total,
            "pred_margin": margin,
        })
        out["pred_winner"] = np.where(out["p_home_win"] >= 0.5, "home", "away")
        out["pred_home_score"] = np.rint((total + margin) / 2).astype(int)
        out["pred_away_score"] = np.rint((total - margin) / 2).astype(int)
        # 信心度 = 距離 50% 多遠，映射到 0~100
        out["confidence"] = (np.abs(out["p_home_win"] - 0.5) * 200).round(0).astype(int)
        return out

    def save(self, path: Path = MODEL_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path: Path = MODEL_PATH) -> "WNBAModel":
        with open(path, "rb") as f:
            return pickle.load(f)


def walk_forward_predictions(feat_df: pd.DataFrame, season: int) -> pd.DataFrame:
    """
    對指定賽季的已完賽比賽做逐日 walk-forward 樣本外預測：
    預測某一天的比賽時，只用「該日之前」的完賽資料重新訓練模型。
    這是回顧模型真實表現的誠實做法（不會考自己出過的題）。
    回傳 predict_games 格式的 DataFrame。
    """
    done = feat_df[feat_df["completed"]].dropna(subset=FEATURE_COLS + ["winner", "total", "margin"]).copy()
    done = done.sort_values("date")
    target = done[done["season"] == season]
    out_frames = []
    for d in sorted(target["date"].dt.normalize().unique()):
        train = done[done["date"] < d]
        if len(train) < 50:      # 訓練資料太少就跳過（賽季極早期）
            continue
        day_games = target[target["date"].dt.normalize() == d]
        m = WNBAModel().fit(train)
        out_frames.append(m.predict_games(day_games))
    if not out_frames:
        return pd.DataFrame()
    return pd.concat(out_frames, ignore_index=True)


def time_series_eval(feat_df: pd.DataFrame, test_frac: float = 0.25) -> dict:
    """依時間切分：前段訓練、後段測試，回傳指標字典。"""
    d = feat_df[feat_df["completed"]].dropna(subset=FEATURE_COLS + ["winner", "total"])
    d = d.sort_values("date")
    n = len(d)
    cut = int(n * (1 - test_frac))
    train, test = d.iloc[:cut], d.iloc[cut:]

    m = WNBAModel().fit(train)
    pred = m.predict_games(test)
    pred = pred.set_index("game_id")
    test = test.set_index("game_id")

    y_true_win = (test["winner"] == "home").astype(int)
    p = pred.loc[test.index, "p_home_win"]
    win_pred = (p >= 0.5).astype(int)

    # 對照基準：永遠猜主隊贏（WNBA 主場優勢的 naive baseline）
    home_rate = y_true_win.mean()

    return {
        "n_train": len(train),
        "n_test": len(test),
        "acc": accuracy_score(y_true_win, win_pred),
        "home_baseline_acc": max(home_rate, 1 - home_rate),
        "brier": brier_score_loss(y_true_win, p),
        "total_mae": mean_absolute_error(test["total"], pred.loc[test.index, "pred_total"]),
        "total_baseline_mae": mean_absolute_error(
            test["total"], np.full(len(test), train["total"].mean())
        ),
    }


if __name__ == "__main__":
    from fetch import load_games
    g = load_games([2023, 2024, 2025, 2026])
    ft = build_feature_table(g)
    metrics = time_series_eval(ft)
    print("=== 時間序評估（後 25% 當測試集）===")
    for k, v in metrics.items():
        print(f"  {k:22s}: {v:.4f}" if isinstance(v, float) else f"  {k:22s}: {v}")
    # 用全部完賽資料重訓並存檔
    WNBAModel().fit(ft).save()
    print(f"model saved -> {MODEL_PATH}")
