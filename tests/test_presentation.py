import unittest
from unittest.mock import patch, AsyncMock

from astrbot_plugin_p5x_feed.core import Post, digest, post_text
from astrbot_plugin_p5x_feed.presentation import image_urls
import test_delivery as host


class FormatTests(unittest.TestCase):
    def post(self):
        return Post('heybox', '1', ' 标题\n换行 ', '作者', '第一段\n\n第二段 &amp; 内容',
                    'https://api.xiaoheihe.cn/example', 1800000000, 128, 15)

    def test_sections_preserve_paragraphs_and_labels(self):
        text = digest([self.post()])
        for part in ['🃏 P5X 帖子精选', '本次收录 1 篇', '📌 1/1 · 小黑盒', '📖 标题：标题 换行',
                     '👤 作者：作者', '👍 点赞：128', '💬 回复：15', '📝 正文摘录：', '第一段\n\n第二段 & 内容', '🌐 原始链接：']:
            self.assertIn(part, text)

    def test_content_switches_and_ellipsis(self):
        p = self.post()
        text = post_text(p, 1, 1, {'include_title_author':False,'include_summary':False,'include_links':False})
        for part in ['📖', '👤', '📝', '🌐', p.url]: self.assertNotIn(part,text)
        p.summary = '字' * 600
        text = post_text(p,1,1,{'summary_length':50})
        self.assertIn('字'*49+'…', text)
        self.assertNotIn('字'*50, text)

    def test_old_posts_have_images_default_and_url_filter(self):
        self.assertEqual(self.post().images, [])
        values = ['https://imgheybox.max-c.com/a.jpg','http://imgheybox.max-c.com/a.jpg',
                  'https://imgheybox.max-c.com.evil.test/a.jpg','https://127.0.0.1/a',
                  'https://user:pass@imgheybox.max-c.com/a','file:///tmp/a',
                  'https://imgheybox.max-c.com/a.jpg','https://tiebapic.baidu.com/b.png']
        self.assertEqual(image_urls(values,1),[values[0]])
        self.assertEqual(image_urls(values),[values[0], values[-1]])

    def test_image_objects_and_url_normalization(self):
        self.assertEqual(image_urls([
            {'url': '//imgheybox.max-c.com/a.jpg'},
            {'original': {'url': 'http://tiebapic.baidu.com/b.jpg'}},
            {'src': 'https://evil.test/c.jpg'},
            ['https://imgheybox.max-c.com/a.jpg'],
        ]), ['https://imgheybox.max-c.com/a.jpg', 'https://tiebapic.baidu.com/b.jpg'])


# Reuse the host fixture without rediscovering its entire existing test suite.
class PresentationHostTests(host.DeliveryTests):
    async def test_collected_images_reach_automatic_and_manual_messages(self):
        from astrbot_plugin_p5x_feed.sources import tieba_post
        post = tieba_post({'id': '42', 'title': '配图帖子', 'pic_list': [
            {'original': {'url': '//tiebapic.baidu.com/example.jpg'}}]})
        self.post.images = post.images
        self.plugin.sources.session = object()
        self.plugin.store.ingest(self.post.source, [self.post], self.plugin.config(), int(__import__('time').time()))
        with patch('astrbot_plugin_p5x_feed.presentation.load_image', new=AsyncMock(return_value=b'image')):
            await self.plugin.send_due()
            chain = self.context.send_message.await_args.args[1]
            self.assertEqual(chain.chain[0].nodes[1].content[2].data, b'image')
            self.plugin.web.body = AsyncMock(return_value={'request_id': 'image-manual-12345678',
                'session': 'bot:GroupMessage:123', 'keys': [self.post.key]})
            result = await self.plugin.web.send()
            self.assertEqual(result['data']['state'], 'submitted')
            chain = self.context.send_message.await_args.args[1]
            self.assertEqual(chain.chain[0].nodes[1].content[2].data, b'image')

    async def test_sectioned_node_selection_matches_preview(self):
        from astrbot_plugin_p5x_feed.presentation import prepare
        from astrbot_plugin_p5x_feed.core import format_preview
        self.post.summary = "第一段正文"
        cfg = {"forward_layout":"sectioned", "include_overview":False, "include_images":False}
        chain, text, layout = await prepare(self.context,self.plugin.sources,'bot:GroupMessage:123',[self.post],'测试',cfg)
        nodes = chain.chain[0].nodes
        self.assertEqual(len(nodes),3)
        preview = format_preview([self.post], cfg, '测试')
        for node in nodes:
            self.assertIn(node.content[0].text, preview)
        self.assertEqual(layout['node_count'],3)
        cfg.update(include_title_author=False, include_links=False)
        chain,_,_ = await prepare(self.context,self.plugin.sources,'bot:GroupMessage:123',[self.post],'测试',cfg)
        self.assertEqual(len(chain.chain[0].nodes),1)
        self.assertIn('正文摘录',chain.chain[0].nodes[0].content[0].text)
        cfg['include_summary'] = False
        with self.assertRaises(ValueError):
            await prepare(self.context,self.plugin.sources,'bot:GroupMessage:123',[self.post],'测试',cfg)

    async def test_forward_nodes_and_history_match(self):
        await self.plugin.send_due()
        chain = self.context.send_message.await_args.args[1]
        self.assertEqual(len(chain.chain),1)
        nodes = chain.chain[0].nodes
        self.assertEqual(len(nodes),2)
        self.assertTrue(nodes[1].content[1].data.startswith(b'\x89PNG'))
        self.assertIn('正文', nodes[1].content[0].text) if self.post.summary else None
        row = self.plugin.store.db.execute('SELECT message,layout FROM history').fetchone()
        self.assertIn(nodes[1].content[0].text,row['message'])
        self.assertIn('forward',row['layout'])

    async def test_text_mode_and_manual_use_same_switches(self):
        self.plugin.cfg = {'send_mode':'text','include_links':False,'include_summary':False}
        await self.plugin.send_due()
        chain = self.context.send_message.await_args.args[1]
        self.assertNotIn('🌐',chain.text)
        self.assertNotIn('📝',chain.text)
        self.plugin.web.body = AsyncMock(return_value={'request_id':'presentation-manual-123456',
            'session':'bot:GroupMessage:123','keys':[self.post.key]})
        result = await self.plugin.web.send()
        self.assertEqual(result['data']['state'],'submitted')
        chain = self.context.send_message.await_args.args[1]
        self.assertIn('P5X 帖子分享',chain.text)
        self.assertNotIn('🌐',chain.text)
        self.assertNotIn('📝',chain.text)

    async def test_successful_image_and_disabled_images(self):
        self.post.images = ['https://imgheybox.max-c.com/test.png']
        self.plugin.sources.session = object()
        from astrbot_plugin_p5x_feed.presentation import prepare
        with patch('astrbot_plugin_p5x_feed.presentation.load_image',new=AsyncMock(return_value=b'image-bytes')) as fetch:
            chain,_,layout = await prepare(self.context,self.plugin.sources,'bot:GroupMessage:123',[self.post],'测试',{})
            self.assertEqual(chain.chain[0].nodes[1].content[2].data,b'image-bytes')
            self.assertEqual(len(layout['images']),1)
            await prepare(self.context,self.plugin.sources,'bot:GroupMessage:123',[self.post],'测试',{'include_images':False})
            self.assertEqual(fetch.await_count,1)

    async def test_failed_image_keeps_text_and_does_not_claim_image(self):
        self.post.images = ['https://imgheybox.max-c.com/test.png']
        self.plugin.sources.session = object()
        from astrbot_plugin_p5x_feed.presentation import prepare
        with patch('astrbot_plugin_p5x_feed.presentation.load_image',new=AsyncMock(return_value=None)):
            chain,text,layout = await prepare(self.context,self.plugin.sources,'bot:GroupMessage:123',[self.post],'测试',{})
        self.assertIn('测试',text)
        self.assertEqual(layout['images'],[])
        self.assertEqual(len(chain.chain[0].nodes[1].content),2)
        self.assertEqual(layout['cards'], [self.post.key])


# unittest discovers inherited test methods; keep only the new checks on this subclass.
for _name in dir(host.DeliveryTests):
    if _name.startswith('test_'):
        setattr(PresentationHostTests,_name,None)
