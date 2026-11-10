"""Offline post cards with a bundled, openly licensed Chinese font."""
from io import BytesIO
from pathlib import Path
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from .core import CST, clean_excerpt

FONT = Path(__file__).parent / 'assets' / 'NotoSansSC-Regular.otf'


def render_card(post, cfg: dict) -> bytes:
    title_font = ImageFont.truetype(str(FONT), 36)
    body_font = ImageFont.truetype(str(FONT), 25)
    small_font = ImageFont.truetype(str(FONT), 21)
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))

    def wrap(text, font):
        lines = []
        for paragraph in text.split('\n'):
            line = ''
            for char in paragraph:
                if line and measure.textlength(line + char, font=font) > 820:
                    lines.append(line)
                    line = ''
                line += char
            lines.append(line)
        return lines

    blocks = []
    if cfg.get('include_title_author', True):
        blocks.append((clean_excerpt(post.title, 140), title_font, '#ffffff', 52))
        origin = '贴吧' if post.source == 'tieba' else '小黑盒'
        date = datetime.fromtimestamp(post.created, CST).strftime('%Y-%m-%d %H:%M') if post.created else '日期未知'
        blocks.append((f'{origin} · {clean_excerpt(post.author, 40) or "未知作者"}\n{date} · 赞 {post.likes if post.likes is not None else "未知"} · 回复 {post.comments}', small_font, '#b8bacb', 34))
    if cfg.get('include_summary', True) and post.summary:
        blocks.append((clean_excerpt(post.summary, int(cfg.get('summary_length', 500)), True), body_font, '#e4e5ee', 40))
    if cfg.get('include_links', True):
        blocks.append((clean_excerpt(post.url, 300), small_font, '#ffb9c4', 32))
    if not blocks:
        blocks.append(('帖子配图分享', body_font, '#e4e5ee', 40))
    prepared = [(wrap(text, font), font, color, step) for text, font, color, step in blocks]
    height = 150 + sum(len(lines) * step + 26 for lines, _, _, step in prepared)
    image = Image.new('RGB', (940, height), '#171923')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 12, height), fill='#e63855')
    draw.text((60, 30), '怪盗情报站 · P5X', font=small_font, fill='#ff627e')
    y = 90
    for lines, font, color, step in prepared:
        for line in lines:
            draw.text((60, y), line, font=font, fill=color)
            y += step
        y += 26
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
