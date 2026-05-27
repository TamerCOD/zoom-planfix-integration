"""
HTML formatting for PlanFix comments and descriptions.

PlanFix has these quirks:
  - \\n is REPLACED WITH SPACE on save
  - **markdown** is saved as literal text (not rendered)
  - <!-- HTML comments --> are STRIPPED
  - <br>, <p>, <b>, <i>, <a>, <ul>, <li>, <hr>, <code> all WORK

Use the builders here to avoid hand-crafting fragile HTML strings.
"""
from typing import Iterable, Union


def escape(text: str) -> str:
    """Escape special HTML chars in user input"""
    if text is None:
        return ""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def b(text: str) -> str:
    return f"<b>{escape(text)}</b>"


def i(text: str) -> str:
    return f"<i>{escape(text)}</i>"


def code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def link(url: str, label: str = None) -> str:
    label = label or url
    return f'<a href="{url}" target="_blank">{escape(label)}</a>'


def p(content: str) -> str:
    return f"<p>{content}</p>"


def br() -> str:
    return "<br>"


def hr() -> str:
    return "<hr>"


def ul(items: Iterable[str]) -> str:
    li_items = "".join(f"<li>{item}</li>" for item in items)
    return f"<ul>{li_items}</ul>"


def field(label: str, value: str) -> str:
    """One labeled field: '<b>Label:</b> value'"""
    return f"{b(label + ':')} {escape(str(value))}"


def section(title: str, content: str) -> str:
    """Title + content paragraph"""
    return p(b(title)) + p(content)


class Comment:
    """Fluent builder for PlanFix comments.

    Example:
        c = (Comment()
             .h("🎥 Zoom-конференция создана")
             .kv("Тема", topic)
             .kv("Время", time_str)
             .list("Участники", participant_names)
             .hr()
             .h("🔗 Подключение")
             .kv("Ссылка", link(join_url))
             .kv("Meeting ID", code(meeting_id))
             .build())
        pf.add_comment(task_id, c)
    """

    def __init__(self):
        self.parts: list[str] = []

    def h(self, title: str) -> "Comment":
        """Heading"""
        self.parts.append(p(b(title)))
        return self

    def text(self, html: str) -> "Comment":
        """Raw paragraph (HTML allowed)"""
        self.parts.append(p(html))
        return self

    def kv(self, label: str, value: Union[str, int]) -> "Comment":
        """Key-value line, joined with <br>"""
        self.parts.append(p(field(label, str(value))))
        return self

    def kv_block(self, pairs: list[tuple[str, str]]) -> "Comment":
        """Multiple key-value lines in one paragraph (separated by <br>)"""
        rows = "<br>".join(field(k, v) for k, v in pairs)
        self.parts.append(p(rows))
        return self

    def list(self, label: str, items: Iterable[str]) -> "Comment":
        items = list(items)
        if not items:
            return self
        self.parts.append(p(b(f"{label} ({len(items)}):")))
        self.parts.append(ul(escape(x) for x in items))
        return self

    def hr(self) -> "Comment":
        self.parts.append(hr())
        return self

    def note(self, html: str) -> "Comment":
        """Italic note"""
        self.parts.append(p(f"<i>{html}</i>"))
        return self

    def build(self) -> str:
        return "".join(self.parts)
