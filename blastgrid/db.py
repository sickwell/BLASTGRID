import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".blastgrid" / "blastgrid.db"
GRAVEYARD = Path.home() / ".blastgrid" / "graveyard"
VAULT = Path.home() / ".blastgrid" / "vault"
LOG_PATH = Path.home() / ".blastgrid" / "usage.log"
WATCH_CONF = Path.home() / ".blastgrid" / "watch.conf"
CONTEXT_BUDGET = 180_000
WATCH_AGENT_ID = "watch"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotation


@dataclass
class SkillRecord:
    name: str
    path: str
    agent: str = ""
    size_bytes: int = 0
    token_count: int = 0
    description: str = ""
    first_seen: str = ""
    last_seen: Optional[str] = None
    last_used: Optional[str] = None
    use_count: int = 0
    tag: Optional[str] = None
    content_hash: Optional[str] = None


@dataclass
class GridStats:
    total: int = 0
    used: int = 0
    unused: int = 0
    tagged_keep: int = 0
    tagged_remove: int = 0
    total_tokens: int = 0
    budget_pct: float = 0.0


@dataclass
class AgentStats:
    agent: str
    count: int
    tokens: int


@dataclass
class UsageTier:
    label: str
    count: int
    tokens: int


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SkillDB:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS skills (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                path         TEXT NOT NULL,
                agent        TEXT NOT NULL DEFAULT '',
                size_bytes   INTEGER DEFAULT 0,
                token_count  INTEGER DEFAULT 0,
                description  TEXT DEFAULT '',
                first_seen   TEXT NOT NULL,
                last_seen    TEXT,
                last_used    TEXT,
                use_count    INTEGER DEFAULT 0,
                tag          TEXT CHECK(tag IN ('keep','remove','secure') OR tag IS NULL),
                content_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id   TEXT NOT NULL,
                ts         TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'manual',
                FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ulog_skill ON usage_log(skill_id);
            CREATE INDEX IF NOT EXISTS idx_ulog_ts    ON usage_log(ts);
            CREATE INDEX IF NOT EXISTS idx_sk_tag     ON skills(tag);
            CREATE INDEX IF NOT EXISTS idx_sk_uses    ON skills(use_count);
            CREATE INDEX IF NOT EXISTS idx_sk_tokens  ON skills(token_count);
            CREATE INDEX IF NOT EXISTS idx_sk_agent   ON skills(agent);
            CREATE INDEX IF NOT EXISTS idx_sk_name    ON skills(name);
        """)
        # Probe whether the CHECK constraint allows 'secure'.
        # We insert a dummy row, then immediately delete it.
        # If the old CHECK rejects 'secure', we recreate the table.
        needs_recreate = False
        try:
            self.conn.execute(
                "INSERT INTO skills (id, name, path, agent, first_seen, tag) "
                "VALUES ('__probe__','__probe__','__probe__','__probe__','__probe__','secure')"
            )
            self.conn.execute("DELETE FROM skills WHERE id='__probe__'")
            self.conn.commit()
        except sqlite3.IntegrityError:
            needs_recreate = True

        if needs_recreate:
            self.conn.executescript("""
                ALTER TABLE skills RENAME TO _skills_old;
                CREATE TABLE skills (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    path         TEXT NOT NULL,
                    agent        TEXT NOT NULL DEFAULT '',
                    size_bytes   INTEGER DEFAULT 0,
                    token_count  INTEGER DEFAULT 0,
                    description  TEXT DEFAULT '',
                    first_seen   TEXT NOT NULL,
                    last_seen    TEXT,
                    last_used    TEXT,
                    use_count    INTEGER DEFAULT 0,
                    tag          TEXT CHECK(tag IN ('keep','remove','secure') OR tag IS NULL),
                    content_hash TEXT
                );
                INSERT INTO skills SELECT * FROM _skills_old;
                DROP TABLE _skills_old;
            """)

    @staticmethod
    def skill_id(agent: str, name: str) -> str:
        return f"{agent}:{name}"

    def upsert_skills(self, skills: list[SkillRecord]):
        now = _now()
        self.conn.executemany("""
            INSERT INTO skills
                (id, name, path, agent, size_bytes, token_count, description,
                 first_seen, last_seen, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path, size_bytes=excluded.size_bytes,
                token_count=excluded.token_count, description=excluded.description,
                last_seen=excluded.last_seen, content_hash=excluded.content_hash
        """, [
            (self.skill_id(s.agent, s.name), s.name, s.path, s.agent,
             s.size_bytes, s.token_count, s.description, now, now, s.content_hash)
            for s in skills
        ])
        self.conn.commit()

    def tag_skill(self, skill_id: str, tag: Optional[str]):
        self.conn.execute("UPDATE skills SET tag=? WHERE id=?", (tag, skill_id))
        self.conn.commit()

    def log_usage(self, skill_id: str, source: str = "daemon"):
        now = _now()
        self.conn.execute(
            "INSERT INTO usage_log(skill_id,ts,source) VALUES(?,?,?)",
            (skill_id, now, source),
        )
        self.conn.execute(
            "UPDATE skills SET use_count=use_count+1, last_used=? WHERE id=?",
            (now, skill_id),
        )
        self.conn.commit()

    def reset_session(self):
        """Reset all usage counters for a fresh daemon session."""
        self.conn.execute("UPDATE skills SET use_count=0, last_used=NULL")
        self.conn.execute("DELETE FROM usage_log")
        self.conn.commit()

    def delete_skill(self, skill_id: str):
        self.conn.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        self.conn.commit()

    def get_all(
        self,
        sort_by: str = "name",
        desc: bool = False,
        tag_filter: Optional[str] = None,
        search: str = "",
        agent_filter: str = "",
    ) -> list[SkillRecord]:
        allowed = {"name", "size_bytes", "token_count", "use_count", "last_used", "tag", "agent"}
        col = sort_by if sort_by in allowed else "name"
        order = "DESC" if desc else "ASC"

        clauses: list[str] = []
        params: list[str] = []
        if tag_filter == "untagged":
            clauses.append("tag IS NULL")
        elif tag_filter:
            clauses.append("tag=?")
            params.append(tag_filter)
        if search:
            clauses.append("name LIKE ?")
            params.append(f"%{search}%")
        if agent_filter:
            clauses.append("agent=?")
            params.append(agent_filter)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM skills{where} ORDER BY {col} {order}", params
        ).fetchall()
        return [self._rec(r) for r in rows]

    def get_by_id(self, skill_id: str) -> Optional[SkillRecord]:
        r = self.conn.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        return self._rec(r) if r else None

    def get_stats(self, agent_filter: str = "") -> GridStats:
        where = " WHERE agent=?" if agent_filter else ""
        params = [agent_filter] if agent_filter else []
        r = self.conn.execute(f"""
            SELECT
                COUNT(*)                                           AS total,
                COALESCE(SUM(CASE WHEN use_count>0 THEN 1 END),0) AS used,
                COALESCE(SUM(CASE WHEN use_count=0 THEN 1 END),0) AS unused,
                COALESCE(SUM(CASE WHEN tag='keep'   THEN 1 END),0) AS tkeep,
                COALESCE(SUM(CASE WHEN tag='remove' THEN 1 END),0) AS tremove,
                COALESCE(SUM(token_count),0)                       AS ttokens
            FROM skills{where}
        """, params).fetchone()
        s = GridStats(
            total=r["total"], used=r["used"], unused=r["unused"],
            tagged_keep=r["tkeep"], tagged_remove=r["tremove"],
            total_tokens=r["ttokens"],
        )
        s.budget_pct = min(100.0, s.total_tokens / CONTEXT_BUDGET * 100) if CONTEXT_BUDGET else 0
        return s

    def get_agent_breakdown(self) -> list[AgentStats]:
        rows = self.conn.execute("""
            SELECT agent, COUNT(*) AS cnt, COALESCE(SUM(token_count),0) AS tkn
            FROM skills GROUP BY agent ORDER BY tkn DESC
        """).fetchall()
        return [AgentStats(agent=r["agent"], count=r["cnt"], tokens=r["tkn"]) for r in rows]

    def get_ghosts(self, agent_filter: str = "") -> list[SkillRecord]:
        where_extra = " AND agent=?" if agent_filter else ""
        params = [agent_filter] if agent_filter else []
        rows = self.conn.execute(f"""
            SELECT * FROM skills
            WHERE use_count=0 AND (tag IS NULL OR tag NOT IN ('keep','secure')){where_extra}
            ORDER BY token_count DESC
        """, params).fetchall()
        return [self._rec(r) for r in rows]

    def get_top_used(self, limit: int = 10) -> list[SkillRecord]:
        rows = self.conn.execute(
            "SELECT * FROM skills WHERE use_count>0 ORDER BY use_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._rec(r) for r in rows]

    def get_top_by_tokens(self, limit: int = 10) -> list[SkillRecord]:
        rows = self.conn.execute(
            "SELECT * FROM skills ORDER BY token_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._rec(r) for r in rows]

    def get_usage_tiers(self, agent_filter: str = "") -> list[UsageTier]:
        where = " WHERE agent=?" if agent_filter else ""
        params = [agent_filter] if agent_filter else []
        rows = self.conn.execute(f"""
            SELECT
                CASE
                    WHEN use_count >= 5 THEN 'heavy'
                    WHEN use_count >= 2 THEN 'used'
                    WHEN use_count  = 1 THEN 'once'
                    ELSE 'ghost'
                END AS tier,
                COUNT(*) AS cnt,
                COALESCE(SUM(token_count), 0) AS tkn
            FROM skills{where}
            GROUP BY tier
            ORDER BY CASE tier
                WHEN 'heavy' THEN 1 WHEN 'used' THEN 2
                WHEN 'once' THEN 3 ELSE 4
            END
        """, params).fetchall()
        return [UsageTier(label=r["tier"], count=r["cnt"], tokens=r["tkn"]) for r in rows]

    def get_autopwn_candidates(
        self, threshold: int = 1, agent_filter: str = ""
    ) -> list[SkillRecord]:
        clauses = ["use_count < ?", "(tag IS NULL OR tag NOT IN ('keep','secure'))"]
        params: list = [threshold]
        if agent_filter:
            clauses.append("agent=?")
            params.append(agent_filter)
        where = " WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(
            f"SELECT * FROM skills{where} ORDER BY token_count DESC", params
        ).fetchall()
        return [self._rec(r) for r in rows]

    def get_secured(self, agent_filter: str = "") -> list[SkillRecord]:
        clauses = ["tag='secure'"]
        params: list = []
        if agent_filter:
            clauses.append("agent=?")
            params.append(agent_filter)
        where = " WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(
            f"SELECT * FROM skills{where} ORDER BY name ASC", params
        ).fetchall()
        return [self._rec(r) for r in rows]

    def get_duplicates(self) -> list[tuple[str, list[SkillRecord]]]:
        rows = self.conn.execute("""
            SELECT name FROM skills GROUP BY name HAVING COUNT(DISTINCT agent) > 1
        """).fetchall()
        result = []
        for r in rows:
            dupes = self.conn.execute(
                "SELECT * FROM skills WHERE name=? ORDER BY agent", (r["name"],)
            ).fetchall()
            result.append((r["name"], [self._rec(d) for d in dupes]))
        return result

    def _rec(self, row) -> SkillRecord:
        d = {k: row[k] for k in row.keys()}
        d.pop("id", None)
        return SkillRecord(**d)

    def close(self):
        self.conn.close()
