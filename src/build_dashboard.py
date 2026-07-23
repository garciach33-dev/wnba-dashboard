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

import json
from datetime import datetime, timezone
from pathlib import Path

from db import connect

OUT_PATH = Path(__file__).resolve().parent.parent / "dashboard.html"


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

    return {
        "generated_at": now.isoformat(),
        "metrics": {
            "acc": acc, "total_mae": total_mae, "margin_mae": margin_mae,
            "brier": brier, "n_settled": n, "n_pending": len(pending),
        },
        "pending": pending,
        "settled": settled,
        "trend": trend,
    }


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
    </div>`;
  }).join("");
}

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
