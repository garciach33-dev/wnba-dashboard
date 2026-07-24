"""
從 The Odds API 撈 WNBA 盤口（獨贏 h2h + 大小分 totals），算出「市場共識機率」。

免費方案就能拿「即將開賽」的盤口；申請金鑰：https://the-odds-api.com/
金鑰放環境變數 ODDS_API_KEY（GitHub 上用 repo secret，不會外洩）。

重要：
  - 「去水錢（de-vig）」= 把博彩商抽成拿掉，還原成乾淨的隱含機率。
  - 多家博彩商取中位數當共識，比單一家更穩、更接近真實機率。
  - 台灣運彩沒有 API，這裡用國際盤當『參考基準』；運彩線通常更差，
    所以這是偏寬鬆的照妖鏡：對國際盤沒 edge，對運彩幾乎一定沒有。
  - 沒有金鑰或撈取失敗時，回傳空 dict，整個系統照常運作（只是沒有市場欄位）。
"""
from __future__ import annotations

import json
import os
import statistics
import urllib.error
import urllib.request

SPORT = "basketball_wnba"
ODDS_URL = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"

# The Odds API 用球隊全名；用關鍵字對應到我們的縮寫，容忍命名差異
_TEAM_KEYWORDS = {
    "atlanta": "ATL", "dream": "ATL",
    "chicago": "CHI", "sky": "CHI",
    "connecticut": "CON", "sun": "CON",
    "dallas": "DAL", "wings": "DAL",
    "golden state": "GS", "valkyries": "GS",
    "indiana": "IND", "fever": "IND",
    "las vegas": "LV", "aces": "LV",
    "los angeles": "LA", "sparks": "LA",
    "minnesota": "MIN", "lynx": "MIN",
    "new york": "NY", "liberty": "NY",
    "phoenix": "PHX", "mercury": "PHX",
    "seattle": "SEA", "storm": "SEA",
    "washington": "WSH", "mystics": "WSH",
    "toronto": "TOR", "tempo": "TOR",
    "portland": "POR", "fire": "POR",
}


def team_to_abbr(name: str) -> str | None:
    n = (name or "").lower()
    for kw, abbr in _TEAM_KEYWORDS.items():
        if kw in n:
            return abbr
    return None


def _devig_two_way(dec_a: float, dec_b: float) -> tuple[float, float]:
    """兩邊的十進位賠率 → 去水錢後的機率（相加=1）。"""
    if not dec_a or not dec_b or dec_a <= 1 or dec_b <= 1:
        return (float("nan"), float("nan"))
    ra, rb = 1.0 / dec_a, 1.0 / dec_b
    s = ra + rb
    return (ra / s, rb / s)


def fetch_wnba_odds(api_key: str | None = None, regions: str = "us") -> list:
    """
    回傳 list，每場一筆 dict：{
        home_abbr, away_abbr, commence_time, n_books,
        market_p_home, dec_home, dec_away,        # 獨贏（market_p_home 對應 home_abbr）
        total_line, dec_over, dec_under, p_over,   # 大小分
    }
    回傳 list（不是以隊伍組合當 key 的 dict），才不會讓同兩隊的多場比賽撞號，
    且保留主客方向，之後比對時可精準對上「同一場」並校正主客。
    無金鑰或失敗 → 回傳 []。
    """
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("   [odds] 未設定 ODDS_API_KEY，略過盤口（系統照常運作）")
        return []

    params = f"?apiKey={api_key}&regions={regions}&markets=h2h,totals&oddsFormat=decimal"
    try:
        req = urllib.request.Request(ODDS_URL + params, headers={"User-Agent": "wnba-dash/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            remaining = resp.headers.get("x-requests-remaining")
            data = json.loads(resp.read())
        if remaining is not None:
            print(f"   [odds] 撈到 {len(data)} 場，本月剩餘額度 {remaining}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"   [odds] 撈取失敗（{e}），略過盤口（系統照常運作）")
        return []

    out = []
    for g in data:
        ha = team_to_abbr(g.get("home_team", ""))
        aa = team_to_abbr(g.get("away_team", ""))
        if not ha or not aa:
            continue
        home_name, away_name = g.get("home_team"), g.get("away_team")

        p_homes, dec_homes, dec_aways = [], [], []
        total_lines, dec_overs, dec_unders, p_overs = [], [], [], []
        for bk in g.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] == "h2h":
                    o = {x["name"]: x["price"] for x in mk["outcomes"]}
                    dh, da = o.get(home_name), o.get(away_name)
                    ph, _ = _devig_two_way(dh, da)
                    if ph == ph:  # not NaN
                        p_homes.append(ph); dec_homes.append(dh); dec_aways.append(da)
                elif mk["key"] == "totals":
                    over = next((x for x in mk["outcomes"] if x["name"].lower() == "over"), None)
                    under = next((x for x in mk["outcomes"] if x["name"].lower() == "under"), None)
                    if over and under and over.get("point") is not None:
                        po, _ = _devig_two_way(over["price"], under["price"])
                        if po == po:
                            total_lines.append(over["point"])
                            dec_overs.append(over["price"]); dec_unders.append(under["price"])
                            p_overs.append(po)

        rec = {"home_abbr": ha, "away_abbr": aa,
               "commence_time": g.get("commence_time"),
               "n_books": len(g.get("bookmakers", []))}
        if p_homes:
            rec.update(market_p_home=statistics.median(p_homes),
                       dec_home=statistics.median(dec_homes),
                       dec_away=statistics.median(dec_aways))
        if total_lines:
            rec.update(total_line=statistics.median(total_lines),
                       dec_over=statistics.median(dec_overs),
                       dec_under=statistics.median(dec_unders),
                       p_over=statistics.median(p_overs))
        if "market_p_home" in rec or "total_line" in rec:
            out.append(rec)
    return out


if __name__ == "__main__":
    odds = fetch_wnba_odds()
    print(f"matched {len(odds)} games")
    for v in odds[:5]:
        print(v["away_abbr"], "@", v["home_abbr"], v)
