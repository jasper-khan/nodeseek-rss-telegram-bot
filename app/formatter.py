from __future__ import annotations

from app.utils import escape_html


def _truncate_utf16(value: str, max_units: int) -> str:
    encoded = value.encode("utf-16-le")
    if len(encoded) <= max_units * 2:
        return value
    return encoded[: (max_units - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"


class MessageFormatter:
    def render(
        self,
        *,
        title: str,
        link: str,
        summary: str,
    ) -> str:
        title = _truncate_utf16(title.strip() or "无标题", 512)
        prefix = f"NodeSeek 新帖提醒\n标题：{title}\n摘要："
        remaining = 4096 - len(prefix.encode("utf-16-le")) // 2
        summary = _truncate_utf16(summary.strip() or "暂无正文", remaining)
        title_text = escape_html(title)
        summary_text = escape_html(summary)
        link_text = escape_html(link)
        return (
            "NodeSeek 新帖提醒\n"
            f'标题：<a href="{link_text}"><b>{title_text}</b></a>\n'
            f"摘要：{summary_text}"
        )
