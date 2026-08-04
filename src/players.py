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
from datetime import datetime, timedelta, timezone

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

# 交易／裁員修正：輪值名單是「近 10 場上場過的人」推出來的，這個定義追不上交易。
# 被交易走的人會賴在舊隊名單裡最多 10 場（WNBA 約兩週半），而且傷兵表查不到他，
# 會被當成健康可打 → 舊隊強度灌水；新隊則要等他真的上場才算得到他 → 低估。
# 解法是拿 ESPN 的「當下名冊」跟輪值名單取交集，再把名冊上「最近還在打球」的人補進來。
# ROSTER_ACTIVE_DAYS 是「補進來」的門檻：只認最近這麼多天在聯盟任一隊上場過的人，
# 免得把整季沒上場的深板凳算成輪值，他們的 proj_minutes 會是過期的舊資料。
ROSTER_ACTIVE_DAYS = 21

# 放棄回補的門檻。上游 wehoop 的 game_rosters 只有 2024 年之後，
# 2023 整季（兩百多場）永遠要不到；沒有這組規則就會每天白打兩輪 404。
MISS_GIVEUP_ATTEMPTS = 3      # 失敗幾次之後才考慮放棄
MISS_GIVEUP_DAYS = 14         # 比賽要過多久才算「上游不是還沒發布，是根本沒有」
MISS_RETRY_DAYS = 30          # 放棄之後每隔多久還是回頭試一次（自癒用）

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
    is_home   INTEGER,
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

-- 抓不到球員資料的比賽記在這裡，免得每次排程都去要同一批不存在的檔案。
-- 上游的 game_rosters 只有 2024 年之後，2023 整季永遠要不到；
-- 沒這張表的話那兩百多場會被無止盡地重試下去。
CREATE TABLE IF NOT EXISTS player_sync_miss (
    game_id   TEXT PRIMARY KEY,
    game_date TEXT,
    attempts  INTEGER NOT NULL DEFAULT 0,
    last_try  TEXT
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
    home_abbr = None
    for c in comp.get("competitors", []):
        if c.get("homeAway") == "home":
            home_abbr = (c.get("team") or {}).get("abbreviation")

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
                "is_home": 1 if (home_abbr and abbr == home_abbr) else 0,
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


def history_game_ids(seasons: list[int]) -> list[tuple[str, str]]:
    """
    從賽程 parquet 取得要回補球員資料的比賽 (game_id, 比賽日期)。

    只回「已完賽」的比賽：還沒打的比賽當然沒有 boxscore，去要它只會拿到 404。
    打完之後它會從另一條路徑（predictions 裡 status='final'）自然進來，
    不需要靠這裡先卡一輪失敗。
    """
    try:
        from fetch import load_games
        df = load_games(seasons)
        df = df[df["completed"]]
        return [(str(gid), d.isoformat())
                for gid, d in zip(df["game_id"], df["date"])]
    except Exception:
        return []


def _note_miss(conn: sqlite3.Connection, gid: str, gdate: str | None):
    """記一次「這場要不到」。同一場累積失敗次數，供 _giveup_ids 判斷何時放棄。"""
    conn.execute(
        """INSERT INTO player_sync_miss (game_id, game_date, attempts, last_try)
           VALUES (?,?,1,?)
           ON CONFLICT(game_id) DO UPDATE SET
             attempts = player_sync_miss.attempts + 1,
             last_try = excluded.last_try,
             game_date = COALESCE(player_sync_miss.game_date, excluded.game_date)""",
        (gid, gdate, datetime.now(timezone.utc).isoformat()))


def _giveup_ids(conn: sqlite3.Connection) -> set[str]:
    """
    這一輪要跳過的 game_id。放棄條件是三個一起成立：
      失敗過 MISS_GIVEUP_ATTEMPTS 次以上、比賽已經過了 MISS_GIVEUP_DAYS 天、
      而且最近 MISS_RETRY_DAYS 天內試過了。

    第二個條件保護「上游還沒來得及發布」的新比賽——那種要繼續重試。
    第三個條件留了自癒的後路：每 MISS_RETRY_DAYS 天還是會再試一次，
    萬一上游哪天補上 2023 的資料，系統自己會撿回來，不用改程式。
    """
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(days=MISS_GIVEUP_DAYS)).isoformat()
    recheck = (now - timedelta(days=MISS_RETRY_DAYS)).isoformat()
    return {r[0] for r in conn.execute(
        """SELECT game_id FROM player_sync_miss
           WHERE attempts >= ? AND game_date IS NOT NULL AND game_date < ?
             AND last_try > ?""",
        (MISS_GIVEUP_ATTEMPTS, stale, recheck))}


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
    todo = [(r[0], r[1]) for r in conn.execute(
        "SELECT game_id, game_date FROM predictions WHERE status='final' "
        "ORDER BY game_date DESC") if r[0] not in have]
    if history_seasons:
        seen = {g for g, _ in todo} | have
        for gid, gdate in history_game_ids(history_seasons):
            if gid not in seen:
                todo.append((gid, gdate))
                seen.add(gid)

    # 濾掉「已經放棄」的比賽（見 _giveup_ids）。這是 2023 那兩百多場的去處：
    # 上游沒有就是沒有，試三次還在 404 又已經過了兩週，就別再打了。
    skip = _giveup_ids(conn)
    todo = [t for t in todo if t[0] not in skip][:max(0, limit)]
    skipped = len(skip)

    added = missing = failed = 0
    for gid, gdate in todo:
        try:
            doc = _get(f"{RAW_BASE}/{gid}.json")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing += 1      # 上游還沒發布這場
                _note_miss(conn, gid, gdate)
            else:
                failed += 1       # 連線/伺服器問題不記次，那不是「這場不存在」
            continue
        except Exception:
            failed += 1
            continue
        rows = parse_roster_json(doc)
        if not rows:
            missing += 1
            _note_miss(conn, gid, gdate)
            continue
        conn.execute("DELETE FROM player_sync_miss WHERE game_id=?", (gid,))
        conn.executemany(
            """INSERT OR REPLACE INTO player_games
               (game_id,player_id,game_date,season,team_abbr,is_home,name,starter,
                dnp,reason,minutes,points,gmsc)
               VALUES (:game_id,:player_id,:game_date,:season,:team_abbr,:is_home,
                       :name,:starter,:dnp,:reason,:minutes,:points,:gmsc)""", rows)
        added += 1
    conn.commit()
    return {"added": added, "not_published": missing, "failed": failed,
            "skipped": skipped, "total_games": len(have) + added}


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
        self._last_played: dict[str, str] = {}   # 球員 → 最後一次上場的日期（不分球隊）
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

    def recently_active(self, pid: str, within_days: int = ROSTER_ACTIVE_DAYS) -> bool:
        """這位球員最近有沒有在聯盟裡（任何一隊）上場過。"""
        d = self._last_played.get(pid)
        if not d or not self.last_date:
            return False
        try:
            gap = (datetime.fromisoformat(self.last_date[:10])
                   - datetime.fromisoformat(d[:10])).days
        except ValueError:
            return False
        return gap <= within_days

    def pool(self, team: str, roster: set[str] | None = None) -> set[str]:
        """
        這一隊的可用輪值名單。

        預設是「近 POOL_LOOKBACK 場實際上場過的人」——歷史回填一定要用這個定義，
        因為那才是當時的事實，拿今天的名冊去描述三週前的比賽就是資料洩漏。

        賽前預測才傳 roster（ESPN 的當下名冊）進來，做兩件事：
          減：不在名冊上的人剔掉（被交易走、被裁掉的，今天不會替這隊打球）。
          加：名冊上、最近還在聯盟打過球、但還沒替這隊上場過的人補進來
              （剛交易過來的）。他的評分與預期分鐘沿用他在原隊的資料，
              這是個近似，但比「當他不存在」準得多。
        roster 是空的（抓不到）就完全退回舊行為，不會亂猜。
        """
        out: set[str] = set()
        for s in self._pool[team][-POOL_LOOKBACK:]:
            out |= s
        if not roster:
            return out
        out &= roster
        out |= {p for p in roster
                if p not in out and self._recent.get(p) and self.recently_active(p)}
        return out

    def strength(self, team: str, play_prob: dict[str, float] | None = None,
                 roster: set[str] | None = None) -> tuple[float | None, float]:
        """
        回傳 (可出賽陣容強度, 缺陣價值占比)。
        強度＝把 200 分鐘按「預期出場時間」重新分配給能上的人，乘各自每分鐘評分。
        少一個主力，他的分鐘會流到板凳，強度自然掉下來——這正是我們要模型看見的。

        roster 只有賽前預測會傳（見 pool 的說明）；歷史回填一律不傳。
        """
        pool = self.pool(team, roster)
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

    def missing_players(self, team: str, play_prob: dict[str, float],
                        roster: set[str] | None = None) -> list[tuple]:
        """缺陣名單，按「損失的價值」排序——給前端顯示用。"""
        out = []
        for pid in self.pool(team, roster):
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
                self._last_played[pid] = r["game_date"]
                by_team[r["team_abbr"]].add(pid)
        for team, played in by_team.items():
            self._pool[team].append(played)
        if rows:
            self.last_date = rows[0]["game_date"]


def build_history(conn: sqlite3.Connection) -> int:
    """
    依時間順序走過所有已結束比賽，算出每場的雙方陣容強度並寫入 team_strength。

    「不能打」的定義要跟上線時對得起來，這件事比它看起來重要。
    賽後我們看得到的是「誰沒上場」，但其中有一大類是 COACH'S DECISION——
    人是好的，只是教練沒派他。而上線時我們拿到的是傷兵名單，
    傷兵名單不會列出這種人。如果訓練時把他們算成「缺陣」，
    模型學到的缺陣尺度就會比上線時看到的大一截。
    所以這裡只把「傷病／個人因素缺席」和「根本沒進名單」算成不能打。
    實測這樣做走前準確率 65.2%，比全算成缺陣的 63.6% 還好一點。
    """
    _ensure_player_columns(conn)
    rows = conn.execute(
        "SELECT * FROM player_games ORDER BY game_date, game_id"
    ).fetchall()

    by_game: dict[str, list] = defaultdict(list)
    order: list[str] = []
    for r in rows:
        if r["game_id"] not in by_game:
            order.append(r["game_id"])
        by_game[r["game_id"]].append(r)

    now = datetime.now(timezone.utc).isoformat()
    book = RatingBook()
    written = 0
    for gid in order:
        grp = by_game[gid]
        home = away = None
        for r in grp:
            if r["is_home"]:
                home = r["team_abbr"]
            else:
                away = r["team_abbr"]
        if home and away:
            appeared = {str(r["player_id"]) for r in grp if (r["minutes"] or 0) > 0}
            coach = {str(r["player_id"]) for r in grp
                     if (r["minutes"] or 0) == 0
                     and "COACH" in str(r["reason"] or "").upper()}
            out = {pid: 0.0
                   for team in (home, away)
                   for pid in book.pool(team)
                   if pid not in appeared and pid not in coach}
            hs, hm = book.strength(home, out)
            as_, am = book.strength(away, out)
            if hs is not None and as_ is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO team_strength
                       (game_id,game_date,home_strength,away_strength,
                        home_missshare,away_missshare,computed_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (gid, grp[0]["game_date"], hs, as_, hm, am, now))
                written += 1
        book.observe(grp)          # ← 一定在寫完特徵之後
    conn.commit()
    return written


def _ensure_player_columns(conn: sqlite3.Connection):
    """就地升級：舊版 player_games 沒有 is_home，補上並從 predictions 回填。"""
    conn.executescript(PLAYER_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(player_games)")}
    if "is_home" not in have:
        conn.execute("ALTER TABLE player_games ADD COLUMN is_home INTEGER")
        conn.execute(
            """UPDATE player_games SET is_home = (
                   SELECT CASE WHEN p.home_abbr = player_games.team_abbr THEN 1 ELSE 0 END
                   FROM predictions p WHERE p.game_id = player_games.game_id)
               WHERE is_home IS NULL""")
        conn.commit()


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
        if d is None:
            d = injuries_from_core(team)
        merged.update(d)
    return merged


def _athlete_ids(node) -> set[str]:
    """
    從 ESPN 回傳的任意巢狀結構撈出球員 id。
    刻意寫成通用遞迴：ESPN 的 roster 有好幾種形狀（athletes 直接是陣列、
    或包一層 items 分位置、或掛在 team.athletes 底下），而且會改。
    與其賭其中一種，不如認「長得像球員的物件」，抓不到就回空集合。
    """
    out: set[str] = set()
    if isinstance(node, list):
        for x in node:
            out |= _athlete_ids(x)
    elif isinstance(node, dict):
        pid = node.get("id")
        # 有 id 又有姓名欄位，才當成球員；球隊物件也有 id，用姓名欄位排除掉
        if pid and ("fullName" in node or "displayName" in node) and "abbreviation" not in node:
            out.add(str(pid))
        for k in ("athletes", "items", "entries", "team", "roster"):
            if k in node:
                out |= _athlete_ids(node[k])
    return out


def current_roster(team_abbr: str) -> set[str]:
    """
    ESPN 上這一隊「當下」的名冊（球員 id 集合）。抓不到就回空集合，
    呼叫端會退回舊的「近 10 場上場過的人」定義，不會壞掉。
    """
    tid = ABBR_TO_ESPN_ID.get(team_abbr)
    if not tid:
        return set()
    try:
        doc = _get(f"{SITE_API}/teams/{tid}/roster")
    except Exception:
        try:
            doc = _get(f"{SITE_API}/teams/{tid}?enable=roster")
        except Exception:
            return set()
    ids = _athlete_ids(doc)
    # 合理性檢查：WNBA 一隊 11~12 人。少得離譜多半是解析失敗，
    # 多得離譜多半是撈到別的東西——兩種都寧可不用，別讓壞資料進模型。
    return ids if 8 <= len(ids) <= 30 else set()


def rosters_for(teams) -> dict[str, set[str]]:
    """一次抓多隊名冊並快取；同一次排程裡每隊只打一次 ESPN。"""
    out: dict[str, set[str]] = {}
    for t in dict.fromkeys(teams):
        out[t] = current_roster(t)
    return out


# =====================================================================
# 四、給特徵表用的介面
# =====================================================================
def strength_map(conn: sqlite3.Connection) -> dict[str, tuple]:
    """game_id → (主隊強度, 客隊強度, 主隊缺陣占比, 客隊缺陣占比)。"""
    conn.executescript(PLAYER_SCHEMA)
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
        """SELECT game_id, home_strength, away_strength,
                  home_missshare, away_missshare FROM team_strength""")}


def compute_upcoming(conn: sqlite3.Connection, max_games: int = 40) -> dict:
    """
    對還沒開賽的比賽算陣容強度，寫進同一張 team_strength 表。
    比賽結束後 build_history 會用事實覆蓋掉這一列。

    分兩段處理，因為兩種資訊的成本差很多：
      近 max_games 場 → 名冊 ＋ 今天的傷兵名單（每場要打一次 ESPN summary）
      再遠的比賽      → 只用名冊（名冊每隊只抓一次，額外成本是零）

    第二段是後來補的。原本遠端比賽整批吃中性填值 0，等於那些場次退回舊模型；
    但「這隊現在的輪值有多強」不需要傷兵名單也算得出來，白白丟掉太可惜。
    三週後誰會受傷本來就沒人知道，那一層缺了是誠實，不是缺陷。
    """
    conn.executescript(PLAYER_SCHEMA)
    book = current_book(conn)
    rows = conn.execute(
        """SELECT game_id, game_date, home_abbr, away_abbr FROM predictions
           WHERE status='pending' ORDER BY game_date"""
    ).fetchall()

    # 名冊每隊只抓一次（最多 15 隊），用來修正交易／裁員造成的名單落差。
    # 抓不到的隊回空集合 → 那一隊自動退回「近 10 場上場過的人」。
    try:
        rosters = rosters_for([t for r in rows for t in (r["home_abbr"], r["away_abbr"])])
    except Exception:
        rosters = {}

    now = datetime.now(timezone.utc).isoformat()
    done = injured = roster_only = 0
    near = max(0, max_games)
    for i, r in enumerate(rows):
        if i < near:
            try:
                pp = live_play_prob(r["game_id"], r["home_abbr"], r["away_abbr"])
            except Exception:
                pp = {}
            if pp:
                injured += 1
        else:
            # 遠端比賽不問傷兵：三週後的傷兵名單本來就沒有參考價值，
            # 而且每場要多打一次 ESPN。名冊已經抓好了，這裡是零成本。
            pp = {}
            roster_only += 1
        hs, hm = book.strength(r["home_abbr"], pp, rosters.get(r["home_abbr"]))
        as_, am = book.strength(r["away_abbr"], pp, rosters.get(r["away_abbr"]))
        if hs is None or as_ is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO team_strength
               (game_id,game_date,home_strength,away_strength,
                home_missshare,away_missshare,computed_at)
               VALUES (?,?,?,?,?,?,?)""",
            (r["game_id"], r["game_date"], hs, as_, hm, am, now))
        done += 1
    conn.commit()
    # roster_teams / roster_ok 是給排程日誌看的健康指標：
    # roster_ok 若長期是 0，代表 ESPN 名冊沒抓到，交易修正等於沒作用
    # （系統仍正常運作，只是退回舊行為）。這比靜靜失敗好。
    return {"upcoming": done, "with_injury_data": injured,
            "roster_only": roster_only,
            "roster_teams": len(rosters),
            "roster_ok": sum(1 for v in rosters.values() if v)}
