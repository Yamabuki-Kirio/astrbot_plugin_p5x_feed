"""Selection and persistent delivery state; independent of AstrBot."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
import html
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
    images: list[str] = field(default_factory=list)

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


def clean_excerpt(value: str, limit: int, multiline: bool = False) -> str:
    text = html.unescape(str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    if multiline:
        text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def post_sections(p: Post, i: int, total: int, cfg: dict) -> list[tuple[str, str]]:
    origin = "贴吧" if p.source == "tieba" else "小黑盒"
    label = " · 精华" if p.source == "tieba" and p.good else ""
    parts = []
    if cfg.get("include_title_author", True):
        date = datetime.fromtimestamp(p.created, CST).strftime("%Y-%m-%d %H:%M") if p.created else "日期未知"
        parts.append(("info", f"📌 {i}/{total} · {origin}{label}\n📖 标题：{clean_excerpt(p.title, 140) or '（无标题）'}\n👤 作者：{clean_excerpt(p.author, 40) or '未知作者'}\n🕒 发布：{date}\n👍 点赞：{p.likes if p.likes is not None else '未知'}  ·  💬 回复：{p.comments}"))
    summary = clean_excerpt(p.summary, int(cfg.get("summary_length", 500)), True)
    if cfg.get("include_summary", True) and summary and summary != clean_excerpt(p.title, 140):
        parts.append(("summary", "📝 正文摘录：\n" + summary))
    if cfg.get("include_links", True):
        parts.append(("link", "🌐 原始链接：\n" + p.url))
    return parts


def post_text(p: Post, i: int, total: int, cfg: dict | None = None) -> str:
    return "\n\n".join(text for _, text in post_sections(p, i, total, cfg or {}))


def digest(posts: list[Post], heading: str = "P5X 帖子精选", cfg: dict | None = None) -> str:
    cfg = cfg or {}
    lines = [f"🃏 {heading}\n本次收录 {len(posts)} 篇"] if cfg.get("include_overview", True) else []
    lines += [text for i, p in enumerate(posts, 1) if (text := post_text(p, i, len(posts), cfg))]
    return "\n\n────────────\n\n".join(lines)


def format_preview(posts: list[Post], cfg: dict, heading: str = "P5X 帖子精选") -> str:
    if cfg.get("send_mode", "forward") == "text":
        return digest(posts, heading, cfg) or "（当前设置没有可发送的文字）"
    nodes = [f"🃏 {heading}\n本次收录 {len(posts)} 篇"] if cfg.get("include_overview", True) else []
    for i, p in enumerate(posts, 1):
        sections = post_sections(p, i, len(posts), cfg)
        pictures = f"[配图占位：最多 {min(len(p.images), int(cfg.get('max_images_per_post', 1)))} 张，实际发送以下载成功为准]" if cfg.get("include_images", True) and p.images else ""
        if cfg.get("include_images", True):
            pictures = "[帖子信息卡片：本地生成 1 张]" + ("\n" + pictures if pictures else "")
        if cfg.get("forward_layout", "compact") == "sectioned":
            nodes += [text for kind, text in sections if kind != "link"]
            if pictures: nodes.append(pictures)
            nodes += [text for kind, text in sections if kind == "link"]
        else:
            text = "\n\n".join([text for _, text in sections] + ([pictures] if pictures else []))
            if text: nodes.append(text)
    return "\n\n".join(f"【节点 {i} · 怪盗情报站】\n{text}" for i, text in enumerate(nodes, 1)) or "（当前设置没有可发送的节点）"


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
        if "layout" not in {r[1] for r in self.db.execute("PRAGMA table_info(history)")}:
            self.db.execute("ALTER TABLE history ADD COLUMN layout TEXT NOT NULL DEFAULT '{}'")
        # A crash after claiming a send cannot prove whether OneBot accepted it.
        with self.db:
            self.db.execute("UPDATE runs SET state='unknown' WHERE state='sending'")
            self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
            self.db.execute("UPDATE history SET state='unknown' WHERE state='sending'")

    def prune_history(self, now: int):
        with self.db:
            self.db.execute("DELETE FROM history WHERE created<?", (now - 7 * 86400,))

    def record_history(self, identity: str, session: str, posts: list[Post], trigger: str, now: int,
                       message: str | None = None, layout: dict | None = None) -> bool:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO history(id,session,created,trigger,state,posts,message,layout) VALUES(?,?,?,?,'sending',?,?,?)",
                (identity, session, now, trigger, json.dumps([asdict(p) for p in posts], ensure_ascii=False),
                 message if message is not None else digest(posts, "P5X 帖子分享" if trigger == "manual" else "P5X 帖子精选"),
                 json.dumps(layout or {}, ensure_ascii=False)))
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
