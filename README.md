# WNBA 每日賽事預測儀表板（原型）

一個可實際運作的骨架：抓真實 WNBA 資料 → baseline 模型預測「勝負 + 總分」→
把賽前預測存成快照到 SQLite → 賽後結算 → 產生單頁每日儀表板。
設計重點是**誠實的歷史回顧**：預測在賽前就寫入、之後不改，你看到的永遠是模型當初真正的說法。

## 快速開始

```bash
pip install -r requirements.txt

# 首次執行（含歷史回填，讓歷史回顧一開始就有資料）
python src/run_daily.py --backfill

# 之後每日更新（抓新資料、預測未來、結算已完賽）
python src/run_daily.py

# 用瀏覽器打開 dashboard.html 即可
```

## 資料來源（免費）

[sportsdataverse / wehoop](https://github.com/sportsdataverse/wehoop-wnba-raw) 的
GitHub 資料倉庫，底層來自 ESPN，含每場賽程、最終比分、勝負、完賽狀態。
每季一個 parquet，乾淨、有歷史、免金鑰。

> 想改用「即時」來源（例如 ESPN scoreboard API 當天賽況），只要改 `src/fetch.py`，
> 讓它回傳同樣欄位的 DataFrame，其他模組都不用動。

## 專案結構

```
src/
  fetch.py            下載並正規化賽程/比分
  features.py         近況滾動特徵（嚴格避免資料洩漏）
  model.py            勝負分類 + 總分/分差回歸 + 時間序評估 + walk-forward
  db.py               SQLite schema（predictions 表 = 賽前快照 + 賽後結算）
  predict.py          產生未來賽事的賽前預測快照（不覆蓋既有）
  settle.py           賽後補上實際結果與誤差
  build_dashboard.py  從 DB 產生自足式儀表板 HTML
  run_daily.py        一支跑完整條流程（排程用）
data/
  raw/*.parquet       下載的原始賽程
  wnba.db             SQLite 資料庫
  model.pkl           訓練好的模型
dashboard.html        產出的儀表板
```

## 模型與評估

Baseline 用近況滾動特徵（近 8 場場均得分/失分、勝率、休息天數、已賽場數）配線性模型：
勝負用 LogisticRegression（輸出主隊勝率）、總分與分差用 Ridge 回歸，
再把總分與分差組回雙方預測比分。

以**時間序**評估（前段訓練、後段測試，模擬真實上線）目前約：

| 指標 | 模型 | 對照基準 |
|---|---|---|
| 勝負命中率 | ~0.64 | 永遠猜主隊 ~0.55 |
| 總分 MAE | ~16 分 | 猜平均 ~17 分 |
| Brier（機率校準） | ~0.23 | 亂猜 0.25 |

歷史回顧區的預測是用**逐日 walk-forward 樣本外**方式回填的（預測某天只用該日之前的資料重訓），
所以回顧數字不會灌水，跟上面的樣本外評估一致。

## 預測會隨時間演化（重要行為）

- **未來比賽**：每天用最新近況重新預測，數字會隨球隊狀態變動（連勝往上調、掉分往下修）。
  一旦比賽開賽，該場「開賽前最後一次」的預測就自動鎖定成永久紀錄。
- **已結算比賽**：預測永遠凍結、不再更動，只補上實際結果——這樣歷史回顧才誠實。

技術上：`generate_predictions(refresh=True)` 只重算 `game_date >= 現在` 的比賽，
SQL 更新只作用在 `status='pending'` 的列，所以已開賽/已結算的快照不會被覆蓋。

## 線上自動更新（GitHub Actions + Pages，免費）

已內建 `.github/workflows/daily.yml`，一天自動跑兩次、更新預測並把儀表板發佈到 GitHub Pages。
`data/wnba.db` 會在每次執行後 commit 回 repo，保存歷史快照。

**設定步驟（一次性）：**

1. 把整個資料夾推到一個 GitHub repo（`data/wnba.db` 要一起入庫，裡面已含歷史回填）。
2. repo 的 **Settings → Pages → Build and deployment → Source** 選 **GitHub Actions**。
3. repo 的 **Settings → Actions → General → Workflow permissions** 選 **Read and write permissions**（讓排程能把更新後的 DB 推回）。
4. 到 **Actions** 分頁手動觸發一次 `WNBA 每日預測與部署`（`Run workflow`）確認成功；
   之後就會照排程自動更新，網址是 `https://<你的帳號>.github.io/<repo 名>/`。

排程時間在 workflow 檔裡（UTC）；預設 07:00 / 19:00 台北，可自行調整 cron。

> 首次若想重跑歷史回填：本機執行 `python src/run_daily.py --backfill` 後把 `data/wnba.db` commit 上去即可。
> 純本機排程也行：用 cron 跑 `python src/run_daily.py`（UTC 時間）。

## 下一步可以加的東西

球員層級數據與傷兵名單（目前最缺、最能提升準度的一塊）、主客場分開的攻防評分、
背靠背/旅行距離、對戰歷史，以及把線性模型換成 XGBoost。
介面契約（fetch → features → model）都保持不變，逐項升級不會動到其他部分。

---
此為技術展示原型，非投注建議。
