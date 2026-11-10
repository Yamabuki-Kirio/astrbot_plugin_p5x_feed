"""Bounded, anonymous read-only clients. No account login or comment API."""
from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urlencode

import aiohttp

from .core import Post
from .presentation import image_urls


def number(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def like_count(data: dict, field: str) -> int | None:
    value = data.get(field)
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def tieba_post(t: dict, first: dict | None = None) -> Post:
    first = first or {}
    abstract = t.get("abstract") or first.get("content") or []
    summary = "".join(str(x.get("text", "")) for x in abstract if isinstance(x, dict) and number(x.get("type")) == 0)
    author = t.get("author") or first.get("author") or {}
    likes = like_count(t.get("agree") or {}, "agree_num")
    if likes is None:
        likes = like_count(t, "agree_num")
    pics = list(t.get("pic_list") or [])
    for item in (t.get("abstract") or []) + (first.get("content") or []):
        if isinstance(item, dict) and number(item.get("type")) == 3:
            pics.append(item)
    return Post("tieba", str(t["id"]), str(t.get("title") or ""),
                str(author.get("name_show") or author.get("name") or ""), summary,
                f"https://tieba.baidu.com/p/{t['id']}", number(t.get("create_time") or first.get("time")),
                likes, number(t.get("reply_num")), bool(number(t.get("is_good"))), image_urls(pics))


# Independent Python implementation of the MIT heybox-core hkey algorithm.
ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def hkey(path: str, timestamp: int, nonce: str) -> str:
    def mapped(s, alphabet):
        return "".join(alphabet[ord(c) % len(alphabet)] for c in s)

    def xt(x):
        return (255 & ((x << 1) ^ 27)) if x & 128 else x << 1

    def m3(x):
        return xt(x) ^ x

    def m4(x):
        return m3(xt(x))

    def m8(x):
        return m4(m3(xt(x)))

    def m14(x):
        return m8(x) ^ m4(x) ^ m3(x)

    path = "/" + "/".join(part for part in path.split("/") if part) + "/"
    parts = [mapped(str(timestamp), ALPHABET[:-2]), mapped(path, ALPHABET), mapped(nonce, ALPHABET)]
    mixed = "".join(p[i] for i in range(max(map(len, parts))) for p in parts if i < len(p))[:20]
    value = hashlib.md5(mixed.encode()).hexdigest()
    a, b, c, d, e, f = map(ord, value[-6:])
    tail = [m14(a) ^ m8(b) ^ m4(c) ^ m3(d), m3(a) ^ m14(b) ^ m8(c) ^ m4(d),
            m4(a) ^ m3(b) ^ m14(c) ^ m8(d), m8(a) ^ m4(b) ^ m3(c) ^ m14(d), e, f]
    return mapped(value[:5], ALPHABET[:-4]) + str(sum(tail) % 100).zfill(2)


class Sources:
    def __init__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                            headers={"User-Agent": "Mozilla/5.0 P5XFeed/0.3.0"})
        self.device = secrets.token_hex(16)
        self.warnings: dict[str, str] = {}

    async def request(self, method: str, url: str, **kwargs) -> dict:
        async with self.session.request(method, url, allow_redirects=False, **kwargs) as r:
            if r.status != 200:
                raise RuntimeError(f"来源 HTTP {r.status}，本轮停止")
            # Bound memory even if an upstream unexpectedly returns a large page.
            raw = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 2_000_000:
                    raise RuntimeError("来源响应超出大小限制")
            import json
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise RuntimeError("来源响应结构变化")
            return data

    async def tb(self, path: str, **extra) -> dict:
        data = {"_client_type": "2", "_client_version": "12.35.1.0", **{k: str(v) for k, v in extra.items()}}
        raw = "".join(f"{k}={data[k]}" for k in sorted(data)) + "tiebaclient!!!"
        data["sign"] = hashlib.md5(raw.encode()).hexdigest().upper()
        result = await self.request("POST", "https://tieba.baidu.com/" + path, data=data)
        if str(result.get("error_code")) != "0":
            raise RuntimeError("贴吧拒绝请求或需要验证")
        return result

    async def tieba(self, cfg: dict, tracked: list[str]) -> list[Post]:
        found: dict[str, Post] = {}
        self.warnings.pop("tieba", None)
        for good in (False, True):
            seen = set()
            for page in range(1, int(cfg.get("tieba_pages", 3)) + 1):
                params = {"kw": cfg.get("tieba_forum", "女神异闻录夜幕魅影"), "pn": page, "rn": 30}
                if good:
                    params["is_good"] = 1
                r = await self.tb("c/f/frs/page", **params)
                items = r.get("thread_list")
                if not isinstance(items, list) or (page == 1 and not items):
                    raise RuntimeError("贴吧列表为空或字段变化，未更新基线")
                added = 0
                for t in items:
                    if not isinstance(t, dict) or not str(t.get("id", "")).isdigit():
                        continue
                    identity = str(t["id"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    p = tieba_post(t)
                    if good and not p.good:
                        raise RuntimeError("贴吧精华筛选未生效")
                    if identity in found:
                        p.good = p.good or found[identity].good
                    found[identity] = p
                    added += 1
                if not added or not number((r.get("page") or {}).get("has_more")):
                    break
        # Recheck a bounded rotation of recent threads which have left the list.
        for identity in tracked:
            if identity in found:
                continue
            try:
                r = await self.tb("c/f/pb/page", kz=identity, pn=1, rn=2, lz=1)
                t = r.get("thread") or {}
                if str(t.get("id")) != identity:
                    raise RuntimeError("贴吧详情帖子 ID 不匹配")
                first = next((p for p in r.get("post_list", []) if number(p.get("floor")) == 1), {})
                found[identity] = tieba_post(t, first)
            except (RuntimeError, aiohttp.ClientError, ValueError, TimeoutError):
                self.warnings["tieba"] = "部分离开列表的帖子复查失败；保留已有记录"
        return list(found.values())

    async def heybox(self, cfg: dict) -> list[Post]:
        found: dict[str, Post] = {}
        ids = {str(x) for x in cfg.get("heybox_topics", ["600205", "671144"])}
        if not ids or any(not x.isdigit() or len(x) > 32 for x in ids):
            raise RuntimeError("小黑盒分区 ID 配置不合法")
        failures = 0
        self.warnings.pop("heybox", None)
        for identity in sorted(ids):
            try:
                path = "/bbs/app/topic/feeds"
                timestamp, nonce = int(time.time()), secrets.token_hex(16).upper()
                params = dict(topic_id=identity, offset="0", limit="20", lastval="", dw="720", sort_filter="hot-rank",
                              os_type="web", client_type="web", version="999.0.4", web_version="3.0",
                              x_client_type="web", x_app="heybox_website", x_os_type="Windows", device_info="Chrome",
                              device_id=self.device, _time=str(timestamp), nonce=nonce, hkey=hkey(path, timestamp, nonce))
                r = await self.request("GET", "https://api.xiaoheihe.cn" + path + "?" + urlencode(params))
                if r.get("status") != "ok":
                    raise RuntimeError("小黑盒需要验证、签名失效或接口受限")
                items = (r.get("result") or {}).get("links")
                if not isinstance(items, list):
                    raise RuntimeError("小黑盒列表字段变化")
                for t in items:
                    topics = {str(x.get("topic_id")) for x in t.get("topics", []) if isinstance(x, dict)}
                    if not topics.intersection(ids) or not str(t.get("linkid", "")).isdigit():
                        continue
                    url = str(t.get("share_url") or "")
                    if not url.startswith("https://api.xiaoheihe.cn/"):
                        continue
                    p = Post("heybox", str(t["linkid"]), str(t.get("title") or ""),
                             str((t.get("user") or {}).get("username") or ""), str(t.get("description") or ""),
                             url, number(t.get("create_at")), like_count(t, "link_award_num"), number(t.get("comment_num")),
                             images=image_urls([t.get("imgs") or [], t.get("thumbs") or []]))
                    found[p.id] = p
            except (RuntimeError, aiohttp.ClientError, ValueError, TimeoutError):
                failures += 1
        if failures == len(ids):
            raise RuntimeError("小黑盒全部分区读取失败，可能需要验证或接口已变化")
        if failures:
            # Do not partially establish the initial source baseline.
            raise RuntimeError("小黑盒部分分区读取失败，本轮不更新候选及基线")
        return list(found.values())

    async def close(self):
        await self.session.close()
