from io import BytesIO
import unittest
from PIL import Image
from astrbot_plugin_p5x_feed.cards import render_card
from astrbot_plugin_p5x_feed.core import Post


class CardTests(unittest.TestCase):
    def test_card_without_source_images_is_valid_png(self):
        post = Post('tieba', '1', '没有原图的中文帖子', '作者', '第一段\n第二段',
                    'https://tieba.baidu.com/p/1', 1800000000, 50, 2)
        data = render_card(post, {})
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.width, 940)
            self.assertGreater(image.height, 200)
            image.verify()

    def test_long_post_and_disabled_text_sections(self):
        post = Post('heybox', '2', '标题' * 100, '', '正文' * 1500, '', 0, None, 0)
        data = render_card(post, {'summary_length': 2000})
        with Image.open(BytesIO(data)) as image:
            self.assertLess(image.height, 6000)
        self.assertTrue(render_card(post, {'include_title_author': False,
                        'include_summary': False, 'include_links': False}).startswith(b'\x89PNG'))
