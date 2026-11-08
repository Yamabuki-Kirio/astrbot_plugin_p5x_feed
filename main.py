from __future__ import annotations

import asyncio
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .core import CST, Store, clock, digest
from .sources import Sources
from .web import WebUI


@register("astrbot_plugin_p5x_feed", "Yamabuki-Kirio", "P5X 贴吧与小黑盒帖子推送", "0.2.0")
class P5XFeed(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.lock = asyncio.Lock()
        self.task = None
        self.sources = None
        self.store = None
        self.last_scan = 0.0
        self.web = WebUI(self)

    async def initialize(self):
        self.store = Store(StarTools.get_data_dir("astrbot_plugin_p5x_feed") / "feed.sqlite3")
        self.sources = Sources()
        self.web.register()
        self.task = asyncio.create_task(self.worker(), name="p5x-feed")

    async def terminate(self):
        self.web.unregister()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        # Wait for an in-progress command before closing shared resources.
        async with self.lock:
            if self.sources:
                await self.sources.close()
            if self.store:
                self.store.close()

    def config(self) -> dict:
        cfg = dict(self.cfg)
        for key, default, low, high in [
            ("check_minutes", 30, 10, 1440), ("max_posts", 3, 1, 10),
            ("tieba_pages", 3, 1, 10), ("tieba_days", 7, 1, 90),
            ("heybox_days", 14, 1, 90), ("tieba_likes", 50, 1, 100000),
            ("heybox_likes", 100, 1, 100000), ("recheck_limit", 10, 0, 30),
        ]:
            try:
                cfg[key] = max(low, min(high, int(cfg.get(key, default))))
            except (ValueError, TypeError):
                cfg[key] = default
        return cfg

    async def scan(self):
        cfg = self.config()
        for source in ("tieba", "heybox"):
            if not cfg.get(source + "_enabled", True):
                continue
            try:
                if source == "tieba":
                    tracked = self.store.tracked_tieba(int(time.time()), cfg["tieba_days"], cfg["recheck_limit"])
                    posts = await self.sources.tieba(cfg, tracked)
                else:
                    posts = await self.sources.heybox(cfg)
                self.store.ingest(source, posts, cfg, int(time.time()))
            except Exception as exc:
                # Never log full request URLs, signatures, cookies or upstream bodies.
                self.store.failure(source, int(time.time()), f"读取失败（{type(exc).__name__}），稍后自动检查")
                logger.warning("[P5X推送] %s 检查失败：%s", source, type(exc).__name__)
        self.last_scan = time.monotonic()

    async def send_due(self):
        cfg = self.config()
        now = datetime.now(CST)
        for sub in self.store.subscriptions():
            if now.strftime("%H:%M") < sub["time"]:
                continue
            session, day = sub["session"], now.strftime("%Y-%m-%d")
            posts = self.store.candidates(cfg, int(time.time()), session)[:cfg["max_posts"]]
            if not posts:
                continue
            # Resolve the platform before claiming; missing adapters are not sends.
            try:
                from astrbot.core.platform.message_session import MessageSesion
                origin = MessageSesion.from_str(session)
                if not any(p.meta().id == origin.platform_name for p in self.context.platform_manager.platform_insts):
                    continue
            except (ValueError, AttributeError):
                logger.warning("[P5X推送] 订阅会话无法解析或平台不可用")
                continue
            if not self.store.claim(session, day, posts):
                continue
            history_id = f"auto:{session}:{day}"
            self.store.record_history(history_id, session, posts, "auto", int(time.time()))
            try:
                result = await asyncio.wait_for(self.context.send_message(session, MessageChain().message(digest(posts))), timeout=45)
                state = "submitted" if result else "unknown"
            except asyncio.CancelledError:
                self.store.finish(session, day, posts, "unknown")
                self.store.history_result(history_id, session, posts, "unknown")
                raise
            except Exception as exc:
                state = "unknown"
                logger.warning("[P5X推送] 投递结果未知：%s", type(exc).__name__)
            self.store.finish(session, day, posts, state)
            self.store.history_result(history_id, session, posts, state)

    async def worker(self):
        while True:
            try:
                async with self.lock:
                    self.store.prune_history(int(time.time()))
                    if not self.last_scan or time.monotonic() - self.last_scan >= self.config()["check_minutes"] * 60:
                        await self.scan()
                    await self.send_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[P5X推送] 后台任务异常：%s", type(exc).__name__)
            await asyncio.sleep(30)

    def access(self, event, admin=False) -> str | None:
        if event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            return "请在 OneBot 群聊中使用。"
        if admin:
            role = getattr(event.message_obj.sender, "role", "")
            raw = getattr(event.message_obj, "raw_message", None)
            if isinstance(raw, dict):
                role = (raw.get("sender") or {}).get("role", role)
            if not event.is_admin() and role not in ("owner", "admin"):
                return "仅群主、群管理员或 AstrBot 管理员可修改订阅。"
        if not self.store:
            return "插件尚未初始化完成，请稍后重试。"
        return None

    @filter.command("p5x订阅")
    async def subscribe(self, event: AstrMessageEvent, at: str = "20:00"):
        """订阅当前群，每天按北京时间发送，例如 /p5x订阅 每天20:00。"""
        error = self.access(event, True)
        if error:
            yield event.plain_result(error)
            return
        try:
            at = clock(at)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        async with self.lock:
            self.store.subscribe(event.unified_msg_origin, at, int(time.time()))
        yield event.plain_result(f"本群已订阅：每天 {at}（北京时间），每次最多 {self.config()['max_posts']} 篇。无新达标帖时跳过，已有历史达标帖不补发。")

    @filter.command("p5x退订")
    async def unsubscribe(self, event: AstrMessageEvent):
        """关闭当前群的自动推送。"""
        error = self.access(event, True)
        if error:
            yield event.plain_result(error)
            return
        async with self.lock:
            self.store.unsubscribe(event.unified_msg_origin)
        yield event.plain_result("本群已退订 P5X 帖子推送。")

    @filter.command("p5x精选")
    async def preview(self, event: AstrMessageEvent):
        """查看已采集的达标帖，不消耗自动推送记录。"""
        error = self.access(event)
        if error:
            yield event.plain_result(error)
            return
        async with self.lock:
            cfg = self.config()
            posts = self.store.candidates(cfg, int(time.time()))[:cfg["max_posts"]]
        yield event.plain_result(digest(posts, "P5X 当前达标帖（手动查看）") if posts else "当前缓存没有符合条件的帖子。请用 /p5x推送状态 查看来源是否正常。")

    @filter.command("p5x推送状态")
    async def status(self, event: AstrMessageEvent):
        """查看本群订阅、来源检查与最近投递状态。"""
        error = self.access(event)
        if error:
            yield event.plain_result(error)
            return
        async with self.lock:
            cfg = self.config()
            sub = next((x for x in self.store.subscriptions() if x["session"] == event.unified_msg_origin), None)
            lines = [f"本群：每天 {sub['time']}（北京时间）" if sub else "本群：未订阅",
                     f"贴吧：{cfg['tieba_days']} 天内 ≥{cfg['tieba_likes']} 赞，或新增精华",
                     f"小黑盒：{cfg['heybox_days']} 天内 ≥{cfg['heybox_likes']} 赞"]
            for name, label in (("tieba", "贴吧"), ("heybox", "小黑盒")):
                row = self.store.db.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
                if not cfg.get(name + "_enabled", True):
                    lines.append(label + "：已关闭")
                elif not row:
                    lines.append(label + "：等待首次检查")
                else:
                    success = datetime.fromtimestamp(row["success"], CST).strftime("%m-%d %H:%M") if row["success"] else "无"
                    lines.append(f"{label}：最近成功 {success}；{row['error'] or self.sources.warnings.get(name) or '正常'}")
            run = self.store.db.execute("SELECT day,state FROM runs WHERE session=? ORDER BY day DESC LIMIT 1", (event.unified_msg_origin,)).fetchone()
            if run:
                states = {"submitted": "已提交平台", "unknown": "结果未知，未自动重发", "sending": "正在发送"}
                lines.append(f"最近投递：{run['day']} {states.get(run['state'], run['state'])}")
        yield event.plain_result("\n".join(lines))
