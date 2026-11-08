"""Authenticated AstrBot Plugin View API. No independent HTTP server."""
from __future__ import annotations

import copy
import json
import time
import asyncio
import re
from dataclasses import asdict
from pathlib import Path

from .core import clock, qualifies, Post, digest, format_preview

SCHEMA = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
LIMITS = {"check_minutes": (10, 1440), "max_posts": (1, 10), "tieba_days": (1, 90),
          "heybox_days": (1, 90), "tieba_likes": (1, 100000), "heybox_likes": (1, 100000),
          "tieba_pages": (1, 10), "recheck_limit": (0, 30),
          "summary_length": (50, 2000), "max_images_per_post": (1, 3)}


def validate_config(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - set(SCHEMA):
        raise ValueError("配置包含未知字段，请刷新页面。")
    result = {}
    for key, value in body.items():
        kind = SCHEMA[key]["type"]
        if key == "forward_layout" and value not in ("compact", "sectioned"):
            raise ValueError("请选择有效的转发节点布局。")
        if key == "send_mode" and value not in ("forward", "text"):
            raise ValueError("消息样式请选择合并转发或纯文本。")
        if kind == "bool" and type(value) is not bool:
            raise ValueError(f"{SCHEMA[key]['description']}必须为开关。")
        if kind == "int":
            lo, hi = LIMITS[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{SCHEMA[key]['description']}需为 {lo}–{hi} 之间的整数。")
        if key == "tieba_forum":
            if not isinstance(value, str) or not value.strip() or len(value) > 60:
                raise ValueError("贴吧名称不能为空，且不能超过 60 个字符。")
            value = value.strip()
        if kind == "list":
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) for v in value):
                raise ValueError("列表格式不正确。")
            value = list(dict.fromkeys(v.strip() for v in value if v.strip()))
            if key == "heybox_topics" and (not value or len(value) > 20 or any(not v.isascii() or not v.isdigit() or len(v) > 32 for v in value)):
                raise ValueError("分区 ID 需为数字，每行一个，最多 20 个且不能为空。")
            if key == "blocked_words" and any(len(v) > 80 for v in value):
                raise ValueError("单个屏蔽词不能超过 80 个字符。")
        result[key] = value
    return result


class WebUI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.routes = []
        self.test_posts = {}
        self.test_at = None

    def register(self):
        for path, fn, methods in [("state", self.state, ["GET"]), ("config", self.save, ["POST"]),
                                  ("subscription", self.subscription, ["POST"]),
                                  ("crawl", self.crawl, ["POST"]), ("send", self.send, ["POST"]),
                                  ("targets", self.targets, ["GET"]), ("history", self.history, ["POST"])]:
            route = "/astrbot_plugin_p5x_feed/page/" + path
            self.plugin.context.register_web_api(route, fn, methods, "P5X 推送管理")
            self.routes.append(route)

    def unregister(self):
        # Only remove handlers owned by this exact instance, never a newer reload.
        apis = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(apis, list):
            apis[:] = [r for r in apis if not (r[0] in self.routes and getattr(r[1], "__self__", None) is self)]

    async def body(self):
        from astrbot.api.web import request
        data = await request.json(default=None)
        if not isinstance(data, dict):
            raise ValueError("需要 JSON 对象。")
        return data

    async def state(self):
        p = self.plugin
        async with p.lock:
            now = int(time.time())
            p.store.prune_history(now)
            cfg = {k: p.cfg.get(k, v["default"]) for k, v in SCHEMA.items()}
            sources = []
            for name in ("tieba", "heybox"):
                row = p.store.db.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
                sources.append({"name": name, "enabled": cfg[name + "_enabled"],
                                "success": row["success"] if row else None,
                                "error": row["error"] if row else None,
                                "ready": bool(row and row["ready"]),
                                "warning": p.sources.warnings.get(name)})
            subs = []
            for sub in p.store.subscriptions():
                latest = p.store.db.execute("SELECT day,state FROM runs WHERE session=? ORDER BY day DESC LIMIT 1", (sub["session"],)).fetchone()
                subs.append({**dict(sub), "latest": dict(latest) if latest else None,
                             "pending": len(p.store.candidates(p.config(), now, sub["session"]))})
            posts = p.store.candidates(p.config(), now)[:12]
            history = []
            for row in p.store.db.execute("SELECT * FROM history WHERE created>=? ORDER BY created DESC LIMIT 100", (now - 7 * 86400,)):
                history.append({**dict(row), "posts": json.loads(row["posts"])})
            return {"status": "ok", "data": {"config": cfg, "sources": sources, "subscriptions": subs,
                    "posts": [asdict(x) for x in posts],
                    "message_preview": format_preview(posts[:p.config()["max_posts"]], p.config()) if posts else "",
                    "stats": {"tracked": p.store.db.execute("SELECT count(*) FROM posts").fetchone()[0],
                              "qualified": len(p.store.candidates(p.config(), now)), "groups": len(subs)},
                    "history": history, "test_at": self.test_at,
                    "test_posts": [asdict(x) for x in self.test_posts.values()],
                    "server_time": now, "version": "0.3.1"}}

    def platforms(self):
        return [x for x in self.plugin.context.platform_manager.platform_insts if x.meta().name == "aiocqhttp"]

    async def history(self):
        try:
            body = await self.body()
            offset = body.get("offset", 0)
            if type(offset) is not int or not 0 <= offset <= 1000000:
                raise ValueError("页码不正确。")
            async with self.plugin.lock:
                store = self.plugin.store
                store.prune_history(int(time.time()))
                total = store.db.execute("SELECT count(*) FROM history").fetchone()[0]
                rows = [{**dict(r), "posts": json.loads(r["posts"])} for r in store.db.execute("SELECT * FROM history ORDER BY created DESC,id DESC LIMIT 25 OFFSET ?", (offset,))]
            return {"status": "ok", "data": {"rows": rows, "total": total, "offset": offset}}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def targets(self):
        targets, errors = [], []
        for platform in self.platforms():
            identity = platform.meta().id
            try:
                groups = await asyncio.wait_for(platform.bot.get_group_list(), timeout=10)
                if isinstance(groups, dict):
                    groups = groups.get("data", [])
                if not isinstance(groups, list):
                    raise ValueError("群列表格式变化")
                for group in groups:
                    gid = str(group.get("group_id", ""))
                    if gid.isascii() and gid.isdigit():
                        targets.append({"session": f"{identity}:GroupMessage:{gid}", "group_id": gid,
                                        "name": str(group.get("group_name") or gid), "bot": identity})
            except Exception:
                errors.append(identity)
        return {"status": "ok", "data": {"targets": targets, "errors": errors}}

    async def crawl(self):
        p = self.plugin
        results, errors = [], {}
        async with p.lock:
            cfg = p.config()
            for name in ("tieba", "heybox"):
                if not cfg.get(name + "_enabled", True):
                    continue
                try:
                    items = await (p.sources.tieba(cfg, []) if name == "tieba" else p.sources.heybox(cfg))
                    results.extend(items)
                except Exception as exc:
                    errors[name] = f"爬取失败（{type(exc).__name__}），来源可能受限，请稍后重试。"
            self.test_posts = {x.key: x for x in results[:200]}
            self.test_at = int(time.time())
        return {"status": "ok", "data": {"posts": [asdict(x) for x in self.test_posts.values()],
                "errors": errors, "tested_at": self.test_at}}

    async def send(self):
        p = self.plugin
        try:
            body = await self.body()
            identity, session, keys = body.get("request_id"), body.get("session"), body.get("keys")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,80}", identity):
                raise ValueError("推送请求标识不正确，请重新打开页面。")
            if not isinstance(session, str):
                raise ValueError("请选择目标群聊。")
            parts = session.split(":")
            if len(parts) != 3 or parts[1] != "GroupMessage" or not parts[2].isascii() or not parts[2].isdigit():
                raise ValueError("目标必须是有效的 OneBot 群聊。")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 10 or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
                raise ValueError("每次请选择 1–10 篇不同帖子。")
            platform = next((x for x in self.platforms() if x.meta().id == parts[0]), None)
            if platform is None:
                raise ValueError("目标机器人当前不可用。")
            async with p.lock:
                p.store.prune_history(int(time.time()))
                previous = p.store.db.execute("SELECT state,session,posts FROM history WHERE id=?", ("manual:" + identity,)).fetchone()
                if previous:
                    if previous["session"] != session or [x["source"] + ":" + x["id"] for x in json.loads(previous["posts"])] != keys:
                        raise ValueError("同一请求标识不能用于不同的推送内容。")
                    return {"status": "ok", "data": {"state": previous[0], "duplicate": True}}
                # Revalidate membership server-side; do not trust a forged session from the page.
                groups = await asyncio.wait_for(platform.bot.get_group_list(), timeout=10)
                if isinstance(groups, dict):
                    groups = groups.get("data", [])
                if not isinstance(groups, list) or not any(str(g.get("group_id")) == parts[2] for g in groups):
                    raise ValueError("机器人不在所选群内，或群列表不可用。")
                available = dict(self.test_posts)
                for row in p.store.db.execute("SELECT key,data FROM posts WHERE seen>=?", (int(time.time()) - 86400,)):
                    available.setdefault(row["key"], Post(**json.loads(row["data"])))
                if self.test_at and int(time.time()) - self.test_at > 3600:
                    available = {k: v for k, v in available.items() if k not in self.test_posts}
                if any(k not in available for k in keys):
                    raise ValueError("部分帖子预览已过期，请重新测试爬取或刷新。")
                posts = [available[k] for k in keys]
                hid = "manual:" + identity
                from .presentation import prepare
                chain, text, layout = await prepare(p.context, p.sources, session, posts, "P5X 帖子分享", p.config())
                p.store.record_history(hid, session, posts, "manual", int(time.time()), text, layout)
                try:
                    result = await asyncio.wait_for(p.context.send_message(session, chain), timeout=45)
                    state = "submitted" if result else "unknown"
                except asyncio.CancelledError:
                    p.store.history_result(hid, session, posts, "unknown")
                    raise
                except Exception:
                    state = "unknown"
                p.store.history_result(hid, session, posts, state)
            return {"status": "ok", "data": {"state": state, "duplicate": False}}
        except (ValueError, TimeoutError) as exc:
            return {"status": "error", "message": str(exc) or "读取群列表超时，请稍后重试。"}
        except Exception:
            return {"status": "error", "message": "无法确认群列表或执行推送，请刷新状态后重试。"}

    async def save(self):
        p = self.plugin
        try:
            values = validate_config(await self.body())
            async with p.lock:
                previous = copy.deepcopy(dict(p.cfg))
                changed_sources = [name for name, field in (("tieba", "tieba_forum"), ("heybox", "heybox_topics"))
                                   if field in values and values[field] != p.cfg.get(field, SCHEMA[field]["default"])]
                try:
                    p.cfg.update(values)
                    # AstrBot owns the config path and atomic persistence.
                    p.cfg.save_config()
                except Exception:
                    p.cfg.clear()
                    p.cfg.update(previous)
                    return {"status": "error", "message": "配置写入失败，运行设置已恢复，请检查文件权限。"}
                now = int(time.time())
                with p.store.db:
                    for row in p.store.db.execute("SELECT key,data,qualified_at FROM posts").fetchall():
                        post = Post(**json.loads(row["data"]))
                        if post.source in changed_sources:
                            p.store.db.execute("UPDATE posts SET qualified=0,qualified_at=0,seen=0 WHERE key=?", (row["key"],))
                            continue
                        hit = qualifies(post, p.config(), now)
                        when = row["qualified_at"]
                        if hit and when is None:
                            when = now
                        p.store.db.execute("UPDATE posts SET qualified=?,qualified_at=? WHERE key=?", (int(hit), when, row["key"]))
                    for name in changed_sources:
                        p.store.db.execute("DELETE FROM sources WHERE name=?", (name,))
                p.last_scan = 0
            return {"status": "ok", "data": {"saved": True}, "message": "配置已保存，下轮检查使用新设置。"}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def subscription(self):
        p = self.plugin
        try:
            body = await self.body()
            session, action = body.get("session"), body.get("action")
            if not isinstance(session, str) or action not in ("update", "remove"):
                raise ValueError("订阅操作格式不正确。")
            at = clock(body.get("time", "")) if action == "update" and isinstance(body.get("time"), str) else None
            if action == "update" and at is None:
                raise ValueError("请填写有效时间。")
            async with p.lock:
                if not any(s["session"] == session for s in p.store.subscriptions()):
                    raise ValueError("该订阅不存在，请刷新页面。")
                if action == "remove":
                    p.store.unsubscribe(session)
                else:
                    p.store.subscribe(session, at, int(time.time()))
            return {"status": "ok", "data": {"saved": True}}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
