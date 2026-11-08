"""Shared QQ presentation for scheduled delivery, manual delivery and preview."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import aiohttp

from .core import digest, post_text, post_sections


def image_urls(values: list, limit: int = 3) -> list[str]:
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            u = urlsplit(value)
            allowed = ("bdimg.com", "baidu.com", "bdstatic.com", "max-c.com")
            if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
                continue
            if not any(u.hostname == h or (u.hostname or "").endswith("." + h) for h in allowed):
                continue
        except ValueError:
            continue
        if value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


async def load_image(http, url: str) -> bytes | None:
    try:
        async with http.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200 or not r.headers.get("Content-Type", "").lower().startswith("image/"):
                return None
            data = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > 3 * 1024 * 1024:
                    return None
            valid = (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")
                     or data.startswith((b"GIF87a", b"GIF89a")) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
            return bytes(data) if valid else None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def prepare(context, sources, session: str, posts: list, heading: str, cfg: dict):
    from astrbot.api import message_components as Comp
    from astrbot.core.message.message_event_result import MessageChain

    text = digest(posts, heading, cfg)
    mode = cfg.get("send_mode", "forward")
    if mode == "text":
        if not text:
            raise ValueError("当前设置没有可发送的文字，请至少开启一个文字内容。")
        return MessageChain().message(text), text, {"mode": "text", "images": []}
    platform = next((x for x in context.platform_manager.platform_insts if x.meta().id == session.split(":", 1)[0]), None)
    bot_uin = "10000"
    if platform:
        try:
            login = await asyncio.wait_for(platform.bot.get_login_info(), timeout=5)
            if isinstance(login, dict):
                user = str(login.get("user_id") or (login.get("data") or {}).get("user_id") or "")
                if user.isascii() and user.isdigit():
                    bot_uin = user
        except Exception:
            pass
    nodes = []
    def add(content):
        if content:
            nodes.append(Comp.Node(uin=bot_uin, name="怪盗情报站", content=content))
    if cfg.get("include_overview", True):
        add([Comp.Plain(f"🃏 {heading}\n本次收录 {len(posts)} 篇")])
    requests = []
    if cfg.get("include_images", True) and getattr(sources, "session", None):
        for i, p in enumerate(posts):
            for url in image_urls(p.images, int(cfg.get("max_images_per_post", 1))):
                requests.append((i, url))
    semaphore = asyncio.Semaphore(4)
    async def read_one(url):
        async with semaphore:
            return await load_image(sources.session, url)
    tasks = [asyncio.create_task(read_one(url)) for _, url in requests]
    if tasks:
        try:
            await asyncio.wait(tasks, timeout=12)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    images = [t.result() if not t.cancelled() and t.exception() is None else None for t in tasks]
    delivered = []
    for i, p in enumerate(posts):
        sections = post_sections(p, i+1, len(posts), cfg)
        content = [Comp.Plain(post_text(p, i+1, len(posts), cfg))] if sections else []
        picture_components = []
        for (index, url), data in zip(requests, images):
            if index == i and data:
                picture_components.append(Comp.Image.fromBytes(data))
                delivered.append({"post": p.key, "url": url})
        if cfg.get("forward_layout", "compact") == "sectioned":
            for kind, text_part in sections:
                if kind != "link": add([Comp.Plain(text_part)])
            add(picture_components)
            for kind, text_part in sections:
                if kind == "link": add([Comp.Plain(text_part)])
        else:
            add(content + picture_components)
    if not nodes:
        raise ValueError("当前设置没有可发送的节点，请开启至少一项有内容的节点。")
    # Construct everything before claiming a delivery; no speculative fallback
    # after a platform send, which could duplicate a successfully accepted message.
    return MessageChain([Comp.Nodes(nodes)]), text, {"mode": "forward", "forward_layout": cfg.get("forward_layout", "compact"), "node_count": len(nodes), "images": delivered}
