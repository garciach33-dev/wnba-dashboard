"""
球員層級資料：出賽評分 + 「本場可出賽陣容強度」。

為什麼要有這一支（治本的核心）：
  舊模型只看球隊層級的近期得分／失分滾動平均，完全不知道「今天誰不能打」。
  結果是模型的勝率分佈比市場窄（std 0.13 vs 市場 0.15）——它對強隊不夠強、
  對弱隊不夠弱。這種「溫和」不是洞見，是無知。而你一旦用「模型與市場分歧最大」
  去挑注，就會機械性地挑到冷門那一邊，因為分歧本身就是分佈太窄造成的假象。
  把陣容資訊放進去，模型才敢說重話，edge 才可能是真的。

資料來源：
  歷史 → sportsdataverse/wehoop-wnba-raw 的 wnba/game_rosters/json/{game_id}.json
         （ESPN 賽後 boxscore，含每位球員的出場時間與 DNP 原因）
  即時 → ESPN summary / core API 的 injuries 區塊（賽前傷兵名單）

一個必須誠實面對的落差：
  訓練時的「不能打」＝賽後看到他沒上場（事實）。
  預測時的「不能打」＝賽前傷兵名單說他 Out（預期）。
  兩者定義不同，Day-To-Day 更是灰色地帶。這裡用出賽機率加權處理，
  但它就是一個近似，不要假裝它不是。
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

RAW_BASE = ("https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-raw"
            "/main/wnba/game_rosters/json")
SITE_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
CORE_API = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba"
UA = {"User-Agent": "Mozilla/5.0 (compatible; wnba-dashboard/1.0)"}

# ---- 評分參數 ----
LEAGUE_GMSC_PER_MIN = 0.317   # 由 2024–2026 全聯盟實算得出
PRIOR_MINUTES = 120.0         # 收縮強度：等於「先驗地看過 120 分鐘的聯盟平均表現」
DECAY = 0.97                  # 每場對舊資料的衰減，約等於半衰期 23 場
POOL_LOOKBACK = 10            # 近 10 場出現過的人算進輪值名單
TEAM_MINUTES = 200.0          # 一隊一場 5 人 × 40 分鐘

# 傷兵狀態 → 出賽機率。Out 是確定的，Day-To-Day 是猜的，這個數字可以之後校準。
PLAY_PROB = {
    "out": 0.0, "injured reserve": 0.0, "suspension": 0.0, "suspended": 0.0,
    "day-to-day": 0.55, "game-time decision": 0.55, "questionable": 0.55,
    "doubtful": 0.2, "probable": 0.9, "active": 1.0,
}

ESPN_TEAM_ID = {
    "3": "DAL", "5": "IND", "6": "LA", "8": "MIN", "9": "NY", "11": "PHX",
    "14": "SEA", "16": "WSH", "17": "LV", "18": "CON", "19": "CHI", "20": "ATL",
    "129689": "GS", "131935": "TOR", "132052": "POR", "133383": "SPO",
    "133384": "COOP",
}
ABBR_TO_ESPN_ID = {v: k for k, v in ESPN_TEAM_ID.items()}

PLAYER_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_games (
    game_id   TEXT NOT NULL,
    player_id TEXT NOT NULL,
    game_date TEXT NOT NULL,
    season    INTEGER,
    team_abbr TEXT,
    name      TEXT,
    starter   INTEGER,
    dnp       INTEGER,
    reason    TEXT,
    minutes   REAL,
    points    REAL,
    gmsc      REAL,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pg_date ON player_games(game_date);
CREATE INDEX IF NOT EXISTS idx_pg_team ON player_games(team_abbr);

CREATE TABLE IF NOT EXISTS team_strength (
    game_id        TEXT PRIMARY KEY,
    game_date      TEXT,
    home_strength  REAL,
    away_strength  REAL,
    home_missshare REAL,
    away_missshare REAL,
    computed_at    TEXT
);
"""


# =====================================================================
# 一、抓取與解析
# =====================================================================
def _get(url: str, timeout: int = 30, cap: int = 8_000_000):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(cap))


def _num(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _pair(s) -> tuple[float, float]:
    if not s or "-" not in str(s):
        return 0.0, 0.0
    a, b = str(s).split("-", 1)
    return _num(a), _num(b)


def _minutes(s) -> float:
    if not s:
        return 0.0
    s = str(s)
    if ":" in s:
        m, sec = s.split(":", 1)
        return _num(m) + _num(sec) / 60.0
    return _num(s)


def game_score(pts, fgm, fga, ftm, fta, oreb, dreb, ast, stl, blk, pf, tov) -> float:
    """Hollinger 的 Game Score：單場箱形分數的綜合產出，約略對齊得分尺度。"""
    return (pts + 0.4 * fgm - 0.7 * fga - 0.4 * (fta - ftm)
            + 0.7 * oreb + 0.3 * dreb + stl + 0.7 * ast + 0.7 * blk
            - 0.4 * pf - tov)


def parse_roster_json(doc: dict) -> list[dict]:
    """把一場 game_rosters JSON 拆成每位球員一列。只處理已結束的比賽。"""
    header = doc.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    state = ((comp.get("status") or {}).get("type") or {}).get("state")
    if state != "post":
        return []
    # 明星賽不算數：沒有防守、陣容是拼湊的，會把球員評分整個灌水。
    # ESPN 沒有給季別旗標（All-Star 一樣是 type=2），只能認 gameNote。
    if "all-star" in str(header.get("gameNote") or "").lower():
        return []
    gid = str(header.get("id") or "")
    date = comp.get("date")
    season = (header.get("season") or {}).get("year")

    rows: list[dict] = []
    for team in (doc.get("boxscore") or {}).get("players", []):
        abbr = (team.get("team") or {}).get("abbreviation")
        stats = team.get("statistics") or []
        if not stats:
            continue
        block = stats[0]
        idx = {k: i for i, k in enumerate(block.get("keys") or [])}

        for entry in block.get("athletes", []):
            ath = entry.get("athlete") or {}
            vals = entry.get("stats") or []

            def g(key):
                i = idx.get(key)
                return vals[i] if (i is not None and i < len(vals)) else None

            fgm, fga = _pair(g("fieldGoalsMade-fieldGoalsAttempted"))
            ftm, fta = _pair(g("freeThrowsMade-freeThrowsAttempted"))
            rows.append({
                "game_id": gid, "player_id": str(ath.get("id")),
                "game_date": date, "season": season, "team_abbr": abbr,
                "name": ath.get("displayName"),
                "starter": 1 if entry.get("starter") else 0,
                "dnp": 1 if entry.get("didNotPlay") else 0,
                "reason": entry.get("reason") or "",
                "minutes": _minutes(g("minutes")),
                "points": _num(g("points")),
                "gmsc": game_score(
                    _num(g("points")), fgm, fga, ftm, fta,
                    _num(g("offensiveRebounds")), _num(g("defensiveRebounds")),
                    _num(g("assists")), _num(g("steals")), _num(g("blocks")),
                    _num(g("fouls")), _num(g("turnovers")),
                ),
            })
    return rows


def history_game_ids(seasons: list[int]) -> list[str]:
    """從賽程 parquet 取得指定賽季的所有 game_id，用來回補往年的球員資料。"""
    try:
        from fetch import load_games
        df = load_games(seasons)
        return [str(g) for g in df["game_id"].tolist()]
    except Exception:
        return []


def sync_player_games(conn: sqlite3.Connection, limit: int = 300,
                      history_seasons: list[int] | None = None) -> dict:
    """
    把還沒抓過球員資料的比賽逐場補上。每天通常只有幾場，
    但第一次回補有好幾百場——所以預設一次最多 limit 場，
    剩下的下一次排程再繼續，避免單次跑太久或打爆上游。
    404 代表上游還沒發布這場，下次再試。
    """
    conn.executescript(PLAYER_SCHEMA)
    have = {r[0] for r in conn.execute("SELECT DISTINCT game_id FROM player_games")}
    todo = [r[0] for r in conn.execute(
        "SELECT game_id FROM predictions WHERE status='final' ORDER BY game_date DESC"
    ) if r[0] not in have]
    if history_seasons:
        seen = set(todo) | have
        for gid in history_game_ids(history_seasons):
            if gid not in seen:
                todo.append(gid)
                seen.add(gid)
    todo = todo[:max(0, limit)]

    added = missing = failed = 0
    for gid in todo:
        try:
            doc = _get(f"{RAW_BASE}/{gid}.json")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing += 1      # 上游還沒發布這場，下次排程再試
            else:
                failed += 1
            continue
        except Exception:
            failed += 1
            continue
        rows = parse_roster_json(doc)
        if not rows:
            missing += 1
            continue
        conn.executemany(
            """INSERT OR REPLACE INTO player_games
               (game_id,player_id,game_date,season,team_abbr,name,starter,dnp,
                reason,minutes,points,gmsc)
               VALUES (:game_id,:player_id,:game_date,:season,:team_abbr,:name,
                       :starter,:dnp,:reason,:minutes,:points,:gmsc)""", rows)
        added += 1
    conn.commit()
    return {"added": added, "not_published": missing, "failed": failed,
            "total_games": len(have) + added}


# =====================================================================
# 二、球員評分與陣容強度
# =====================================================================
class RatingBook:
    """
    依時間順序累積每位球員的每分鐘產出，任何時刻的狀態都只反映「之前」的比賽。
    這是防洩漏的關鍵：更新一定發生在該場特徵算完之後。
    """

    def __init__(self):
        self._mins: dict[str, float] = defaultdict(float)
        self._gmsc: dict[str, float] = defaultdict(float)
        self._recent: dict[str, list[float]] = defaultdict(list)
        self._pool: dict[str, list[set[str]]] = defaultdict(list)
        self.names: dict[str, str] = {}
        self.last_date: str | None = None

    # ---- 查詢 ----
    def rate(self, pid: str) -> float:
        """每分鐘 Game Score，往聯盟平均收縮；樣本越少越靠近平均。"""
        return ((self._gmsc[pid] + PRIOR_MINUTES * LEAGUE_GMSC_PER_MIN)
                / (self._mins[pid] + PRIOR_MINUTES))

    def proj_minutes(self, pid: str) -> float:
        h = self._recent[pid][-POOL_LOOKBACK:]
        return sum(h) / len(h) if h else 0.0

    def pool(self, team: str) -> set[str]:
        """近 POOL_LOOKBACK 場實際上場過的人，當作這一隊的可用輪值名單。"""
        out: set[str] = set()
        for s in self._pool[team][-POOL_LOOKBACK:]:
            out |= s
        return out

    def strength(self, team: str, play_prob: dict[str, float] | None = None
                 ) -> tuple[float | None, float]:
        """
        回傳 (可出賽陣容強度, 缺陣價值占比)。
        強度＝把 200 分鐘按「預期出場時間」重新分配給能上的人，乘各自每分鐘評分。
        少一個主力，他的分鐘會流到板凳，強度自然掉下來——這正是我們要模型看見的。
        """
        pool = self.pool(team)
        if not pool:
            return None, 0.0
        pp = play_prob or {}
        w_all = w_av = 0.0
        for pid in pool:
            m = self.proj_minutes(pid)
            w_all += m * self.rate(pid)
            w_av += m * pp.get(pid, 1.0)
        if w_av <= 0:
            return None, 1.0
        scale = TEAM_MINUTES / w_av
        strength = sum(self.proj_minutes(p) * pp.get(p, 1.0) * scale * self.rate(p)
                       for p in pool)
        w_present = sum(self.proj_minutes(p) * pp.get(p, 1.0) * self.rate(p)
                        for p in pool)
        miss_share = max(0.0, 1.0 - (w_present / w_all)) if w_all > 0 else 0.0
        return strength, miss_share

    def missing_players(self, team: str, play_prob: dict[str, float]) -> list[tuple]:
        """缺陣名單，按「損失的價值」排序——給前端顯示用。"""
        out = []
        for pid in self.pool(team):
            p = play_prob.get(pid, 1.0)
            if p >= 0.95:
                continue
            loss = self.proj_minutes(pid) * self.rate(pid) * (1 - p)
            out.append((self.names.get(pid, pid), round(self.proj_minutes(pid), 1),
                        round(p, 2), round(loss, 2)))
        return sorted(out, key=lambda x: -x[3])

    # ---- 更新 ----
    def observe(self, rows: list[sqlite3.Row]):
        """吃掉一場比賽的所有球員列，更新狀態。務必在算完該場特徵之後才呼叫。"""
        by_team: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            pid = str(r["player_id"])
            self.names[pid] = r["name"] or pid
            if (r["minutes"] or 0) > 0:
                self._mins[pid] = self._mins[pid] * DECAY + r["minutes"]
                self._gmsc[pid] = self._gmsc[pid] * DECAY + (r["gmsc"] or 0.0)
                self._recent[pid].append(r["minutes"])
                by_team[r["team_abbr"]].add(pid)
        for team, played in by_team.items():
            self._pool[team].append(played)
        if rows:
            self.last_date = rows[0]["game_date"]


def build_history(conn: sqlite3.Connection) -> int:
    """
    依時間順序走過所有已結束比賽，算出每場的雙方陣容強度並寫入 team_strength。
    歷史上的「不能打」直接用事實：他沒有出現在該場 boxscore 的上場名單裡。
    """
    conn.executescript(PLAYER_SCHEMA)
    rows = conn.execute(
        "SELECT * FROM player_games ORDER BY game_date, game_id"
    ).fetchall()
    meta = {r["game_id"]: r for r in conn.execute(
        "SELECT game_id, game_date, home_abbr, away_abbr FROM predictions")}

    by_game: dict[str, list] = defaultdict(list)
    order: list[str] = []
    for r in rows:
        if r["game_id"] not in by_game:
            order.append(r["game_id"])
        by_game[r["game_id"]].append(r)

    book = RatingBook()
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for gid in order:
        grp = by_game[gid]
        m = meta.get(gid)
        if m:
            appeared = {str(r["player_id"]) for r in grp if (r["minutes"] or 0) > 0}
            out = {}
            for team in (m["home_abbr"], m["away_abbr"]):
                for pid in book.pool(team):
                    if pid not in appeared:
                        out[pid] = 0.0
            hs, hm = book.strength(m["home_abbr"], out)
            as_, am = book.strength(m["away_abbr"], out)
            if hs is not None and as_ is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO team_strength
                       (game_id,game_date,home_strength,away_strength,
                        home_missshare,away_missshare,computed_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (gid, m["game_date"], hs, as_, hm, am, now))
                written += 1
        book.observe(grp)          # ← 一定在寫完特徵之後
    conn.commit()
    return written


def current_book(conn: sqlite3.Connection) -> RatingBook:
    """把所有已結束比賽吃完，得到「截至今天」的評分狀態，供賽前預測使用。"""
    conn.executescript(PLAYER_SCHEMA)
    rows = conn.execute(
        "SELECT * FROM player_games ORDER BY game_date, game_id").fetchall()
    book = RatingBook()
    cur, gid = [], None
    for r in rows:
        if r["game_id"] != gid:
            if cur:
                book.observe(cur)
            cur, gid = [], r["game_id"]
        cur.append(r)
    if cur:
        book.observe(cur)
    return book


# =====================================================================
# 三、即時傷兵（賽前）
# =====================================================================
def _status_prob(status: str | None) -> float:
    return PLAY_PROB.get(str(status or "").strip().lower(), 1.0)


def injuries_from_summary(event_id: str) -> dict[str, dict[str, float]]:
    """
    從 ESPN summary 抓單場雙方的傷兵名單。
    回傳 {隊伍縮寫: {球員id: 出賽機率}}；抓不到就回空 dict（呼叫端要能接受）。
    """
    try:
        doc = _get(f"{SITE_API}/summary?event={event_id}")
    except Exception:
        return {}
    out: dict[str, dict[str, float]] = {}
    for block in doc.get("injuries") or []:
        team = (block.get("team") or {}).get("abbreviation")
        if not team:
            continue
        d = out.setdefault(team, {})
        for item in block.get("injuries") or []:
            pid = str(((item.get("athlete") or {}).get("id")) or "")
            if pid:
                d[pid] = _status_prob(item.get("status"))
    return out


def injuries_from_core(team_abbr: str) -> dict[str, float]:
    """備援：core API 的球隊傷兵清單。項目是 $ref，要再追一層。"""
    tid = ABBR_TO_ESPN_ID.get(team_abbr)
    if not tid:
        return {}
    try:
        idx = _get(f"{CORE_API}/teams/{tid}/injuries")
    except Exception:
        return {}
    out: dict[str, float] = {}
    for item in (idx.get("items") or [])[:40]:
        try:
            if "$ref" in item and len(item) <= 2:
                item = _get(item["$ref"].replace("http://", "https://"))
            ath = item.get("athlete") or {}
            pid = str(ath.get("id") or "")
            if not pid and "$ref" in ath:
                pid = ath["$ref"].rstrip("/").split("/")[-1].split("?")[0]
            if pid:
                out[pid] = _status_prob(item.get("status"))
        except Exception:
            continue
    return out


def live_play_prob(event_id: str, home_abbr: str, away_abbr: str) -> dict[str, float]:
    """
    賽前這一場的球員出賽機率表。先試 summary（一次拿兩隊），
    缺哪一隊就用 core API 補。兩個都失敗就回空表——
    此時強度會退化成「全員健康」，等同於沒有這個特徵，不會亂猜。
    """
    per_team = injuries_from_summary(event_id)
    merged: dict[str, float] = {}
    for team in (home_abbr, away_abbr):
        d = per_team.get(team)
        if not d:
            d = injuries_from_core(team)
        merged.update(d)
    return merged
