from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from html import escape
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
from markdown_it import MarkdownIt

from .config import AppleNotesConfig

_ALLOWED_TEMPLATES = frozenset({"knowledge_card", "flow_chain", "plain"})
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "blockquote",
        "br",
        "code",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "small",
        "strong",
        "ul",
    }
)
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})


@dataclass(frozen=True)
class RenderedNote:
    title: str
    html: str
    plaintext: str
    content_hash: str
    html_bytes: int


class NotesRenderer:
    def __init__(self, config: AppleNotesConfig) -> None:
        self._config = config
        self._markdown = MarkdownIt(
            "commonmark",
            {
                "html": False,
                "linkify": False,
                "typographer": False,
            },
        )

    def fingerprint_create(self, title: str, markdown: str, template: str) -> str:
        normalized_title, normalized_markdown, normalized_template = self._normalize(
            title,
            markdown,
            template,
        )
        return _digest(
            "create",
            normalized_title,
            normalized_markdown,
            normalized_template,
        )

    def fingerprint_append(
        self,
        markdown: str,
        section_title: str,
        template: str,
    ) -> str:
        normalized_title, normalized_markdown, normalized_template = self._normalize(
            section_title or "追加内容",
            markdown,
            template,
        )
        return _digest(
            "append",
            normalized_title,
            normalized_markdown,
            normalized_template,
        )

    def render_create(
        self,
        *,
        title: str,
        markdown: str,
        template: str,
        operation_id: str,
        saved_at: datetime,
    ) -> RenderedNote:
        normalized_title, normalized_markdown, normalized_template = self._normalize(
            title,
            markdown,
            template,
        )
        content_hash = _digest(
            "create",
            normalized_title,
            normalized_markdown,
            normalized_template,
        )
        body = self._render_markdown(normalized_markdown, normalized_title)
        lead = self._lead(normalized_template, saved_at)
        footer = self._footer(operation_id, saved_at)
        return self._finish(
            normalized_title,
            f"<div><h1>{escape(normalized_title)}</h1>{lead}{body}{footer}</div>",
            content_hash,
        )

    def render_append(
        self,
        *,
        note_title: str,
        markdown: str,
        section_title: str,
        template: str,
        operation_id: str,
        saved_at: datetime,
    ) -> RenderedNote:
        normalized_section, normalized_markdown, normalized_template = self._normalize(
            section_title or "追加内容",
            markdown,
            template,
        )
        normalized_note_title = self._normalize_title(note_title)
        content_hash = _digest(
            "append",
            normalized_section,
            normalized_markdown,
            normalized_template,
        )
        body = self._render_markdown(normalized_markdown, normalized_section)
        footer = self._footer(operation_id, saved_at)
        html = (
            "<div><hr>" f"<h2>{escape(normalized_section)}</h2>" f"{body}{footer}</div>"
        )
        return self._finish(normalized_note_title, html, content_hash)

    def preview(self, *, title: str, markdown: str, template: str) -> RenderedNote:
        return self.render_create(
            title=title,
            markdown=markdown,
            template=template,
            operation_id="preview",
            saved_at=datetime.now().astimezone(),
        )

    def _normalize(
        self,
        title: str,
        markdown: str,
        template: str,
    ) -> tuple[str, str, str]:
        normalized_title = self._normalize_title(title)
        normalized_markdown = markdown.strip()
        if not normalized_markdown:
            raise ValueError("备忘录正文不能为空")
        if len(normalized_markdown) > self._config.max_markdown_characters:
            raise ValueError(
                "备忘录正文超过限制: "
                f"{len(normalized_markdown)} > "
                f"{self._config.max_markdown_characters}"
            )
        normalized_template = template.strip() or self._config.default_template
        if normalized_template not in _ALLOWED_TEMPLATES:
            raise ValueError(f"未知备忘录模板: {normalized_template}")
        return normalized_title, normalized_markdown, normalized_template

    @staticmethod
    def _normalize_title(title: str) -> str:
        normalized = " ".join(title.split())
        if not normalized:
            raise ValueError("备忘录标题不能为空")
        if len(normalized) > 180:
            raise ValueError("备忘录标题不能超过 180 个字符")
        return normalized

    def _render_markdown(self, markdown: str, title: str) -> str:
        rendered = self._markdown.render(markdown)
        soup = BeautifulSoup(rendered, "html.parser")
        first = next((item for item in soup.contents if isinstance(item, Tag)), None)
        if first is not None and first.name == "h1":
            if " ".join(first.get_text(" ", strip=True).split()) == title:
                first.decompose()
        self._sanitize(soup)
        return str(soup)

    @staticmethod
    def _sanitize(soup: BeautifulSoup) -> None:
        for tag in list(soup.find_all(True)):
            if tag.name not in _ALLOWED_TAGS:
                if tag.name in {"script", "style", "iframe", "object"}:
                    tag.decompose()
                else:
                    _ = tag.unwrap()
                continue
            if tag.name != "a":
                tag.attrs = {}
                continue
            href = str(tag.attrs.get("href") or "").strip()
            parsed = urlsplit(href)
            if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
                tag.attrs = {}
                continue
            tag.attrs = {"href": href}

    def _lead(self, template: str, saved_at: datetime) -> str:
        if template == "plain":
            return ""
        label = "🧭 链路知识卡" if template == "flow_chain" else "🧠 知识卡片"
        date_text = saved_at.astimezone().strftime("%Y-%m-%d")
        return f"<p><strong>{label}</strong> · {escape(date_text)}</p><hr>"

    def _footer(self, operation_id: str, saved_at: datetime) -> str:
        marker = f"AKASHIC_EXPORT:{operation_id}"
        saved_text = saved_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        return (
            "<hr><p><small>由 Akashic 保存 · "
            f"{escape(saved_text)} · {escape(marker)}</small></p>"
        )

    def _finish(self, title: str, html: str, content_hash: str) -> RenderedNote:
        html_bytes = len(html.encode("utf-8"))
        if html_bytes > self._config.max_html_bytes:
            raise ValueError(
                f"渲染后的 HTML 超过限制: {html_bytes} > "
                f"{self._config.max_html_bytes}"
            )
        plaintext = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return RenderedNote(
            title=title,
            html=html,
            plaintext=plaintext,
            content_hash=content_hash,
            html_bytes=html_bytes,
        )


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.hexdigest()
