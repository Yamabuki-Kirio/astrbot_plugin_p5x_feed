import json
import unittest

from astrbot_plugin_p5x_feed.sources import Sources, like_count, tieba_post


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tieba_abstract_and_detail_images(self):
        post = tieba_post({'id': '1', 'abstract': [
            {'type': 3, 'src': '//tiebapic.baidu.com/a.jpg'}]},
            {'content': [{'type': 3, 'origin_src': 'http://tiebapic.baidu.com/b.jpg'}]})
        self.assertEqual(post.images, ['https://tiebapic.baidu.com/a.jpg', 'https://tiebapic.baidu.com/b.jpg'])

    async def test_heybox_image_objects(self):
        from unittest.mock import AsyncMock
        source = Sources.__new__(Sources)
        source.device, source.warnings = 'test', {}
        source.request = AsyncMock(return_value={'status': 'ok', 'result': {'links': [{
            'linkid': '42', 'topics': [{'topic_id': '600205'}],
            'share_url': 'https://api.xiaoheihe.cn/example',
            'imgs': [{'url': '//imgheybox.max-c.com/a.jpg'}],
            'thumbs': ['https://imgheybox.max-c.com/b.jpg'],
        }]}})
        posts = await source.heybox({'heybox_topics': ['600205']})
        self.assertEqual(posts[0].images, ['https://imgheybox.max-c.com/a.jpg', 'https://imgheybox.max-c.com/b.jpg'])

    async def test_chunked_json_is_not_truncated(self):
        body = json.dumps({'title': '夜幕魅影'}, ensure_ascii=False).encode()
        class Content:
            async def iter_chunked(self, size):
                for i in range(0, len(body), 2):
                    yield body[i:i+2]
        class Response:
            status = 200
            content = Content()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session:
            def request(self, *args, **kwargs):
                return Response()
        source = Sources.__new__(Sources)
        source.session = Session()
        self.assertEqual(await source.request('GET', 'https://example.invalid'), {'title': '夜幕魅影'})

    async def test_unknown_like_is_not_zero(self):
        self.assertIsNone(like_count({}, 'link_award_num'))
        self.assertIsNone(like_count({'link_award_num': 'invalid'}, 'link_award_num'))
        self.assertEqual(like_count({'link_award_num': 0}, 'link_award_num'), 0)
