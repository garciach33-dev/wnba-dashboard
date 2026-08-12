"""
從 SQLite 產生自足式單頁儀表板 (dashboard.html)。

兩大區塊：
  · 今日 / 未來賽事：模型的賽前預測（勝方、勝率、預測比分、信心度）
  · 歷史回顧：預測 vs 實際並排，附滾動命中率趨勢圖與整體準確率指標

整頁自足（CSS/JS 內嵌，無外部相依），可直接用瀏覽器開，
也可掛到任何靜態空間。資料在建置時從 DB 讀出、內嵌成 JSON。
配色採用經 CVD 驗證的色盤，支援深/淺色模式。
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import connect

OUT_PATH = Path(__file__).resolve().parent.parent / "dashboard.html"

# 進場線超過開賽前這麼多小時記的，視為「太早」，CLV 不可信。
# 要跟 market.ENTRY_MAX_HOURS 一致。
EARLY_ENTRY_HOURS = 48
MYBETS_CSV = Path(__file__).resolve().parent.parent / "data" / "my_bets.csv"
TPE = timezone(timedelta(hours=8))   # 台北固定 UTC+8（無日光節約）


def _taipei_date(iso: str) -> str:
    s = (iso or "").strip()
    # 純日期就照原樣回傳。丟進 fromisoformat 會變成「本機時區的午夜」再換算，
    # 容器時區一改就會差一天。
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(TPE).strftime("%Y-%m-%d")
    except Exception:
        return s[:10]


def _dec_odds(o: float) -> float:
    """十進位賠率一定 > 1。台灣習慣寫淨賠率（0.75 = 押 1 賺 0.75），補回 1。"""
    return o if o > 1 else o + 1


def load_mybets(conn) -> tuple[list, dict | None]:
    """
    讀 data/my_bets.csv（跨裝置：存在 repo，任何裝置打開網站都看得到），
    對上 predictions 的比賽，算出你的損益、模型看法、以及「誰對」。
    CSV 欄位：date(YYYY-MM-DD,台北),away,home,market(ML/OU),side,stake,odds,line
    """
    if not MYBETS_CSV.exists():
        return [], None

    # 建立 (away,home,台北日期) -> 比賽 的查表
    rows = conn.execute("SELECT * FROM predictions").fetchall()
    index = {}
    for r in rows:
        key = (r["away_abbr"].upper(), r["home_abbr"].upper(), _taipei_date(r["game_date"]))
        index[key] = dict(r)

    out = []
    with open(MYBETS_CSV, newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            try:
                # 跟前端同一套規則：完整時間戳要先換成台北日期，不能直接截前 10 碼
                date = _taipei_date((raw.get("date") or "").strip())
                away = (raw.get("away") or "").strip().upper()
                home = (raw.get("home") or "").strip().upper()
                market = (raw.get("market") or "").strip().upper()
                side = (raw.get("side") or "").strip()
                stake = float(raw.get("stake"))
                odds = _dec_odds(float(raw.get("odds")))
                line = raw.get("line")
                line = float(line) if (line not in (None, "",) ) else None
            except (TypeError, ValueError):
                continue
            market = "OU" if market in ("OU", "TOTAL", "TOTALS", "大小分", "TOT") else "ML"

            g = index.get((away, home, date))
            base = {"date": date[5:], "matchup": f"{away}@{home}",
                    "stake": stake, "odds": odds, "pnl": None, "agree": None,
                    "who": None, "model_side": "—", "status": "無法對應"}
            if not g:
                base["target_label"] = side
                out.append(base); continue

            completed = g["status"] == "final" and g["actual_winner"] is not None
            if market == "ML":
                su = side.upper()
                my = "home" if su in (home, "HOME", "H") else ("away" if su in (away, "AWAY", "A") else None)
                if my is None:
                    base["target_label"] = side; out.append(base); continue
                my_abbr = home if my == "home" else away
                mSide = "home" if (g["p_home_win"] is not None and g["p_home_win"] >= 0.5) else "away"
                base["target_label"] = f"{my_abbr} 獨贏"
                base["model_side"] = (home if mSide == "home" else away) + " 贏"
                base["agree"] = (my == mSide)
                if completed:
                    yw = (my == g["actual_winner"]); mw = (mSide == g["actual_winner"])
                    base["status"] = "贏" if yw else "輸"
                    base["pnl"] = stake * (odds - 1) if yw else -stake
                    base["who"] = "both" if (yw and mw) else "neither" if (not yw and not mw) else ("you" if yw else "model")
                    base["_yw"], base["_mw"] = yw, mw
                else:
                    base["status"] = "待開賽"
            else:  # OU
                su = side.lower()
                my = "over" if su in ("over", "o", "大", "大分") else ("under" if su in ("under", "u", "小", "小分") else None)
                if my is None or line is None:
                    base["target_label"] = (side or "大小分") + (f" {line}" if line is not None else "")
                    base["status"] = "缺盤口線" if line is None else "無法對應"
                    out.append(base); continue
                mSide = "over" if (g["pred_total"] is not None and g["pred_total"] > line) else "under"
                base["target_label"] = ("大分 O" if my == "over" else "小分 U") + f" {line:g}"
                base["model_side"] = "大分" if mSide == "over" else "小分"
                base["agree"] = (my == mSide)
                at = g["actual_total"]
                if completed and at is not None:
                    if at == line:
                        base["status"] = "退注"; base["pnl"] = 0.0
                    else:
                        ow = at > line
                        yw = (my == "over" and ow) or (my == "under" and not ow)
                        mw = (mSide == "over" and ow) or (mSide == "under" and not ow)
                        base["status"] = "贏" if yw else "輸"
                        base["pnl"] = stake * (odds - 1) if yw else -stake
                        base["who"] = "both" if (yw and mw) else "neither" if (not yw and not mw) else ("you" if yw else "model")
                        base["_yw"], base["_mw"] = yw, mw
                else:
                    base["status"] = "待開賽"
            out.append(base)

    # 統計（只算有勝負的注：排除待開賽與退注）
    decided = [b for b in out if "_yw" in b]
    stats = None
    if out:
        n = len(decided)
        staked = sum(b["stake"] for b in decided)
        pnl = sum(b["pnl"] for b in decided if b["pnl"] is not None)
        stats = {
            "n": len(out), "n_settled": n,
            "pnl": pnl, "staked": staked,
            "roi": (pnl / staked) if staked else 0.0,
            "you_hits": sum(1 for b in decided if b.get("_yw")),
            "model_hits": sum(1 for b in decided if b.get("_mw")),
        }
    for b in out:
        b.pop("_yw", None); b.pop("_mw", None)
    # 依日期新到舊
    out.sort(key=lambda b: b["date"], reverse=True)
    return out, stats


def _rows(conn, where, order):
    cur = conn.execute(f"SELECT * FROM predictions WHERE {where} ORDER BY {order}")
    return [dict(r) for r in cur.fetchall()]


def gather_data() -> dict:
    conn = connect()
    now = datetime.now(timezone.utc)

    pending = _rows(conn, "status='pending'", "game_date ASC")
    settled = _rows(conn, "status='final'", "game_date DESC")
    conn.close()

    # 整體指標
    n = len(settled)
    if n:
        wins = sum(r["winner_correct"] for r in settled)
        acc = wins / n
        total_mae = sum(r["total_abs_error"] for r in settled) / n
        margin_mae = sum(r["margin_abs_error"] for r in settled) / n
        # Brier：機率校準（0 最好，越低越準）
        brier = sum(
            (r["p_home_win"] - (1 if r["actual_winner"] == "home" else 0)) ** 2
            for r in settled
        ) / n
    else:
        acc = total_mae = margin_mae = brier = None

    # 滾動命中率趨勢（依日期正序，最近 40 場的 10 場滾動命中率）
    chron = sorted(settled, key=lambda r: r["game_date"])
    window = 10
    trend = []
    for i in range(len(chron)):
        lo = max(0, i - window + 1)
        seg = chron[lo:i + 1]
        hr = sum(s["winner_correct"] for s in seg) / len(seg)
        trend.append({"date": chron[i]["game_date"][:10], "acc": round(hr, 3)})
    trend = trend[-40:]

    paper = _paper_stats(settled)
    has_market = any(r.get("market_captured_at") for r in pending) or paper["ml"]["n"] or paper["total"]["n"]

    # 今日/未來的「最大 edge 候選」（有被自動記注的場次，依 edge 由大到小）
    cands = []
    for r in pending:
        best = max(r.get("edge_ml") or 0, r.get("edge_total") or 0)
        if r.get("paper_ml_side") or r.get("paper_total_side"):
            cands.append({**r, "_best_edge": best})
    cands.sort(key=lambda r: r["_best_edge"], reverse=True)

    # 已結算的紙上下注紀錄
    paper_bets = _paper_bet_rows(settled)

    return {
        "generated_at": now.isoformat(),
        "metrics": {
            "acc": acc, "total_mae": total_mae, "margin_mae": margin_mae,
            "brier": brier, "n_settled": n, "n_pending": len(pending),
        },
        "has_market": bool(has_market),
        "paper": paper,
        "bets_endpoint": os.environ.get("BETS_ENDPOINT", "").strip(),
        "edge_candidates": cands,
        "paper_bets": paper_bets,
        "pending": pending,
        "settled": settled,
        "trend": trend,
    }


def _agg(bets, result_key, clv_key):
    n = len(bets)
    if not n:
        return {"n": 0, "hit_rate": None, "roi": None, "units": 0.0,
                "avg_clv": None, "pct_pos_clv": None}
    units = sum(b[result_key] for b in bets)
    wins = sum(1 for b in bets if b[result_key] > 0)
    clvs = [b[clv_key] for b in bets if b[clv_key] is not None]
    return {
        "n": n,
        "hit_rate": wins / n,
        "roi": units / n,                       # 每注平均淨利（單位）
        "units": units,
        "avg_clv": (sum(clvs) / len(clvs)) if clvs else None,
        "pct_pos_clv": (sum(1 for c in clvs if c > 0) / len(clvs)) if clvs else None,
    }


def _paper_stats(settled) -> dict:
    ml = [r for r in settled if r.get("paper_ml_side") and r.get("paper_ml_result") is not None]
    tot = [r for r in settled if r.get("paper_total_side") and r.get("paper_total_result") is not None]
    # 太早記的注：進場線是開賽好幾天前的報價，不是真的下得到的價，
    # 拿它算出來的 CLV 會系統性偏樂觀（早期線本來就偏離真值，收盤會往真值收斂）。
    # 這裡把數量算出來，讓儀表板明講，而不是讓那些數字混在一起看起來像 edge。
    all_bets = {id(r): r for r in ml + tot}.values()
    # lead <= 0 是「進場線比開賽時間還晚」——上游停更期間比賽一直掛在 pending，
    # 盤口就對到了同兩隊兩天內的下一場，等於記到別場的價。那比太早更糟。
    early = sum(1 for r in all_bets
                if r.get("entry_lead_hours") is None
                or not (0 < r["entry_lead_hours"] <= EARLY_ENTRY_HOURS))
    return {
        "ml": _agg(ml, "paper_ml_result", "clv_ml"),
        "total": _agg(tot, "paper_total_result", "clv_total"),
        "early_entries": early,
        "total_entries": len(all_bets),
    }


def _paper_bet_rows(settled) -> list:
    rows = []
    for r in sorted(settled, key=lambda x: x["game_date"], reverse=True):
        if r.get("paper_ml_side") and r.get("paper_ml_result") is not None:
            rows.append({
                "date": r["game_date"][:10], "matchup": f'{r["away_abbr"]} @ {r["home_abbr"]}',
                "type": "獨贏", "side": r["paper_ml_side"],
                "side_abbr": r["home_abbr"] if r["paper_ml_side"] == "home" else r["away_abbr"],
                "edge": r.get("edge_ml"), "result": r["paper_ml_result"], "clv": r.get("clv_ml"),
            })
        if r.get("paper_total_side") and r.get("paper_total_result") is not None:
            rows.append({
                "date": r["game_date"][:10], "matchup": f'{r["away_abbr"]} @ {r["home_abbr"]}',
                "type": "大小分", "side": r["paper_total_side"],
                "side_abbr": ("over" if r["paper_total_side"] == "over" else "under"),
                "line": r.get("market_total_line"),
                "edge": r.get("edge_total"), "result": r["paper_total_result"],
                "clv": r.get("clv_total"), "clv_pts": r.get("clv_total_pts"),
                "close_line": r.get("closing_total_line"),
            })
    return rows


def build_dashboard(out: Path = OUT_PATH) -> Path:
    data = gather_data()
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out.write_text(html, encoding="utf-8")
    return out


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WNBA 每日預測儀表板</title>
<style>
  :root{
    --surface-1:#fcfcfb; --page:#f9f9f7; --text-primary:#0b0b0b; --text-secondary:#52514e;
    --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,.10);
    --series-1:#2a78d6; --good:#0ca30c; --bad:#d03b3b; --card:#ffffff;
    --chip:#eef2f7;
  }
  :root[data-theme="dark"]{
    --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#fff; --text-secondary:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,.10);
    --series-1:#3987e5; --good:#0ca30c; --bad:#e34948; --card:#1f1f1e; --chip:#26261f;
  }
  @media (prefers-color-scheme:dark){
    :root[data-theme="auto"]{
      --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#fff; --text-secondary:#c3c2b7;
      --muted:#898781; --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,.10);
      --series-1:#3987e5; --good:#0ca30c; --bad:#e34948; --card:#1f1f1e; --chip:#26261f;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--page);color:var(--text-primary);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5;
    -webkit-font-smoothing:antialiased}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 64px}
  header{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:4px}
  h1{font-size:22px;margin:0;font-weight:650}
  .sub{color:var(--text-secondary);font-size:13px;margin:2px 0 20px}
  .theme-btn{background:var(--card);border:1px solid var(--border);color:var(--text-secondary);
    border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}
  h2{font-size:15px;font-weight:600;margin:28px 0 12px;letter-spacing:.01em}
  h2 .count{color:var(--muted);font-weight:400;font-size:13px;margin-left:6px}
  /* KPI tiles */
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .kpi .label{font-size:12px;color:var(--text-secondary);margin-bottom:6px}
  .kpi .val{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  .kpi .hint{font-size:11px;color:var(--muted);margin-top:4px}
  /* upcoming cards */
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .card .date{font-size:12px;color:var(--muted);margin-bottom:8px}
  .matchup{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .team{font-weight:600;font-size:15px}
  .team.win{color:var(--series-1)}
  .vs{color:var(--muted);font-size:12px}
  .score{font-variant-numeric:tabular-nums;font-weight:650;font-size:18px}
  .prob-row{display:flex;align-items:center;gap:8px;margin-top:12px}
  .bar{flex:1;height:7px;border-radius:99px;background:var(--grid);overflow:hidden}
  .bar>span{display:block;height:100%;background:var(--series-1);border-radius:99px}
  .prob-txt{font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums;min-width:118px;text-align:right}
  .pick{display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;background:var(--chip);
    color:var(--text-secondary);margin-top:10px}
  /* table */
  .tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:12px;background:var(--card)}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{padding:9px 12px;text-align:left;white-space:nowrap}
  th{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;
    border-bottom:1px solid var(--border)}
  td{border-bottom:1px solid var(--grid);font-variant-numeric:tabular-nums}
  tr:last-child td{border-bottom:none}
  .num{text-align:right}
  .ok{color:var(--good);font-weight:600}
  .no{color:var(--bad);font-weight:600}
  .filters{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
  .filters button{background:var(--card);border:1px solid var(--border);color:var(--text-secondary);
    border-radius:8px;padding:5px 12px;font-size:12px;cursor:pointer}
  .filters button.on{background:var(--series-1);color:#fff;border-color:var(--series-1)}
  /* chart */
  .chartcard{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
  svg{display:block;width:100%;height:auto;overflow:visible}
  .tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
    border-radius:8px;padding:6px 9px;font-size:12px;color:var(--text-primary);opacity:0;
    transition:opacity .1s;box-shadow:0 4px 14px rgba(0,0,0,.12);z-index:9}
  .empty{color:var(--muted);font-size:13px;padding:14px 2px}
  .note{font-size:11px;color:var(--muted);margin-top:8px}
  /* betting */
  .mkt-row{display:flex;justify-content:space-between;font-size:12px;color:var(--text-secondary);
    margin-top:8px;padding-top:8px;border-top:1px dashed var(--grid);font-variant-numeric:tabular-nums}
  .edge{font-weight:650}
  .edge.pos{color:var(--good)} .edge.neg{color:var(--muted)}
  .badge{display:inline-block;font-size:10px;font-weight:600;padding:1px 6px;border-radius:99px;
    background:var(--series-1);color:#fff;margin-left:6px;vertical-align:middle}
  .kpi .val.pos{color:var(--good)} .kpi .val.neg{color:var(--bad)}
  .disclaimer{font-size:12px;color:var(--text-secondary);background:var(--chip);border:1px solid var(--border);
    border-radius:10px;padding:10px 14px;margin:0 0 14px;line-height:1.6}
  .seg{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;background:var(--chip);
    color:var(--text-secondary);margin-right:6px}
  /* 判斷 pill + 總體建議 banner */
  .j{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;white-space:nowrap}
  .j-avoid{background:rgba(208,59,59,.14);color:var(--bad)}
  .j-watch{background:rgba(234,161,0,.16);color:#a86b00}
  .j-weak{background:var(--chip);color:var(--muted)}
  :root[data-theme="dark"] .j-watch{color:#fab219}
  @media (prefers-color-scheme:dark){:root[data-theme="auto"] .j-watch{color:#fab219}}
  .decision{border-radius:12px;padding:14px 16px;margin:0 0 14px;font-size:13px;line-height:1.6;
    border:1px solid var(--border)}
  .decision b{font-size:14px}
  .decision.hold{background:rgba(234,161,0,.10)}
  .decision.stop{background:rgba(208,59,59,.10)}
  .decision.go{background:rgba(12,163,12,.10)}
  .jhint{font-size:11px;color:var(--muted);margin-top:2px}
  /* 我的下注（localStorage） */
  .mybet{margin-top:10px;padding-top:8px;border-top:1px dashed var(--grid)}
  .mybet-toggle{background:none;border:1px dashed var(--border);color:var(--text-secondary);
    border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer;width:100%}
  .mybet-form{display:none;margin-top:8px;gap:6px;flex-wrap:wrap;align-items:center}
  .mybet-form.show{display:flex}
  .mybet-form select,.mybet-form input{font-size:12px;padding:5px 6px;border:1px solid var(--border);
    border-radius:6px;background:var(--surface-1);color:var(--text-primary)}
  .mybet-form input{width:62px}
  .mybet-save{background:var(--series-1);color:#fff;border:none;border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer}
  .mybet-chip{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;
    background:var(--chip);border-radius:8px;padding:5px 10px;margin-top:6px;font-variant-numeric:tabular-nums}
  .mybet-chip .del{color:var(--bad);cursor:pointer;font-size:16px;line-height:1;border:none;background:none;padding:0 2px}
  .mb-export{background:none;border:1px solid var(--border);color:var(--text-secondary);
    border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer;margin-left:6px}
  footer{margin-top:40px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);padding-top:14px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>WNBA 每日預測儀表板</h1>
    </div>
    <button class="theme-btn" id="themeBtn">深/淺色</button>
  </header>
  <div class="sub" id="subtitle"></div>

  <div class="kpis" id="kpis"></div>

  <!-- 投注評估區（無盤口時整段隱藏）-->
  <div id="betting" style="display:none">
    <h2>投注評估（紙上模擬） <span class="count">edge ≥ 3% 自動記一注・非投注建議</span></h2>
    <div class="disclaimer" id="betDisclaimer"></div>
    <div class="kpis" id="paperKpis"></div>
    <div class="decision" id="decision"></div>

    <h2>輸贏候選（獨贏） <span class="count" id="candMLCount"></span></h2>
    <div class="note" style="margin:0 0 8px">「候選」= 值得看一眼的觀察名單，不是下注建議。請以「判斷」欄與上方總體建議為準。<br>
      「模型勝率」與「市場勝率」都是<b>下注邊</b>的勝率（不是主隊）。所以下注邊機率低於 50% 很正常——
      這是在賭「被低估」，不是在賭「誰會贏」。兩欄都是<b>記注當下</b>的凍結值；若模型後來改變看法，下方會標「現在 x%」，
      判斷欄則會顯示「已失效」。</div>
    <div class="tablewrap">
      <table>
        <thead><tr><th>開賽</th><th>對戰</th><th>下注邊</th>
          <th class="num">模型勝率</th><th class="num">市場勝率</th><th class="num">edge</th><th>判斷</th></tr></thead>
        <tbody id="candMLBody"></tbody>
      </table>
    </div>

    <h2>比分候選（大小分） <span class="count" id="candTotalCount"></span></h2>
    <div class="tablewrap">
      <table>
        <thead><tr><th>開賽</th><th>對戰</th><th>下注邊</th>
          <th class="num">模型總分</th><th class="num">盤口線</th><th class="num">edge</th><th>判斷</th></tr></thead>
        <tbody id="candTotalBody"></tbody>
      </table>
    </div>
  </div>

  <h2>今日 / 未來賽事 <span class="count" id="pendCount"></span></h2>
  <div class="cards" id="pending"></div>

  <h2>命中率趨勢 <span class="count">近 40 場・10 場滾動勝負命中率</span></h2>
  <div class="chartcard"><svg id="trend" viewBox="0 0 720 220"></svg>
    <div class="note">灰色虛線 = 50%（隨機猜測水準）。點越高代表近況預測越準。</div>
  </div>

  <h2>歷史回顧 <span class="count" id="histCount"></span></h2>
  <div class="filters" id="filters">
    <button data-f="all" class="on">全部</button>
    <button data-f="hit">命中</button>
    <button data-f="miss">未中</button>
  </div>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>日期</th><th>對戰（客 @ 主）</th><th class="num">預測比分</th>
        <th class="num">實際比分</th><th>預測勝方</th><th>勝負</th>
        <th class="num">勝率</th><th class="num">總分誤差</th>
      </tr></thead>
      <tbody id="histBody"></tbody>
    </table>
  </div>

  <!-- 紙上下注紀錄（無盤口時隱藏）-->
  <div id="paperSection" style="display:none">
    <h2>輸贏紀錄（獨贏） <span class="count" id="pbMLCount"></span></h2>
    <div class="tablewrap">
      <table>
        <thead><tr><th>日期</th><th>對戰</th><th>下注邊</th>
          <th class="num">edge</th><th class="num">結果(單位)</th><th class="num">CLV</th></tr></thead>
        <tbody id="pbMLBody"></tbody>
      </table>
    </div>

    <h2>比分紀錄（大小分） <span class="count" id="pbTotalCount"></span></h2>
    <div class="tablewrap">
      <table>
        <thead><tr><th>日期</th><th>對戰</th><th>下注邊</th>
          <th class="num">edge</th><th class="num">結果(單位)</th><th class="num">CLV</th></tr></thead>
        <tbody id="pbTotalBody"></tbody>
      </table>
    </div>
    <div class="note">CLV（收盤線價值）&gt; 0 = 你進場的線贏過收盤線，是長期有無 edge 最早的訊號。結果為每注 1 單位的淨利。</div>
  </div>

  <!-- 我的下注紀錄（跨裝置：來自 repo 的 data/my_bets.csv）-->
  <h2>我的下注紀錄 <span class="count" id="mbCount"></span></h2>
  <div class="kpis" id="mbKpis"></div>
  <div class="tablewrap">
    <table>
      <thead><tr><th>日期</th><th>對戰</th><th>我的標的</th><th class="num">注額</th>
        <th class="num">賠率</th><th>模型看法</th><th>誰對</th><th class="num">損益</th></tr></thead>
      <tbody id="mbBody"></tbody>
    </table>
  </div>
  <div class="note" id="mbNote"></div>

  <footer id="foot"></footer>
</div>
<div class="tip" id="tip"></div>

<script>
const DATA = __DATA__;
const $ = s => document.querySelector(s);
const fmt1 = x => x==null ? "—" : x.toFixed(1);
const pct = x => x==null ? "—" : (x*100).toFixed(1)+"%";

// ---- theme toggle ----
$("#themeBtn").onclick = () => {
  const el = document.documentElement;
  const cur = el.getAttribute("data-theme");
  const isDark = cur==="dark" || (cur==="auto" && matchMedia("(prefers-color-scheme:dark)").matches);
  el.setAttribute("data-theme", isDark ? "light" : "dark");
  drawTrend();
};

// ---- subtitle ----
const gen = new Date(DATA.generated_at);
$("#subtitle").textContent =
  `最後更新 ${gen.toLocaleString("zh-TW",{timeZone:"Asia/Taipei"})}（台北時間）　`+
  `已結算 ${DATA.metrics.n_settled} 場・待賽 ${DATA.metrics.n_pending} 場`;

// ---- KPI tiles ----
const m = DATA.metrics;
const kpis = [
  {label:"勝負命中率", val: pct(m.acc), hint:`${m.n_settled} 場已結算`},
  {label:"總分平均誤差 (MAE)", val: m.total_mae==null?"—":fmt1(m.total_mae)+" 分", hint:"預測總分 vs 實際"},
  {label:"分差平均誤差", val: m.margin_mae==null?"—":fmt1(m.margin_mae)+" 分", hint:"預測分差 vs 實際"},
  {label:"機率校準 Brier", val: m.brier==null?"—":m.brier.toFixed(3), hint:"越低越準・0.25=亂猜"},
];
$("#kpis").innerHTML = kpis.map(k=>
  `<div class="kpi"><div class="label">${k.label}</div>`+
  `<div class="val">${k.val}</div><div class="hint">${k.hint}</div></div>`).join("");

// ---- pending cards ----
const tz = {timeZone:"Asia/Taipei", month:"numeric", day:"numeric", weekday:"short", hour:"2-digit", minute:"2-digit"};
$("#pendCount").textContent = `${DATA.pending.length} 場`;
const pend = $("#pending");
if(!DATA.pending.length){ pend.innerHTML = `<div class="empty">目前沒有待預測的比賽。</div>`; }
else {
  pend.innerHTML = DATA.pending.map(g=>{
    const d = new Date(g.game_date).toLocaleString("zh-TW", tz);
    const homeWin = g.pred_winner==="home";
    const pHome = g.p_home_win, pShown = homeWin ? pHome : 1-pHome;
    const pickAbbr = homeWin ? g.home_abbr : g.away_abbr;
    return `<div class="card">
      <div class="date">${d}</div>
      <div class="matchup">
        <span class="team ${!homeWin?'win':''}">${g.away_abbr}</span>
        <span class="score">${g.pred_away_score} <span class="vs">–</span> ${g.pred_home_score}</span>
        <span class="team ${homeWin?'win':''}">${g.home_abbr}</span>
      </div>
      <div class="prob-row">
        <div class="bar"><span style="width:${(pShown*100).toFixed(0)}%"></span></div>
        <div class="prob-txt">${pickAbbr} 勝率 ${(pShown*100).toFixed(0)}%</div>
      </div>
      <span class="pick">預測勝方：${pickAbbr}　預測總分 ${Math.round(g.pred_total)}　信心 ${g.confidence}</span>
      ${marketRows(g)}
      ${mybetBlock(g)}
    </div>`;
  }).join("");
}

// 每張卡片的「記錄我的下注」表單（送出後存到 Google 後端）
function mybetBlock(g){
  if(!DATA.bets_endpoint) return "";   // 未設定後端就不顯示表單
  const line = g.market_total_line!=null ? g.market_total_line : "";
  const gd = new Date(g.game_date).toLocaleDateString("en-CA",{timeZone:"Asia/Taipei"}); // YYYY-MM-DD
  return `<div class="mybet" data-away="${g.away_abbr}" data-home="${g.home_abbr}" data-date="${gd}">
    <div class="mybet-list"></div>
    <button class="mybet-toggle" type="button">＋ 記錄我的下注</button>
    <div class="mybet-form">
      <select class="mb-target">
        <option value="ML|away">${g.away_abbr} 獨贏</option>
        <option value="ML|home">${g.home_abbr} 獨贏</option>
        <option value="TOT|over">大分</option>
        <option value="TOT|under">小分</option>
      </select>
      <input class="mb-stake" type="number" placeholder="注額" inputmode="decimal">
      <input class="mb-odds" type="number" placeholder="賠率" inputmode="decimal">
      <input class="mb-line" type="number" placeholder="盤口線" inputmode="decimal" style="display:none" value="${line}">
      <button class="mybet-save" type="button">儲存</button>
    </div>
  </div>`;
}

// 卡片內的市場對照（有盤口才顯示）
function marketRows(g){
  if(g.market_p_home==null && g.market_total_line==null) return "";
  let html = "";
  if(g.market_p_home!=null){
    const mShown = g.pred_winner==="home" ? g.market_p_home : 1-g.market_p_home;
    const e = g.edge_ml, flagged = !!g.paper_ml_side;
    html += `<div class="mkt-row"><span>獨贏　模型 ${(pShown0(g))}% ・ 市場 ${(mShown*100).toFixed(0)}%</span>`+
      `<span class="edge ${flagged?'pos':'neg'}">edge ${e==null?'—':(e>=0?'+':'')+(e*100).toFixed(1)+'%'}`+
      `${flagged?'<span class="badge">候選</span>':''}</span></div>`;
  }
  if(g.market_total_line!=null){
    const pOver = g.pred_total!=null && g.market_p_over!=null ? null : null;
    const side = g.paper_total_side;
    const e = g.edge_total, flagged = !!side;
    const sideTxt = side ? (side==="over"?"大分":"小分") : (g.pred_total>g.market_total_line?"偏大":"偏小");
    html += `<div class="mkt-row"><span>大小分　盤口 ${g.market_total_line} ・ 模型 ${Math.round(g.pred_total)}（${sideTxt}）</span>`+
      `<span class="edge ${flagged?'pos':'neg'}">edge ${e==null?'—':(e>=0?'+':'')+(e*100).toFixed(1)+'%'}`+
      `${flagged?'<span class="badge">候選</span>':''}</span></div>`;
  }
  return html;
}
function pST(g){ return g.pred_winner==="home" ? g.p_home_win : 1-g.p_home_win; }
function pShown0(g){ return (pST(g)*100).toFixed(0); }

// ---- 投注評估區 ----
if(DATA.has_market){
  $("#betting").style.display = "";
  $("#paperSection").style.display = "";
  $("#betDisclaimer").innerHTML =
    "參考基準為<b>國際盤共識</b>（多家博彩商去水錢後的中位數），非台灣運彩線——運彩通常更差，"+
    "所以這是偏寬鬆的照妖鏡。以下為<b>紙上模擬、非投注建議</b>：系統把「模型看好且贏過市場 ≥3%」的場次自動記為 1 單位下注，"+
    "用來評估你到底有沒有 edge。<b>CLV（贏過收盤線）比損益更早、更可信</b>——樣本要夠大才有意義。";
  const P = DATA.paper;
  function paperTile(label, a){
    const roi = a.roi==null?null:a.roi*100;
    const roiCls = roi==null?"":(roi>=0?"pos":"neg");
    return `<div class="kpi"><div class="label">${label}（${a.n} 注）</div>`+
      `<div class="val ${roiCls}">${roi==null?"—":(roi>=0?"+":"")+roi.toFixed(1)+"%"}</div>`+
      `<div class="hint">命中 ${a.hit_rate==null?"—":(a.hit_rate*100).toFixed(0)+"%"}`+
      ` ・ 贏過收盤 ${a.pct_pos_clv==null?"—":(a.pct_pos_clv*100).toFixed(0)+"%"}`+
      ` ・ 平均CLV ${a.avg_clv==null?"—":(a.avg_clv>=0?"+":"")+(a.avg_clv*100).toFixed(1)+"pp"}</div></div>`;
  }
  $("#paperKpis").innerHTML = paperTile("獨贏 ROI", P.ml) + paperTile("大小分 ROI", P.total);

  // ---- 決策判斷邏輯 ----
  // 每一列給一個「判斷」：大 edge（尤其押冷門）多半是模型漏看→觀望；
  // 溫和 edge 合理但未驗證→候選觀察；接近門檻→訊號微弱。
  // 進場當下模型看這一邊的機率（凍結）；沒有就退回現況
  function entryP(g){
    const eh = (g.entry_p_home_model==null) ? g.p_home_win : g.entry_p_home_model;
    return g.paper_ml_side==="home" ? eh : 1-eh;
  }
  // 今天模型看這一邊的機率（每天會變）
  function nowP(g){ return g.paper_ml_side==="home" ? g.p_home_win : 1-g.p_home_win; }

  function jml(g){
    const e = g.edge_ml;
    const backP = g.paper_ml_side==="home" ? g.market_p_home : 1-g.market_p_home;
    const dog = backP < 0.5;                       // 是否在押冷門
    // 這幾天模型改變看法、已經不站在這一邊了 → 這個 edge 已經失效
    if(nowP(g) < backP) return {c:"j-avoid", t:"已失效", h:"模型後來翻盤，現在不站這邊了"};
    if(e >= 0.12) return {c:"j-avoid", t:"避開", h:"edge 過大，多半是模型漏看資訊"};
    if(dog && e >= 0.08) return {c:"j-avoid", t:"避開", h:"大 edge 押冷門，逆選擇風險高"};
    if(e >= 0.05) return {c:"j-watch", t:"候選觀察", h:"edge 溫和合理，但尚未經 CLV 驗證"};
    return {c:"j-weak", t:"訊號微弱", h:"edge 偏小，可能只是雜訊"};
  }
  function jtot(g){
    const e = g.edge_total;
    const line = g.market_total_line;
    // 模型總分後來跑到盤口線的另一邊 → 這個 edge 已經失效
    if(line!=null && ((g.paper_total_side==="over" && g.pred_total < line) ||
                      (g.paper_total_side==="under" && g.pred_total > line)))
      return {c:"j-avoid", t:"已失效", h:"模型總分後來跨過盤口線，方向反了"};
    if(e >= 0.12) return {c:"j-avoid", t:"避開", h:"edge 過大，模型與盤口差太多"};
    if(e >= 0.05) return {c:"j-watch", t:"候選觀察", h:"edge 溫和合理，但尚未經 CLV 驗證"};
    return {c:"j-weak", t:"訊號微弱", h:"edge 偏小，可能只是雜訊"};
  }
  function jcell(j){ return `<td><span class="j ${j.c}">${j.t}</span><div class="jhint">${j.h}</div></td>`; }

  // ---- 總體建議 banner：依樣本數與整體 CLV 決定該不該進場 ----
  (function(){
    const P = DATA.paper, n = P.ml.n + P.total.n;
    const clvs = [P.ml.avg_clv, P.total.avg_clv].filter(x=>x!=null);
    const avgClv = clvs.length ? clvs.reduce((a,b)=>a+b,0)/clvs.length : null;
    const early = P.early_entries || 0, tot = P.total_entries || 0;
    let cls, html;
    // 太早記的注會讓 CLV 系統性偏樂觀，所以只要大半是這種，就不准 banner 轉綠。
    // 寧可少賺也不要把一個量錯的數字包裝成「可以下注了」。
    if(tot && early / tot > 0.5){
      cls = "hold";
      html = `<b>總體建議：先全部觀望。目前的 CLV 還不能當判準。</b><br>`+
        `${tot} 注裡有 ${early} 注的進場盤口是開賽 48 小時以前記的。那不是你真的下得到的價，`+
        `而早期線本來就偏離真值、收盤會往真值收斂，所以只要模型大致正確，CLV 就會假性偏高。`+
        `記注時機已經改成只在開賽前 48 小時內開倉，請等新規則下的注累積起來再看這一區。`;
    } else if(n < 20){
      cls = "hold";
      html = `<b>總體建議：先全部觀望、不下真錢。</b><br>目前只累積了 ${n} 注，樣本太小、還無法判斷模型有沒有真 edge。`+
        `請先讓它跑，等累積到約 30 注、且下方「平均 CLV」有意義後再回來看這裡。`;
    } else if(avgClv == null || avgClv <= 0){
      cls = "stop";
      html = `<b>總體建議：繼續紙上觀察、不投入真錢。</b><br>目前 ${n} 注的平均 CLV 為 `+
        `${avgClv==null?"—":(avgClv*100).toFixed(1)+"pp"}（≤0），代表你的下注長期<b>輸給收盤線</b>——還沒看到可信的 edge。`+
        `這時候下真錢，期望值是負的。`;
    } else {
      cls = "go";
      html = `<b>總體建議：訊號初步為正，若要試僅小注、且只挑「候選觀察」。</b><br>目前 ${n} 注平均 CLV `+
        `+${(avgClv*100).toFixed(1)}pp（>0），代表你平均<b>贏過收盤線</b>，訊號較可信。`+
        `即便如此，也請避開「避開」標籤那種大 edge、只挑溫和的、下小注控管風險，並持續看 CLV 是否維持為正。`;
    }
    $("#decision").className = "decision " + cls;
    $("#decision").innerHTML = html + `<div class="jhint" style="margin-top:6px">此為分析輔助、非投注建議；投注長期為負期望、有輸錢風險。</div>`;
  })();

  // ---- 候選：輸贏（獨贏）與 比分（大小分）各自一張表，各自照 edge 由大到小 ----
  const ctz = {timeZone:"Asia/Taipei", month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"};
  const C = DATA.edge_candidates || [];

  const mlC = C.filter(g=>g.paper_ml_side).sort((a,b)=>b.edge_ml-a.edge_ml);
  $("#candMLCount").textContent = `${mlC.length} 場`;
  if(!mlC.length){ $("#candMLBody").innerHTML = `<tr><td colspan="7" class="empty">今天沒有超過門檻的獨贏 edge。</td></tr>`; }
  else $("#candMLBody").innerHTML = mlC.map(g=>{
    const d = new Date(g.game_date).toLocaleString("zh-TW", ctz);
    const sideAbbr = g.paper_ml_side==="home"?g.home_abbr:g.away_abbr;
    const mp = g.paper_ml_side==="home"?g.market_p_home:1-g.market_p_home;
    const modp = entryP(g), curp = nowP(g);
    const drift = Math.abs(curp-modp) >= 0.02
      ? `<div class="jhint">現在 ${(curp*100).toFixed(0)}%</div>` : "";
    return `<tr><td>${d}</td><td>${g.away_abbr}@${g.home_abbr}</td>`+
      `<td><b>${sideAbbr}</b></td><td class="num">${(modp*100).toFixed(0)}%${drift}</td>`+
      `<td class="num">${(mp*100).toFixed(0)}%</td><td class="num ok">+${(g.edge_ml*100).toFixed(1)}%</td>`+
      jcell(jml(g))+`</tr>`;
  }).join("");

  const totC = C.filter(g=>g.paper_total_side).sort((a,b)=>b.edge_total-a.edge_total);
  $("#candTotalCount").textContent = `${totC.length} 場`;
  if(!totC.length){ $("#candTotalBody").innerHTML = `<tr><td colspan="7" class="empty">今天沒有超過門檻的大小分 edge。</td></tr>`; }
  else $("#candTotalBody").innerHTML = totC.map(g=>{
    const d = new Date(g.game_date).toLocaleString("zh-TW", ctz);
    const sideTxt = g.paper_total_side==="over"?`大分 O ${g.market_total_line}`:`小分 U ${g.market_total_line}`;
    const et = (g.entry_pred_total==null) ? g.pred_total : g.entry_pred_total;
    const tdrift = Math.abs(g.pred_total-et) >= 1
      ? `<div class="jhint">現在 ${Math.round(g.pred_total)}</div>` : "";
    return `<tr><td>${d}</td><td>${g.away_abbr}@${g.home_abbr}</td>`+
      `<td><b>${sideTxt}</b></td><td class="num">${Math.round(et)}${tdrift}</td>`+
      `<td class="num">${g.market_total_line}</td><td class="num ok">+${(g.edge_total*100).toFixed(1)}%</td>`+
      jcell(jtot(g))+`</tr>`;
  }).join("");

  // ---- 紙上下注紀錄：同樣拆兩張表 ----
  const B = DATA.paper_bets || [];
  function pbRow(b){
    const win = b.result>0, push = b.result===0;
    const resTxt = push?"退注":(win?"+"+b.result.toFixed(2):b.result.toFixed(2));
    const resCls = push?"":(win?"ok":"no");
    // 大小分的 CLV 本質是「盤口線往你的方向走了幾分」，直接把分數寫出來比 pp 好懂
    let clvTxt = b.clv==null?"—":(b.clv>=0?"+":"")+(b.clv*100).toFixed(1)+"pp";
    if(b.clv!=null && b.clv_pts!=null && b.clv_pts!==0)
      clvTxt = (b.clv_pts>=0?"+":"")+b.clv_pts.toFixed(1)+"分 ("+clvTxt+")";
    const clvCls = b.clv==null?"":(b.clv>=0?"ok":"no");
    const sideTxt = b.type==="大小分" ? (b.side==="over"?`大分 O${b.line??""}`:`小分 U${b.line??""}`) : b.side_abbr;
    return `<tr><td>${b.date.slice(5)}</td><td>${b.matchup}</td>`+
      `<td><b>${sideTxt}</b></td><td class="num">+${(b.edge*100).toFixed(1)}%</td>`+
      `<td class="num ${resCls}">${resTxt}</td><td class="num ${clvCls}">${clvTxt}</td></tr>`;
  }
  const mlB = B.filter(b=>b.type==="獨贏"), totB = B.filter(b=>b.type==="大小分");
  $("#pbMLCount").textContent = `${mlB.length} 注`;
  $("#pbTotalCount").textContent = `${totB.length} 注`;
  $("#pbMLBody").innerHTML = mlB.length ? mlB.map(pbRow).join("")
    : `<tr><td colspan="6" class="empty">還沒有已結算的獨贏紙上下注。</td></tr>`;
  $("#pbTotalBody").innerHTML = totB.length ? totB.map(pbRow).join("")
    : `<tr><td colspan="6" class="empty">還沒有已結算的大小分紙上下注。</td></tr>`;
}

// ==== 我的下注紀錄（跨裝置：存到 Google 後端；即時、任何裝置同步）＋ 與模型比對 ====
const EP = DATA.bets_endpoint || "";
let MYBETS = [];   // 從後端抓來的下注列表

// 用 (客,主,台北日期) 找到對應比賽，才能算損益與模型看法
function tpeDate(iso){ try{ return new Date(iso).toLocaleDateString("en-CA",{timeZone:"Asia/Taipei"}); }catch(e){ return (iso||"").slice(0,10); } }
const GAMEIDX = {};
(DATA.settled||[]).concat(DATA.pending||[]).forEach(g=>{
  GAMEIDX[[g.away_abbr,g.home_abbr,tpeDate(g.game_date)].join("|")] = g;
});
// 試算表回傳的日期可能是純日期字串 "2026-07-29"，也可能是 Google 幫你轉成
// Date 之後序列化的完整時間 "2026-07-28T16:00:00.000Z"（那其實就是台北 7/29 00:00）。
// 只截前 10 碼會得到 "2026-07-28"（差一天）甚至 "07-28T16:0"（整個對不上），
// 這就是「無法對應」的來源。統一先換算成台北日期再比對。
function betDate(b){
  const s = String(b.date || "");
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : tpeDate(s);
}
// 賠率：十進位一定 > 1（1.75 = 押 1 賺 0.75）。台灣習慣寫淨賠率 0.75，
// 直接丟進 stake*(odds-1) 會算成倒賠。<=1 一律當成淨賠率補回 1。
function decOdds(o){ const x = +o; return (isFinite(x) && x > 1) ? x : x + 1; }
function gameOf(b){ return GAMEIDX[[String(b.away).toUpperCase(),String(b.home).toUpperCase(),betDate(b)].join("|")]; }

function targetLabel(b){
  if(String(b.market).toUpperCase()==="ML") return (String(b.side).toUpperCase()===String(b.home).toUpperCase()?b.home:b.away)+" 獨贏";
  const s=String(b.side).toLowerCase();
  return (s==="over"?"大分 O":"小分 U")+(b.line!==""&&b.line!=null?(" "+b.line):"");
}
function betResult(b){
  const g=gameOf(b);
  if(!g) return {status:"無法對應", pnl:null, yw:null};
  if(g.actual_winner==null) return {status:"待開賽", pnl:null, yw:null};
  const stake=+b.stake, odds=decOdds(b.odds);
  if(String(b.market).toUpperCase()==="ML"){
    const my = String(b.side).toUpperCase()===String(b.home).toUpperCase()?"home":"away";
    const yw = (my===g.actual_winner);
    return {status: yw?"贏":"輸", pnl: yw? stake*(odds-1) : -stake, yw};
  }
  const line=+b.line, s=String(b.side).toLowerCase();
  if(b.line===""||b.line==null||g.actual_total==null) return {status:"缺盤口線", pnl:null, yw:null};
  if(g.actual_total===line) return {status:"退注", pnl:0, yw:null};
  const ow=g.actual_total>line, yw=(s==="over"&&ow)||(s==="under"&&!ow);
  return {status: yw?"贏":"輸", pnl: yw? stake*(odds-1) : -stake, yw};
}
function modelView(b){
  const g=gameOf(b); if(!g) return {side:"—", agree:null, mw:null};
  if(String(b.market).toUpperCase()==="ML"){
    const mSide = (g.p_home_win!=null&&g.p_home_win>=0.5)?"home":"away";
    const my = String(b.side).toUpperCase()===String(b.home).toUpperCase()?"home":"away";
    let mw=null; if(g.actual_winner!=null) mw=(mSide===g.actual_winner);
    return {side:(mSide==="home"?g.home_abbr:g.away_abbr)+" 贏", agree:(mSide===my), mw};
  }
  if(b.line===""||b.line==null) return {side:"—", agree:null, mw:null};
  const line=+b.line, s=String(b.side).toLowerCase();
  const mSide = (g.pred_total!=null&&g.pred_total>line)?"over":"under";
  let mw=null; if(g.actual_total!=null&&g.actual_total!==line){ const ow=g.actual_total>line; mw=(mSide==="over"&&ow)||(mSide==="under"&&!ow); }
  return {side:(mSide==="over"?"大分":"小分"), agree:(mSide===s), mw};
}

function renderMyBets(){
  const bets=[...MYBETS].sort((a,b)=>betDate(b).localeCompare(betDate(a)));
  // 卡片上已存注
  document.querySelectorAll(".mybet").forEach(el=>{
    const key=[el.dataset.away,el.dataset.home,el.dataset.date].join("|");
    const list=bets.filter(b=>[String(b.away).toUpperCase(),String(b.home).toUpperCase(),betDate(b)].join("|")===key);
    el.querySelector(".mybet-list").innerHTML=list.map(b=>{
      const r=betResult(b);
      const tag=r.status==="待開賽"?"":`・${r.status}${r.pnl!=null?`（${r.pnl>=0?"+":""}${r.pnl.toFixed(0)}）`:""}`;
      return `<div class="mybet-chip"><span>${targetLabel(b)}　${b.stake}@${b.odds}${tag}</span>`+
        `<button class="del" data-id="${b.id}" title="刪除">×</button></div>`;
    }).join("");
  });
  const tile=(l,v,c,h)=>`<div class="kpi"><div class="label">${l}</div><div class="val ${c||''}">${v}</div><div class="hint">${h||''}</div></div>`;
  const decided=bets.map(b=>({b,r:betResult(b),mv:modelView(b)})).filter(x=>x.r.yw!=null);
  $("#mbCount").textContent=`${bets.length} 注`;
  if(!decided.length){
    $("#mbKpis").innerHTML=tile("尚無已結算下注","—","",bets.length?"比賽打完後才有統計":"還沒有任何記錄");
  } else {
    const n=decided.length, pnl=decided.reduce((s,x)=>s+(x.r.pnl||0),0), staked=decided.reduce((s,x)=>s+ +x.b.stake,0);
    const yh=decided.filter(x=>x.r.yw).length, mh=decided.filter(x=>x.mv.mw===true).length, roi=staked?pnl/staked*100:0;
    $("#mbKpis").innerHTML=
      tile("淨損益（依你的賠率）",(pnl>=0?"+":"")+pnl.toFixed(0),pnl>=0?"pos":"neg",`ROI ${roi>=0?"+":""}${roi.toFixed(1)}% ・ ${n} 注已結算`)+
      tile("你的命中率",(yh/n*100).toFixed(0)+"%","",`${yh}/${n} 命中`)+
      tile("模型命中率（同場）",(mh/n*100).toFixed(0)+"%","",`${mh}/${n} 命中`)+
      tile("你 vs 模型",yh>mh?"你較準":(yh<mh?"模型較準":"平手"),yh>mh?"pos":(yh<mh?"neg":""),`你 ${yh} : 模型 ${mh}`);
  }
  $("#mbBody").innerHTML = !bets.length ?
    `<tr><td colspan="8" class="empty">${EP?"還沒有記錄。到上面任一張比賽卡片點「＋ 記錄我的下注」。":"尚未設定 Google 後端（見下方說明）。"}</td></tr>` :
    bets.map(b=>{
      const r=betResult(b), mv=modelView(b);
      const pnlTxt=r.pnl==null?"—":(r.pnl>=0?"+":"")+r.pnl.toFixed(0);
      const pnlCls=r.pnl==null?"":(r.pnl>0?"ok":(r.pnl<0?"no":""));
      let who="—";
      if(r.yw!=null&&mv.mw!=null) who=r.yw&&mv.mw?"都對":(!r.yw&&!mv.mw?"都錯":(r.yw?"<span class='ok'>你對 ✓</span>":"<span class='no'>模型對</span>"));
      else if(r.status==="待開賽"||r.status==="無法對應"||r.status==="缺盤口線") who=r.status;
      const agreeTag=mv.agree==null?"":(mv.agree?" <span class='seg'>同</span>":" <span class='seg'>異</span>");
      return `<tr><td>${betDate(b).slice(5)}</td><td>${b.away}@${b.home}</td><td><b>${targetLabel(b)}</b></td>`+
        `<td class="num">${b.stake}</td><td class="num">${b.odds}</td><td>${mv.side}${agreeTag}</td>`+
        `<td>${who}</td><td class="num ${pnlCls}">${pnlTxt}</td></tr>`;
    }).join("");
  $("#mbNote").innerHTML = EP ?
    "紀錄存在你的 Google 試算表，<b>任何裝置打開網站都同步看得到</b>。到任一張比賽卡片點「＋ 記錄我的下注」即可新增。「誰對」比較你和模型當時看好的一邊誰猜中。此為個人記錄工具、非投注建議。" :
    "尚未接上 Google 後端——請照我提供的步驟建立 Google 試算表與 Apps Script，並把網址設成 GitHub secret <code>BETS_ENDPOINT</code>。設好後這區就會出現記錄表單。";
}

// JSONP 讀取（避開跨網域限制）
function fetchBets(){
  if(!EP) return Promise.resolve([]);
  return new Promise((resolve)=>{
    const cb="__mb_cb_"+Math.floor(Math.random()*1e9);
    const s=document.createElement("script");
    window[cb]=d=>{ resolve((d&&d.bets)||[]); delete window[cb]; s.remove(); };
    s.onerror=()=>{ resolve(MYBETS); s.remove(); };
    s.src=EP+"?callback="+cb;
    document.body.appendChild(s);
  });
}
function reloadBets(){ fetchBets().then(b=>{ MYBETS=b; renderMyBets(); }); }
// 寫入用 no-cors POST（送出後隔一下重抓）
function postBet(payload){
  return fetch(EP,{method:"POST",mode:"no-cors",headers:{"Content-Type":"text/plain;charset=utf-8"},body:JSON.stringify(payload)});
}

// 事件：展開表單 / 儲存 / 刪除 / 切換盤口線欄
document.addEventListener("click", e=>{
  const t=e.target;
  if(t.classList.contains("mybet-toggle")){ t.nextElementSibling.classList.toggle("show"); }
  else if(t.classList.contains("mybet-save")){
    const box=t.closest(".mybet"), sel=box.querySelector(".mb-target").value;
    const stake=parseFloat(box.querySelector(".mb-stake").value), odds=parseFloat(box.querySelector(".mb-odds").value);
    const lineRaw=box.querySelector(".mb-line").value;
    if(!(stake>0)||!(odds>1)){ alert("請填有效的注額與賠率（賠率需 > 1）。"); return; }
    const isTot=sel.startsWith("TOT"); const [mkt,side]=sel.split("|");
    const line=isTot?(lineRaw!==""?parseFloat(lineRaw):null):"";
    if(isTot&&line==null){ alert("大小分請填盤口線。"); return; }
    const sideVal = isTot? side : (side==="home"?box.dataset.home:box.dataset.away);
    const bet={date:box.dataset.date, away:box.dataset.away, home:box.dataset.home,
      market:mkt==="TOT"?"OU":"ML", side:sideVal, stake, odds, line:isTot?line:""};
    t.textContent="儲存中…"; t.disabled=true;
    MYBETS.push({...bet, id:"tmp_"+Date.now()}); renderMyBets();     // 樂觀更新
    postBet(bet).finally(()=>{ setTimeout(()=>{ reloadBets(); }, 900);
      box.querySelector(".mybet-form").classList.remove("show");
      box.querySelector(".mb-stake").value=""; box.querySelector(".mb-odds").value="";
      t.textContent="儲存"; t.disabled=false; });
  }
  else if(t.classList.contains("del")){
    if(!EP) return; const id=t.dataset.id;
    MYBETS=MYBETS.filter(b=>String(b.id)!==String(id)); renderMyBets();
    postBet({action:"delete", id}).finally(()=>setTimeout(reloadBets, 900));
  }
});
document.addEventListener("change", e=>{
  if(e.target.classList.contains("mb-target")){
    const box=e.target.closest(".mybet");
    box.querySelector(".mb-line").style.display = e.target.value.startsWith("TOT")?"":"none";
  }
});
reloadBets();

// ---- history table ----
let filter = "all";
function renderHist(){
  const rows = DATA.settled.filter(r=>
    filter==="all" ? true : filter==="hit" ? r.winner_correct===1 : r.winner_correct===0);
  $("#histCount").textContent = `${DATA.settled.length} 場`;
  const body = $("#histBody");
  if(!rows.length){ body.innerHTML = `<tr><td colspan="8" class="empty">沒有符合條件的比賽。</td></tr>`; return; }
  body.innerHTML = rows.map(r=>{
    const d = new Date(r.game_date).toLocaleDateString("zh-TW",{timeZone:"Asia/Taipei",month:"numeric",day:"numeric"});
    const pickAbbr = r.pred_winner==="home"?r.home_abbr:r.away_abbr;
    const pHome=r.p_home_win, pShown=r.pred_winner==="home"?pHome:1-pHome;
    const ok = r.winner_correct===1;
    return `<tr>
      <td>${d}</td>
      <td>${r.away_abbr} @ ${r.home_abbr}</td>
      <td class="num">${r.pred_away_score}–${r.pred_home_score}</td>
      <td class="num">${r.actual_away_score}–${r.actual_home_score}</td>
      <td>${pickAbbr}</td>
      <td class="${ok?'ok':'no'}">${ok?'✓ 命中':'✗ 未中'}</td>
      <td class="num">${(pShown*100).toFixed(0)}%</td>
      <td class="num">${r.total_abs_error.toFixed(0)}</td>
    </tr>`;
  }).join("");
}
$("#filters").addEventListener("click", e=>{
  if(e.target.tagName!=="BUTTON") return;
  filter = e.target.dataset.f;
  [...$("#filters").children].forEach(b=>b.classList.toggle("on", b===e.target));
  renderHist();
});
renderHist();

// ---- trend line chart (single series, no legend needed) ----
function css(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function drawTrend(){
  const svg = $("#trend"), T = DATA.trend;
  const W=720,H=220, padL=40,padR=16,padT=14,padB=28;
  const iw=W-padL-padR, ih=H-padT-padB;
  if(!T.length){ svg.innerHTML=`<text x="${W/2}" y="${H/2}" fill="${css('--muted')}" font-size="13" text-anchor="middle">尚無已結算比賽</text>`; return; }
  const x = i => padL + (T.length===1?iw/2:(i/(T.length-1))*iw);
  const y = v => padT + (1-v)*ih;   // 0..1
  const grid = css('--grid'), muted=css('--muted'), line=css('--series-1'), base=css('--baseline');
  let s = "";
  // y gridlines at 0,25,50,75,100
  [0,.25,.5,.75,1].forEach(v=>{
    s+=`<line x1="${padL}" y1="${y(v)}" x2="${W-padR}" y2="${y(v)}" stroke="${v===.5?base:grid}" stroke-width="1" ${v===.5?'stroke-dasharray="4 4"':''}/>`;
    s+=`<text x="${padL-6}" y="${y(v)+4}" fill="${muted}" font-size="10" text-anchor="end">${(v*100)|0}%</text>`;
  });
  // area-ish path
  const pts = T.map((d,i)=>`${x(i).toFixed(1)},${y(d.acc).toFixed(1)}`);
  s+=`<polyline fill="none" stroke="${line}" stroke-width="2" stroke-linejoin="round" points="${pts.join(' ')}"/>`;
  // markers + hover
  T.forEach((d,i)=>{
    s+=`<circle cx="${x(i).toFixed(1)}" cy="${y(d.acc).toFixed(1)}" r="4" fill="${line}" stroke="${css('--card')}" stroke-width="1.5"
        data-t="${d.date}｜滾動命中率 ${(d.acc*100).toFixed(0)}%"/>`;
  });
  // x labels: first, middle, last
  [0, Math.floor(T.length/2), T.length-1].forEach(i=>{
    s+=`<text x="${x(i)}" y="${H-8}" fill="${muted}" font-size="10" text-anchor="middle">${T[i].date.slice(5)}</text>`;
  });
  svg.innerHTML = s;
  // tooltip
  const tip=$("#tip");
  svg.querySelectorAll("circle").forEach(c=>{
    c.addEventListener("mousemove",e=>{ tip.textContent=c.dataset.t; tip.style.opacity=1;
      tip.style.left=(e.clientX+12)+"px"; tip.style.top=(e.clientY-10)+"px"; });
    c.addEventListener("mouseleave",()=>tip.style.opacity=0);
  });
}
drawTrend();
matchMedia("(prefers-color-scheme:dark)").addEventListener("change", drawTrend);

// ---- footer ----
$("#foot").innerHTML =
  "資料來源：sportsdataverse / wehoop（ESPN）。此為 baseline 展示模型（近況滾動特徵＋線性模型），"+
  "非投注建議。歷史區含「回填」的賽前預測（用無資料洩漏特徵重建），供回顧模型表現。";
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(build_dashboard())
