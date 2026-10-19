"""Copy and flatten Slack Block Kit for message sync.

Events API ``text`` is a notification fallback. Long Block Kit posts (forms
and similar) often arrive with newlines stripped and the tail omitted. The
full body is in ``blocks``. ``Show more`` is a client chrome on those blocks,
not a second payload.

Interactive blocks (buttons, pickers) are dropped: they belong to the source
app and would be dead controls in the target.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from urllib.parse import urlparse

from logger import log_debug, log_warning

_CONTENT_BLOCK_TYPES = frozenset(
    {
        "section",
        "header",
        "context",
        "divider",
        "image",
        "rich_text",
        "markdown",
        "video",
    }
)
_SLACK_MAX_BLOCKS = 50
_SLACK_MAX_SECTION_TEXT = 3000
_SKIP_BLOCK_TYPES = frozenset(
    {
        "actions",
        "input",
        "file",
        "rich_text_input",
        "call",
        "table",
    }
)
_INTERACTIVE_ACCESSORY = frozenset(
    {
        "button",
        "overflow",
        "datepicker",
        "timepicker",
        "datetimepicker",
        "static_select",
        "multi_static_select",
        "external_select",
        "multi_external_select",
        "users_select",
        "multi_users_select",
        "conversations_select",
        "multi_conversations_select",
        "channels_select",
        "multi_channels_select",
        "checkboxes",
        "radio_buttons",
        "workflow_button",
    }
)
_BODY_BLOCK_TYPES = frozenset({"section", "header", "rich_text", "context", "markdown"})
_SLACK_HOSTED_MEDIA_HOSTS = frozenset({"files.slack.com", "slack-files.com"})


def is_slack_hosted_media_url(url: object) -> bool:
    """True when a Block Kit media URL is a Slack-hosted file the target cannot play."""
    if not isinstance(url, str) or not url.strip():
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in _SLACK_HOSTED_MEDIA_HOSTS:
        return True
    return any(host.endswith(f".{root}") for root in _SLACK_HOSTED_MEDIA_HOSTS)


def event_has_slack_hosted_media_blocks(event: dict | None) -> bool:
    """True when the event's Block Kit shows a Slack-hosted video or image."""
    for raw in get_event_layout_blocks(event or {}):
        if not isinstance(raw, dict):
            continue
        btype = raw.get("type")
        if btype == "image" and is_slack_hosted_media_url(raw.get("image_url")):
            return True
        if btype == "video" and is_slack_hosted_media_url(raw.get("video_url")):
            return True
        slack_file = raw.get("slack_file")
        if btype in {"image", "video", "file"} and isinstance(slack_file, dict) and slack_file.get("id"):
            return True
    return False


def get_event_layout_blocks(event: dict) -> list[dict]:
    """Return Block Kit from a message event, including ``message_changed``."""
    if not event:
        return []
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    blocks = event.get("blocks") or nested.get("blocks") or []
    return blocks if isinstance(blocks, list) else []


def blocks_include_body(blocks: list[dict] | None) -> bool:
    """True when *blocks* already carry the message body (do not prepend ``text``)."""
    return any(isinstance(b, dict) and b.get("type") in _BODY_BLOCK_TYPES for b in blocks or [])


def build_content_blocks_for_sync(blocks: list[dict] | None) -> list[dict]:
    """Copy layout blocks that can be re-posted; drop interactivity and private files."""
    out: list[dict] = []
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        btype = raw.get("type")
        if btype in _SKIP_BLOCK_TYPES or btype not in _CONTENT_BLOCK_TYPES:
            continue
        if btype == "image" and (not raw.get("image_url") or is_slack_hosted_media_url(raw.get("image_url"))):
            continue
        if btype == "video" and (not raw.get("video_url") or is_slack_hosted_media_url(raw.get("video_url"))):
            continue
        block = copy.deepcopy(raw)
        block.pop("block_id", None)
        accessory = block.get("accessory")
        if isinstance(accessory, dict) and accessory.get("type") in _INTERACTIVE_ACCESSORY:
            block.pop("accessory", None)
        out.append(block)
    if len(out) > _SLACK_MAX_BLOCKS:
        log_warning("content_blocks_trimmed", original=len(out), kept=_SLACK_MAX_BLOCKS)
        out = out[:_SLACK_MAX_BLOCKS]
    return out


def trim_target_blocks(blocks: list[dict] | None, *, limit: int = _SLACK_MAX_BLOCKS) -> list[dict]:
    """Cap target Block Kit at Slack's postMessage limit."""
    items = list(blocks or [])
    if len(items) <= limit:
        return items
    log_warning("target_blocks_trimmed", original=len(items), kept=limit)
    return items[:limit]


def clamp_section_text_in_blocks(blocks: list[dict] | None, *, limit: int = _SLACK_MAX_SECTION_TEXT) -> list[dict]:
    """Truncate section/header ``text.text`` fields that exceed Slack's limit."""
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict) and isinstance(text_obj.get("text"), str):
            raw = text_obj["text"]
            if len(raw) > limit:
                log_warning("section_text_clamped", original=len(raw), kept=limit, block_type=block.get("type"))
                text_obj["text"] = raw[: limit - 1] + "…"
    return blocks or []


def text_from_blocks(blocks: list[dict] | None) -> str:
    """Flatten content blocks to mrkdwn, preserving newlines between sections."""
    parts: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        chunk = _block_to_text(block)
        if chunk.strip():
            parts.append(chunk)
    return "\n".join(parts)


def choose_message_text(event_text: str | None, blocks: list[dict] | None) -> str:
    """Prefer Block Kit text when it is richer than Slack's ``event.text`` fallback."""
    fallback = (event_text or "").strip()
    from_blocks = text_from_blocks(blocks)
    if not from_blocks:
        return fallback or " "
    if not fallback:
        return from_blocks
    if "\n" in from_blocks and "\n" not in fallback:
        return from_blocks
    if len(from_blocks) > len(fallback):
        return from_blocks
    return fallback


def rewrite_content_blocks(
    blocks: list[dict],
    rewrite_mrkdwn: Callable[[str], str],
    map_user_id: Callable[[str], str | None],
    unmapped_user_label: Callable[[str], str],
) -> list[dict]:
    """Rewrite mentions inside copied blocks for the target workspace."""
    rewritten = [_rewrite_node(copy.deepcopy(b), rewrite_mrkdwn, map_user_id, unmapped_user_label) for b in blocks]
    clamp_section_text_in_blocks(rewritten)
    return rewritten


def collect_user_ids_from_blocks(blocks: list[dict] | None) -> list[str]:
    """User IDs from ``<@U…>`` mrkdwn and rich_text user elements."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(uid: str | None) -> None:
        if uid and uid not in seen:
            seen.add(uid)
            ids.append(uid)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "user":
                add(node.get("user_id"))
            text = node.get("text")
            if isinstance(text, str):
                for uid in re.findall(r"<@(\w+)>", text):
                    add(uid)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return ids


def _block_to_text(block: dict) -> str:
    btype = block.get("type")
    if btype in {"section", "header", "markdown"}:
        parts = [_text_obj_to_str(block.get("text"))]
        for field in block.get("fields") or []:
            parts.append(_text_obj_to_str(field))
        return "\n".join(p for p in parts if p)
    if btype == "context":
        bits = [_text_obj_to_str(el) for el in block.get("elements") or []]
        return " ".join(b for b in bits if b)
    if btype == "rich_text":
        return _rich_text_elements_to_str(block.get("elements") or [])
    if btype == "image":
        return block.get("alt_text") or block.get("title", {}).get("text") or ""
    if btype == "video":
        return block.get("title", {}).get("text") or block.get("alt_text") or ""
    return ""


def _text_obj_to_str(obj: object) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict) and (obj.get("type") in {"mrkdwn", "plain_text", "markdown"} or "text" in obj):
        return str(obj.get("text") or "")
    return ""


def _rich_text_elements_to_str(elements: list) -> str:
    parts: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        if etype == "text":
            parts.append(el.get("text") or "")
        elif etype == "emoji":
            parts.append(_emoji_to_str(el))
        elif etype == "user":
            uid = el.get("user_id")
            parts.append(f"<@{uid}>" if uid else "")
        elif etype == "channel":
            cid = el.get("channel_id")
            parts.append(f"<#{cid}>" if cid else "")
        elif etype in {"link", "message_mention"}:
            url = el.get("url") or ""
            label = el.get("text") or url
            parts.append(f"<{url}|{label}>" if label and label != url else url)
        elif etype == "broadcast":
            parts.append(f"<!{el.get('range') or 'channel'}>")
        elif etype in {"rich_text_section", "rich_text_quote", "rich_text_preformatted"}:
            parts.append(_rich_text_elements_to_str(el.get("elements") or []))
        elif etype == "rich_text_list":
            items = el.get("elements") or []
            for item in items:
                line = _rich_text_elements_to_str(item.get("elements") or [] if isinstance(item, dict) else [])
                if line:
                    parts.append(f"• {line}")
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
        elif "elements" in el:
            parts.append(_rich_text_elements_to_str(el.get("elements") or []))
    return "".join(parts)


_MRKDWN_LINK = re.compile(r"^<([^<>|]+)\|([^>]+)>$")
_MRKDWN_LINK_SPAN = re.compile(r"<([^<>|\s]+)\|([^<>]+)>")
_MRKDWN_CODE = re.compile(r"^`([^`]+)`$")


def is_url_display_text(display: str, url: str) -> bool:
    """True when link text is Slack's URL rendering, not author-chosen words."""
    shown = display.strip().rstrip("…").rstrip("/")
    if not shown:
        return True
    bare = url.strip().removeprefix("https://").removeprefix("http://")
    shown = shown.removeprefix("https://").removeprefix("http://")
    return ("/" in shown or "." in shown) and bare.lower().startswith(shown.lower())


def _text_element(text: str, style: dict | None) -> dict:
    out: dict = {"type": "text", "text": text}
    if style:
        out["style"] = style
    return out


def _link_element(url: str, text: str, style: dict | None) -> dict:
    out: dict = {"type": "link", "url": url, "text": text}
    if style:
        out["style"] = style
    return out


def _rewrite_link(
    url: str,
    display: str,
    rewrite_mrkdwn: Callable[[str], str],
    style: dict | None,
) -> dict | None:
    """Rebuild a rich_text ``link`` or ``message_mention`` as a target ``link``."""
    labeled = _MRKDWN_LINK.fullmatch((display or "").strip())
    if labeled:
        url = url or labeled.group(1)
        display = labeled.group(2)
    if not url:
        return None
    if is_url_display_text(display, url):
        rewritten = rewrite_mrkdwn(url)
        if rewritten != url:
            return _rich_text_from_mrkdwn_span(rewritten, style=style)
    return _link_element(url, display or url, style)


def _rich_text_from_mixed_text(text: str, style: dict | None = None) -> dict | list[dict]:
    """Split ``<url|label>`` mrkdwn out of a rich_text text node into real link elements."""
    parts: list[dict] = []
    pos = 0
    for match in _MRKDWN_LINK_SPAN.finditer(text):
        if match.start() > pos:
            parts.append(_text_element(text[pos : match.start()], style))
        parts.append(_link_element(match.group(1), match.group(2), style))
        pos = match.end()
    if not parts:
        return _text_element(text, style)
    if pos < len(text):
        parts.append(_text_element(text[pos:], style))
    return parts


def _rich_text_from_mrkdwn_span(mrkdwn: str, *, style: dict | None = None) -> dict:
    """Turn ``resolve_channel_references`` output into a rich_text ``link`` or code-ticked text."""
    s = (mrkdwn or "").strip()
    m = _MRKDWN_LINK.fullmatch(s)
    if m:
        return _link_element(m.group(1), m.group(2), style)
    code = _MRKDWN_CODE.fullmatch(s)
    if code:
        merged = dict(style or {})
        merged["code"] = True
        return {"type": "text", "text": code.group(1), "style": merged}
    return _text_element(mrkdwn, style)


def _emoji_to_str(el: dict) -> str:
    name = el.get("name") or ""
    uni = el.get("unicode")
    if isinstance(uni, str) and uni:
        try:
            return "".join(chr(int(part, 16)) for part in uni.replace(" ", "-").split("-") if part)
        except ValueError:
            log_debug("emoji_unicode_parse_failed", unicode=uni, emoji_name=name)
    return f":{name}:" if name else ""


def _rewrite_node(
    node: object,
    rewrite_mrkdwn: Callable[[str], str],
    map_user_id: Callable[[str], str | None],
    unmapped_user_label: Callable[[str], str],
) -> object:
    if isinstance(node, list):
        out_list: list = []
        for item in node:
            rewritten_item = _rewrite_node(item, rewrite_mrkdwn, map_user_id, unmapped_user_label)
            if isinstance(rewritten_item, list):
                out_list.extend(rewritten_item)
            else:
                out_list.append(rewritten_item)
        return out_list
    if not isinstance(node, dict):
        return node
    if node.get("type") == "user" and node.get("user_id"):
        mapped = map_user_id(node["user_id"])
        if mapped:
            out = dict(node)
            out["user_id"] = mapped
            return out
        return _rich_text_from_mrkdwn_span(
            unmapped_user_label(node["user_id"]),
            style=node.get("style") if isinstance(node.get("style"), dict) else None,
        )
    if node.get("type") == "channel" and node.get("channel_id"):
        return _rich_text_from_mrkdwn_span(
            rewrite_mrkdwn(f"<#{node['channel_id']}>"),
            style=node.get("style") if isinstance(node.get("style"), dict) else None,
        )
    style = node.get("style") if isinstance(node.get("style"), dict) else None
    if node.get("type") in {"link", "message_mention"}:
        url = node["url"] if isinstance(node.get("url"), str) else ""
        display = node["text"] if isinstance(node.get("text"), str) else ""
        rewritten = _rewrite_link(url, display, rewrite_mrkdwn, style)
        if rewritten:
            return rewritten
        cid = node.get("channel_id")
        if node.get("type") == "message_mention" and isinstance(cid, str) and cid:
            return _rich_text_from_mrkdwn_span(rewrite_mrkdwn(f"<#{cid}>"), style=style)
    if node.get("type") == "text" and isinstance(node.get("text"), str):
        return _rich_text_from_mixed_text(rewrite_mrkdwn(node["text"]), style=style)
    out: dict = {}
    for key, val in node.items():
        if key == "text" and isinstance(val, str):
            out[key] = rewrite_mrkdwn(val)
        else:
            out[key] = _rewrite_node(val, rewrite_mrkdwn, map_user_id, unmapped_user_label)
    return out
