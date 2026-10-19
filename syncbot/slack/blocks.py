"""Block Kit shorthand constructors.

Thin wrappers around :mod:`slack.orm` dataclasses that collapse the most
common 5-10 line patterns into single function calls.  Every function
returns an ``orm`` object, so they compose naturally with
:class:`~slack.orm.BlockView` and the existing dataclass API.

Usage::

    from slack.blocks import header, divider, context, text, button, actions

    blocks = [
        header("SyncBot Configuration"),
        actions(button(":arrows_counterclockwise: Refresh", action=CONFIG_REFRESH_HOME)),
        divider(),
        context(":lock: This area of SyncBot is limited to Workspace Admins."),
    ]
"""

from slack import orm


def header(label: str) -> orm.HeaderBlock:
    """Large bold header text."""
    return orm.HeaderBlock(text=label)


def divider() -> orm.DividerBlock:
    """Horizontal divider line."""
    return orm.DividerBlock()


def context(label: str) -> orm.ContextBlock:
    """Mrkdwn context block (small grey text)."""
    return orm.ContextBlock(element=orm.ContextElement(initial_value=label))


def text(label: str) -> orm.SectionBlock:
    """Mrkdwn section block (body text)."""
    return orm.SectionBlock(label=label)


# Alias for section-style usage (SectionBlock with label only).
section = text


def button(
    label: str,
    action: str,
    *,
    value: str | None = None,
    style: str | None = None,
    confirm: object = None,
    url: str | None = None,
) -> orm.ButtonElement:
    """Button element for use inside :func:`actions`."""
    return orm.ButtonElement(
        label=label,
        action=action,
        value=value or label,
        style=style,
        confirm=confirm,
        url=url,
    )


def actions(*elements: orm.ButtonElement) -> orm.ActionsBlock:
    """Actions block containing one or more buttons."""
    return orm.ActionsBlock(elements=list(elements))


def section_with_image(
    label: str,
    image_url: str | None,
    alt_text: str = "icon",
) -> orm.SectionBlock:
    """Section block with an optional image accessory.

    If *image_url* is falsy, returns a plain section block.
    """
    if image_url:
        return orm.SectionBlock(
            label=label,
            element=orm.ImageAccessoryElement(image_url=image_url, alt_text=alt_text),
        )
    return orm.SectionBlock(label=label)


def workspace_card(
    label: str,
    ws_info: dict,
    ws_name: str,
) -> orm.SectionBlock:
    """Section block showing workspace info with an optional team icon."""
    return section_with_image(label, ws_info.get("icon_url"), ws_name)
