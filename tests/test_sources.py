import json
import unittest

from astrbot_plugin_p5x_feed.sources import Sources, like_count


class SourceTests(unittest.IsolatedAsyncioTestCase):
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
