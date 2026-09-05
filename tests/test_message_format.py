from __future__ import annotations

import unittest
from html.parser import HTMLParser
from unittest.mock import AsyncMock, MagicMock, patch

from app.formatter import MessageFormatter
from app.rss import FeedClient


class ParsedMessage(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = ""
        self.links = []
        self.feed(text)

    def handle_data(self, data: str) -> None:
        self.text += data

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "a":
            self.links.append(dict(attrs)["href"])


class MessageFormatTests(unittest.TestCase):
    def test_notification_matches_requested_layout_and_escapes_content(self) -> None:
        link = 'https://www.nodeseek.com/post-1-1?a=1&b="2"'
        title = '标题 <test> & "引用"'
        summary = '正文 <b>原文</b> & 内容'
        message = MessageFormatter().render(title=title, link=link, summary=summary)
        parsed = ParsedMessage(message)
        self.assertEqual(f"NodeSeek 新帖提醒\n标题：{title}\n摘要：{summary}", parsed.text)
        self.assertEqual([link], parsed.links)
        self.assertNotIn("时间：", message)
        self.assertNotIn("链接：", message)
        self.assertNotIn("关键词：", message)
        self.assertNotIn("<test>", message)
        self.assertNotIn("<b>原文</b>", message)

    def test_long_content_and_emoji_fit_telegram_limit_with_valid_html(self) -> None:
        for title, summary in [("标题", "🐱<&>" * 5000), ("🐱" * 5000, "正文")]:
            message = MessageFormatter().render(title=title, link="https://example.com/", summary=summary)
            parsed = ParsedMessage(message)
            self.assertLessEqual(len(parsed.text.encode("utf-16-le")) // 2, 4096)
            self.assertEqual(["https://example.com/"], parsed.links)
            self.assertIn("…", parsed.text)

    def test_empty_content_has_an_explicit_fallback(self) -> None:
        message = MessageFormatter().render(title="标题", link="https://example.com/", summary="")
        self.assertIn("摘要：暂无正文", message)


class FeedContentTests(unittest.IsolatedAsyncioTestCase):
    async def fetch_entry(self, raw: bytes):
        response = MagicMock()
        response.read = AsyncMock(return_value=raw)
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        with patch("app.rss.aiohttp.ClientSession") as client:
            client.return_value.__aenter__ = AsyncMock(return_value=session)
            result = await FeedClient(20, 30).fetch("https://rss.example.com/")
        return result.entries[0]

    async def test_rss_content_is_not_truncated_before_keyword_matching(self) -> None:
        body = "正文" * 200 + " 末尾关键词"
        raw = f'''<rss version="2.0"><channel><title>NodeSeek</title><item>
            <guid>1</guid><title>帖子</title><link>https://www.nodeseek.com/post-1-1</link>
            <description><![CDATA[<p>{body}</p>]]></description><category>tech</category>
            </item></channel></rss>'''.encode()
        entry = await self.fetch_entry(raw)
        self.assertEqual(body, entry.summary)
        self.assertIn("末尾关键词", entry.source_text)

    async def test_content_encoded_takes_priority_over_short_description(self) -> None:
        raw = '''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel><title>NodeSeek</title><item><guid>1</guid><title>标题</title>
            <description>短摘要</description><content:encoded><![CDATA[
            <p>完整正文 <b>关键词</b></p>]]></content:encoded>
            </item></channel></rss>'''.encode()
        entry = await self.fetch_entry(raw)
        self.assertEqual("完整正文 关键词", entry.summary)
        self.assertIn("关键词", entry.source_text)
