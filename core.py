"""Selection and persistent delivery state; independent of AstrBot."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


def clock(value: str) -> str:
    value = value.removeprefix("每天").strip()
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间请写成 20:00 或 每天20:00（北京时间）。")
    h, m = value.split(":")
    return f"{int(h):02d}:{m}"


@dataclass
class Post:
    source: str
    id: str
    title: str
    author: str
    summary: str
    url: str
    created: int
    likes: int | None
    comments: int
    good: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


def qualifies(p: Post, cfg: dict, now: int) -> bool:
    if not p.title or not p.url:
        return False
    if any(str(w).strip().casefold() in (p.title + " " + p.summary).casefold()
           for w in cfg.get("blocked_words", []) if str(w).strip()):
        return False
    if p.source == "tieba" and p.good:
        return True
    prefix = "tieba" if p.source == "tieba" else "heybox"
    age = now - p.created
    return (p.created > 0 and 0 <= age <= int(cfg.get(prefix + "_days", 7 if prefix == "tieba" else 14)) * 86400
            and p.likes is not None
            and p.likes >= int(cfg.get(prefix + "_likes", 50 if prefix == "tieba" else 100)))


def digest(posts: list[Post], heading: str = "P5X 帖子精选") -> str:
    lines = [heading]
    for i, p in enumerate(posts, 1):
        label = "贴吧精华" if p.source == "tieba" and p.good else ("贴吧热门" if p.source == "tieba" else "小黑盒热门")
        title = " ".join(p.title.split())[:140]
        author = " ".join(p.author.split())[:40] or "未知作者"
        summary = " ".join(p.summary.split())[:180]
        date = datetime.fromtimestamp(p.created, CST).strftime("%m-%d") if p.created else "日期未知"
        lines += [f"\n{i}.【{label}】{title}", f"作者：{author} · {date}",
                  f"赞 {p.likes if p.likes is not None else '未知'} · 回复 {p.comments}"]
        if summary:
            lines.append(summary)
        lines.append(p.url)
    return "\n".join(lines)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS posts(
            key TEXT PRIMARY KEY, data TEXT NOT NULL, qualified INTEGER,
            qualified_at INTEGER, seen INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS sources(
            name TEXT PRIMARY KEY, ready INTEGER NOT NULL DEFAULT 0,
            checked INTEGER, success INTEGER, error TEXT);
          CREATE TABLE IF NOT EXISTS subscriptions(
            session TEXT PRIMARY KEY, time TEXT NOT NULL, created INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries(
            session TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(session,key));
          CREATE TABLE IF NOT EXISTS runs(
            session TEXT NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(session,day));
          CREATE TABLE IF NOT EXISTS history(
            id TEXT PRIMARY KEY, session TEXT NOT NULL, created INTEGER NOT NULL,
            trigger TEXT NOT NULL, state TEXT NOT NULL, posts TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '');
        """)
        if "message" not in {r[1] for r in self.db.execute("PRAGMA table_info(history)")}:
            self.db.execute("ALTER TABLE history ADD COLUMN message TEXT NOT NULL DEFAULT ''")
        # A crash after claiming a send cannot prove whether OneBot accepted it.
        with self.db:
            self.db.execute("UPDATE runs SET state='unknown' WHERE state='sending'")
            self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
            self.db.execute("UPDATE history SET state='unknown' WHERE state='sending'")

    def prune_history(self, now: int):
        with self.db:
            self.db.execute("DELETE FROM history WHERE created<?", (now - 7 * 86400,))

    def record_history(self, identity: str, session: str, posts: list[Post], trigger: str, now: int) -> bool:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO history(id,session,created,trigger,state,posts,message) VALUES(?,?,?,?,'sending',?,?)",
                (identity, session, now, trigger, json.dumps([asdict(p) for p in posts], ensure_ascii=False),
                 digest(posts, "P5X 帖子分享" if trigger == "manual" else "P5X 帖子精选")))
            if cur.rowcount and trigger == "manual":
                self.db.executemany("INSERT OR IGNORE INTO deliveries VALUES(?,?,'sending')", [(session, p.key) for p in posts])
            return bool(cur.rowcount)

    def history_result(self, identity: str, session: str, posts: list[Post], state: str):
        with self.db:
            self.db.execute("UPDATE history SET state=? WHERE id=?", (state, identity))
            self.db.executemany("UPDATE deliveries SET state=? WHERE session=? AND key=? AND state='sending'", [(state, session, p.key) for p in posts])

    def ingest(self, source: str, posts: list[Post], cfg: dict, now: int):
        row = self.db.execute("SELECT ready FROM sources WHERE name=?", (source,)).fetchone()
        baseline = not row or not row[0]
        with self.db:
            for p in posts:
                old = self.db.execute("SELECT qualified,qualified_at FROM posts WHERE key=?", (p.key,)).fetchone()
                hit = qualifies(p, cfg, now)
                # Historical qualifying posts remain suppressed, including after
                # a transient drop below the threshold and later recovery.
                when = old[1] if old else None
                if hit and when is None:
                    when = 0 if baseline else now
                self.db.execute("INSERT OR REPLACE INTO posts VALUES(?,?,?,?,?)",
                                (p.key, json.dumps(asdict(p), ensure_ascii=False), int(hit), when, now))
            self.db.execute("INSERT INTO sources VALUES(?,1,?,?,NULL) ON CONFLICT(name) DO UPDATE SET ready=1,checked=excluded.checked,success=excluded.success,error=NULL", (source, now, now))

    def failure(self, source: str, now: int, error: str):
        with self.db:
            self.db.execute("INSERT INTO sources VALUES(?,0,?,NULL,?) ON CONFLICT(name) DO UPDATE SET checked=excluded.checked,error=excluded.error", (source, now, error[:200]))

    def subscribe(self, session: str, time: str, now: int):
        with self.db:
            self.db.execute("INSERT INTO subscriptions VALUES(?,?,?) ON CONFLICT(session) DO UPDATE SET time=excluded.time", (session, clock(time), now))

    def unsubscribe(self, session: str):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE session=?", (session,))

    def subscriptions(self):
        return self.db.execute("SELECT * FROM subscriptions").fetchall()

    def candidates(self, cfg: dict, now: int, session: str | None = None) -> list[Post]:
        rows = self.db.execute("SELECT * FROM posts WHERE qualified=1").fetchall()
        sub = self.db.execute("SELECT created FROM subscriptions WHERE session=?", (session,)).fetchone() if session else None
        result = []
        for r in rows:
            p = Post(**json.loads(r["data"]))
            if not cfg.get(p.source + "_enabled", True) or not qualifies(p, cfg, now):
                continue
            # Do not deliver stale cached data after a source outage.
            if now - r["seen"] > 86400:
                continue
            if session:
                if not sub or not r["qualified_at"] or r["qualified_at"] <= sub[0]:
                    continue
                if self.db.execute("SELECT 1 FROM deliveries WHERE session=? AND key=?", (session, p.key)).fetchone():
                    continue
            result.append(p)
        return sorted(result, key=lambda p: (p.good, p.created, p.likes or 0), reverse=True)

    def tracked_tieba(self, now: int, days: int, limit: int) -> list[str]:
        result = []
        for r in self.db.execute("SELECT data FROM posts ORDER BY seen ASC"):
            p = Post(**json.loads(r[0]))
            if p.source == "tieba" and not p.good and 0 <= now - p.created <= days * 86400:
                result.append(p.id)
        return result[:limit]

    def claim(self, session: str, day: str, posts: list[Post]) -> bool:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,'sending')", (session, day))
            if not cur.rowcount:
                return False
            self.db.executemany("INSERT INTO deliveries VALUES(?,?,'sending')", [(session, p.key) for p in posts])
        return True

    def finish(self, session: str, day: str, posts: list[Post], state: str):
        with self.db:
            self.db.execute("UPDATE runs SET state=? WHERE session=? AND day=?", (state, session, day))
            self.db.executemany("UPDATE deliveries SET state=? WHERE session=? AND key=?", [(state, session, p.key) for p in posts])

    def close(self):
        self.db.close()
