"""Exercise plugin delivery decisions with a fake host, never send real messages."""
import importlib
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from astrbot_plugin_p5x_feed.core import CST, Post, Store


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.modules = {}
        names = ["astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star", "astrbot.core",
                 "astrbot.core.message", "astrbot.core.message.message_event_result", "astrbot.core.platform",
                 "astrbot.core.platform.message_session"]
        for name in names:
            self.modules[name] = sys.modules.get(name)
            sys.modules[name] = types.ModuleType(name)
        sys.modules['astrbot.api'].AstrBotConfig = dict
        sys.modules['astrbot.api'].logger = logging.getLogger('test')
        event = sys.modules['astrbot.api.event']
        event.AstrMessageEvent = object
        event.filter = types.SimpleNamespace(command=lambda *a, **k: lambda f: f)
        star = sys.modules['astrbot.api.star']
        class Base:
            def __init__(self, context):
                self.context = context
        star.Star, star.Context, star.StarTools = Base, object, object
        star.register = lambda *a: lambda cls: cls
        class Chain:
            def message(self, text):
                self.text = text
                return self
        sys.modules['astrbot.core.message.message_event_result'].MessageChain = Chain
        sys.modules['astrbot.core.platform.message_session'].MessageSesion = types.SimpleNamespace(
            from_str=lambda s: types.SimpleNamespace(platform_name=s.split(':')[0]))
        self.old_main = sys.modules.pop('astrbot_plugin_p5x_feed.main', None)
        module = importlib.import_module('astrbot_plugin_p5x_feed.main')
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True),
            platform_manager=types.SimpleNamespace(platform_insts=[types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id='bot', name='aiocqhttp'),
                bot=types.SimpleNamespace(get_group_list=AsyncMock(return_value=[{'group_id':123,'group_name':'测试群'}])))]))
        self.plugin = module.P5XFeed(self.context, {})
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin.store = Store(Path(self.tmp.name) / 'state.sqlite3')
        self.plugin.sources = types.SimpleNamespace(warnings={})
        now = int(time.time())
        self.plugin.store.ingest('tieba', [], {}, now - 5)
        self.plugin.store.subscribe('bot:GroupMessage:123', '00:00', now - 3)
        self.post = Post('tieba', '1', '测试', '', '', 'https://tieba.baidu.com/p/1', now - 10, 50, 3)
        self.plugin.store.ingest('tieba', [self.post], {}, now - 1)

    async def asyncTearDown(self):
        self.plugin.store.close()
        self.tmp.cleanup()
        sys.modules.pop('astrbot_plugin_p5x_feed.main', None)
        if self.old_main:
            sys.modules['astrbot_plugin_p5x_feed.main'] = self.old_main
        for name, previous in self.modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    async def test_send_once_and_mark_submitted(self):
        await self.plugin.send_due()
        await self.plugin.send_due()
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.store.db.execute('SELECT state FROM runs').fetchone()[0], 'submitted')

    async def test_timeout_not_retried(self):
        self.context.send_message.side_effect = TimeoutError()
        await self.plugin.send_due()
        await self.plugin.send_due()
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.store.db.execute('SELECT state FROM runs').fetchone()[0], 'unknown')

    async def test_missing_platform_does_not_claim(self):
        self.context.platform_manager.platform_insts = []
        await self.plugin.send_due()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin.store.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    async def test_empty_day_does_not_claim(self):
        self.plugin.cfg = {'blocked_words': ['测试']}
        await self.plugin.send_due()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin.store.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    async def test_manual_send_is_idempotent_and_has_snapshot(self):
        web = self.plugin.web
        web.test_posts = {self.post.key: self.post}
        web.test_at = int(time.time())
        web.body = AsyncMock(return_value={'request_id':'12345678-1234-1234-1234-123456789012',
                                          'session':'bot:GroupMessage:123','keys':[self.post.key]})
        first = await web.send()
        second = await web.send()
        self.assertEqual(first['data']['state'], 'submitted')
        self.assertTrue(second['data']['duplicate'])
        self.assertEqual(self.context.send_message.await_count, 1)
        row = self.plugin.store.db.execute('SELECT * FROM history').fetchone()
        self.assertIn('测试', row['message'])
        self.assertEqual(row['trigger'], 'manual')
        self.assertEqual(row['state'], 'submitted')

    async def test_manual_send_rejects_foreign_group(self):
        web = self.plugin.web
        web.test_posts = {self.post.key: self.post}
        web.body = AsyncMock(return_value={'request_id':'12345678-1234-1234-1234-123456789012',
                                          'session':'bot:GroupMessage:999','keys':[self.post.key]})
        result = await web.send()
        self.assertEqual(result['status'], 'error')
        self.context.send_message.assert_not_awaited()

    async def test_crawl_does_not_modify_baseline_or_send(self):
        web = self.plugin.web
        self.plugin.sources.tieba = AsyncMock(return_value=[self.post])
        self.plugin.sources.heybox = AsyncMock(return_value=[])
        before = list(self.plugin.store.db.execute('SELECT qualified_at FROM posts'))[0][0]
        result = await web.crawl()
        self.assertEqual(len(result['data']['posts']), 1)
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin.store.db.execute('SELECT qualified_at FROM posts').fetchone()[0], before)

    async def test_history_expires_without_removing_dedup(self):
        store = self.plugin.store
        now = int(time.time())
        store.record_history('old', 'bot:GroupMessage:123', [self.post], 'manual', now - 7*86400 - 1)
        store.record_history('new', 'bot:GroupMessage:123', [self.post], 'manual', now)
        store.prune_history(now)
        self.assertEqual(store.db.execute('SELECT id FROM history').fetchone()[0], 'new')
        self.assertEqual(store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 1)

    async def test_config_persist_and_validation(self):
        class Config(dict):
            def save_config(self):
                self.saved = dict(self)
        cfg = Config(tieba_likes=50)
        self.plugin.cfg = cfg
        web = self.plugin.web
        web.body = AsyncMock(return_value={'tieba_likes':45})
        result = await web.save()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(cfg.saved['tieba_likes'], 45)
        web.body = AsyncMock(return_value={'tieba_likes':True})
        self.assertEqual((await web.save())['status'], 'error')
        self.assertEqual(cfg['tieba_likes'], 45)

    async def test_config_disk_failure_rolls_back(self):
        class Config(dict):
            def save_config(self):
                raise OSError('disk full')
        self.plugin.cfg = Config(tieba_likes=50)
        self.plugin.web.body = AsyncMock(return_value={'tieba_likes':40})
        self.assertEqual((await self.plugin.web.save())['status'], 'error')
        self.assertEqual(self.plugin.cfg['tieba_likes'], 50)

    async def test_history_pages_and_seven_day_window(self):
        now = int(time.time())
        for i in range(30):
            self.plugin.store.record_history('entry'+str(i), 'bot:GroupMessage:123', [self.post], 'manual', now-i)
        self.plugin.store.record_history('expired', 'bot:GroupMessage:123', [self.post], 'manual', now-8*86400)
        self.plugin.web.body = AsyncMock(return_value={'offset':25})
        result = await self.plugin.web.history()
        self.assertEqual(result['data']['total'], 30)
        self.assertEqual(len(result['data']['rows']), 5)
        self.assertTrue(all(row['id']!='expired' for row in result['data']['rows']))

    async def test_source_change_resets_baseline_and_pending(self):
        class Config(dict):
            def save_config(self):
                pass
        self.plugin.cfg = Config(tieba_forum='旧来源')
        self.plugin.web.body = AsyncMock(return_value={'tieba_forum':'新来源'})
        self.assertEqual((await self.plugin.web.save())['status'], 'ok')
        self.assertIsNone(self.plugin.store.db.execute("SELECT ready FROM sources WHERE name='tieba'").fetchone())
        self.assertEqual(self.plugin.store.db.execute('SELECT qualified_at FROM posts').fetchone()[0], 0)
        self.assertEqual(self.plugin.store.candidates({}, int(time.time())), [])
