import json
import logging
from dataclasses import dataclass, field
from typing import Any

from helpers import format_error_dm, get_user_id_from_body, safe_get
from helpers.slack_api import slack_error_code as _slack_error_code

logger = logging.getLogger(__name__)

_MODAL_EXPIRED_TRIGGER_DM = "SyncBot could not open that window in time. Please click the button again."


def _notify_expired_trigger(
    client: Any,
    exc: BaseException,
    body: dict | None,
    *,
    callback_id: str | None,
    mode: str,
) -> None:
    """DM the acting user when ``views.open`` lost the 3s trigger_id window."""
    code = _slack_error_code(exc)
    if code != "expired_trigger_id" and "expired_trigger_id" not in str(exc):
        return
    user_id = get_user_id_from_body(body) if body else None
    if not user_id:
        return
    action_id = safe_get(body, "actions", 0, "action_id") if body else None
    text = format_error_dm(
        _MODAL_EXPIRED_TRIGGER_DM,
        {
            "error": code or "expired_trigger_id",
            "window": callback_id,
            "open": "push" if mode == "add" else "open",
            "button": action_id,
        },
    )
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception as dm_exc:
        logger.warning("modal_open_timeout_dm_failed", extra={"error": str(dm_exc)})


def open_or_push_view(
    client: Any,
    trigger_id: str,
    view: dict,
    *,
    new_or_add: str = "new",
    body: dict | None = None,
) -> Any | None:
    """Open or push a Slack modal, logging and DMing on ``expired_trigger_id``.

    Returns the Slack API response on success, or ``None`` on failure.
    """
    callback_id = view.get("callback_id") if isinstance(view, dict) else None
    try:
        if new_or_add == "add":
            return client.views_push(trigger_id=trigger_id, view=view)
        return client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:
        logger.error(
            "modal_open_or_push_failed",
            extra={"callback_id": callback_id, "mode": new_or_add, "error": str(e)},
        )
        logger.debug("modal_view_payload", extra={"view": json.dumps(view, indent=2)})
        _notify_expired_trigger(client, e, body, callback_id=callback_id, mode=new_or_add)
        return None


@dataclass
class BaseElement:
    placeholder: str = None
    initial_value: str = None

    def make_placeholder_field(self):
        return {"placeholder": {"type": "plain_text", "text": self.placeholder, "emoji": True}}

    def get_selected_value(self, input_data, action):
        raise NotImplementedError


@dataclass
class BaseBlock:
    label: str = None
    action: str = None
    element: BaseElement = None

    def make_label_field(self, text=None):
        return {"type": "plain_text", "text": text or self.label or "", "emoji": True}

    def as_form_field(self, initial_value=None):
        raise Exception("Not Implemented")

    def get_selected_value(self, input_data, action):
        raise NotImplementedError


@dataclass
class BaseAction:
    label: str
    action: str = None

    def make_label_field(self, text=None):
        return {"type": "plain_text", "text": text or self.label, "emoji": True}

    def as_form_field(self, initial_value=None):
        raise Exception("Not Implemented")


@dataclass
class InputBlock(BaseBlock):
    optional: bool = True
    element: BaseElement = None
    dispatch_action: bool = False

    def get_selected_value(self, input_data):
        return self.element.get_selected_value(input_data, self.action)

    def as_form_field(self):
        block = {
            "type": "input",
            "block_id": self.action,
            "optional": self.optional,
            "label": self.make_label_field(),
        }
        block.update({"element": self.element.as_form_field(action=self.action)})
        if self.dispatch_action:
            block.update({"dispatch_action": True})
        return block


@dataclass
class SectionBlock(BaseBlock):
    element: BaseElement = None

    def get_selected_value(self, input_data, **kwargs):
        return self.element.get_selected_value(input_data, self.action, **kwargs)

    def as_form_field(self):
        block = {"type": "section", "text": self.make_label_field()}
        if self.action:
            block["block_id"] = self.action
        if self.element:
            block.update({"accessory": self.element.as_form_field(action=self.action)})
        return block

    def make_label_field(self, text=None):
        return {"type": "mrkdwn", "text": text or self.label or ""}


@dataclass
class HeaderBlock(BaseBlock):
    """A ``header`` block — renders as large bold text."""

    text: str = None

    def as_form_field(self):
        return {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": self.text or self.label or "",
                "emoji": True,
            },
        }


@dataclass
class ButtonElement(BaseAction):
    style: str = None
    value: str = None
    confirm: object = None
    url: str = None

    def as_form_field(self, action: str = None):
        j = {
            "type": "button",
            "text": self.make_label_field(),
            "action_id": self.action or action,
            "value": self.value or self.label,
        }
        if self.style:
            j["style"] = self.style
        if self.confirm:
            j["confirm"] = self.confirm
        if self.url:
            j["url"] = self.url
        return j


@dataclass
class SelectorOption:
    name: str
    value: str


def as_selector_options(names: list[str], values: list[str] | None = None) -> list[SelectorOption]:
    if values is None:
        selectors = [SelectorOption(name=x, value=x) for x in names]
    else:
        selectors = [SelectorOption(name=x, value=y) for x, y in zip(names, values)]
    return selectors


@dataclass
class StaticSelectElement(BaseElement):
    initial_value: str = None
    options: list[SelectorOption] = None

    def as_form_field(self, action: str):
        if not self.options:
            self.options = as_selector_options(["Default"])

        option_elements = [self.__make_option(o) for o in self.options]
        j = {"type": "static_select", "options": option_elements, "action_id": action}
        if self.placeholder:
            j.update(self.make_placeholder_field())

        initial_option = None
        if self.initial_value:
            initial_option = next((x for x in option_elements if x["value"] == self.initial_value), None)
            if initial_option:
                j["initial_option"] = initial_option
        return j

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_option", "value")

    def __make_option(self, option: SelectorOption):
        return {
            "text": {"type": "plain_text", "text": option.name, "emoji": True},
            "value": option.value,
        }


@dataclass
class RadioButtonsElement(BaseElement):
    initial_value: str = None
    options: list[SelectorOption] = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_option", "value")

    def as_form_field(self, action: str):
        if not self.options:
            self.options = as_selector_options(["Default"])

        option_elements = [self.__make_option(o) for o in self.options]
        j = {
            "type": "radio_buttons",
            "options": option_elements,
            "action_id": action,
        }

        initial_option = None
        if self.initial_value:
            initial_option = next((x for x in option_elements if x["value"] == self.initial_value), None)
            if initial_option:
                j["initial_option"] = initial_option
        return j

    def __make_option(self, option: SelectorOption):
        return {
            "text": {"type": "plain_text", "text": option.name, "emoji": True},
            "value": option.value,
        }


@dataclass
class MultiStaticSelectElement(BaseElement):
    """Multi-select over a fixed option list, for picking several values at once."""

    initial_values: list[str] = None
    options: list[SelectorOption] = None

    def get_selected_value(self, input_data, action):
        selected = safe_get(input_data, action, action, "selected_options") or []
        return [option.get("value") for option in selected if option.get("value")]

    def as_form_field(self, action: str):
        if not self.options:
            self.options = as_selector_options(["Default"])

        option_elements = [self.__make_option(o) for o in self.options]
        j = {"type": "multi_static_select", "options": option_elements, "action_id": action}
        if self.placeholder:
            j.update(self.make_placeholder_field())

        if self.initial_values:
            initial = [x for x in option_elements if x["value"] in self.initial_values]
            if initial:
                j["initial_options"] = initial
        return j

    def __make_option(self, option: SelectorOption):
        return {
            "text": {"type": "plain_text", "text": option.name, "emoji": True},
            "value": option.value,
        }


@dataclass
class PlainTextInputElement(BaseElement):
    initial_value: str = None
    multiline: bool = False
    max_length: int = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "value")

    def as_form_field(self, action: str):
        j = {
            "type": "plain_text_input",
            "action_id": action,
            "initial_value": self.initial_value or "",
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.multiline:
            j["multiline"] = True
        if self.max_length:
            j["max_length"] = self.max_length
        return j


@dataclass
class NumberInputElement(BaseElement):
    initial_value: float = None
    min_value: float = None
    max_value: float = None
    is_decimal_allowed: bool = True

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "value")

    def as_form_field(self, action: str):
        j = {
            "type": "number_input",
            "action_id": action,
            "is_decimal_allowed": self.is_decimal_allowed,
        }
        if self.initial_value:
            j["initial_value"] = str(self.initial_value)
        if self.min_value:
            j["min_value"] = str(self.min_value)
        if self.max_value:
            j["max_value"] = str(self.max_value)
        return j


@dataclass
class ChannelsSelectElement(BaseElement):
    initial_value: str = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_channel")

    def as_form_field(self, action: str):
        j = {
            "type": "channels_select",
            "action_id": action,
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.initial_value:
            j["initial_channel"] = self.initial_value
        return j


@dataclass
class ConversationsSelectElement(BaseElement):
    """Slack's native channel picker, searchable over all of the user's conversations.

    Unlike a ``static_select`` populated from ``conversations_list``, this has no
    app-side enumeration and therefore no option cap, so it works in workspaces
    with thousands of channels.

    ``include_private`` only controls the client-side filter, which is advisory:
    the payload can still name a private channel, so callers must also validate
    on submit. It defaults to ``False`` to match the default of the
    ``allow_private_channels`` setting; set it from that setting at render time.
    """

    initial_value: str = None
    include_private: bool = False

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_conversation")

    def as_form_field(self, action: str):
        j = {
            "type": "conversations_select",
            "action_id": action,
            "filter": {
                "include": ["public", "private"] if self.include_private else ["public"],
                "exclude_bot_users": True,
                "exclude_external_shared_channels": True,
            },
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.initial_value:
            j["initial_conversation"] = self.initial_value
        return j


@dataclass
class DatepickerElement(BaseElement):
    initial_value: str = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_date")

    def as_form_field(self, action: str):
        j = {
            "type": "datepicker",
            "action_id": action,
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.initial_value:
            j["initial_date"] = self.initial_value
        return j


@dataclass
class TimepickerElement(BaseElement):
    initial_value: str = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_time")

    def as_form_field(self, action: str):
        j = {
            "type": "timepicker",
            "action_id": action,
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.initial_value:
            j["initial_time"] = self.initial_value
        return j


@dataclass
class UsersSelectElement(BaseElement):
    initial_value: str = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_user")

    def as_form_field(self, action: str):
        j = {
            "type": "users_select",
            "action_id": action,
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.initial_value:
            j["initial_user"] = self.initial_value
        return j


@dataclass
class MultiUsersSelectElement(BaseElement):
    initial_value: list[str] = None

    def get_selected_value(self, input_data, action):
        return safe_get(input_data, action, action, "selected_users")

    def as_form_field(self, action: str):
        j = {
            "type": "multi_users_select",
            "action_id": action,
        }
        if self.placeholder:
            j.update(self.make_placeholder_field())
        if self.initial_value:
            j["initial_users"] = self.initial_value
        return j


@dataclass
class ContextBlock(BaseBlock):
    element: BaseElement = None
    elements: list = None
    initial_value: str = ""

    def get_selected_value(self, input_data, action):
        for block in input_data:
            if block["block_id"] == action:
                return block["elements"][0]["text"]
        return None

    def as_form_field(self):
        j = {"type": "context"}
        if self.elements:
            j["elements"] = [e.as_form_field() for e in self.elements]
        elif self.element:
            j["elements"] = [self.element.as_form_field()]
        if self.action:
            j["block_id"] = self.action
        return j


@dataclass
class ImageContextElement(BaseElement):
    """An image element for use inside a ContextBlock."""

    image_url: str = None
    alt_text: str = "icon"

    def as_form_field(self):
        return {
            "type": "image",
            "image_url": self.image_url,
            "alt_text": self.alt_text,
        }


@dataclass
class ImageAccessoryElement(BaseElement):
    """An image element for use as a SectionBlock accessory."""

    image_url: str = None
    alt_text: str = "icon"

    def as_form_field(self, action: str = None):
        return {
            "type": "image",
            "image_url": self.image_url,
            "alt_text": self.alt_text,
        }


@dataclass
class ContextElement(BaseElement):
    initial_value: str = None

    def as_form_field(self):
        j = {
            "type": "mrkdwn",
            "text": self.initial_value,
        }
        return j


@dataclass
class DividerBlock(BaseBlock):
    def as_form_field(self):
        return {"type": "divider"}


@dataclass
class ActionsBlock(BaseBlock):
    elements: list[BaseAction] = field(default_factory=list)

    def as_form_field(self):
        j = {
            "type": "actions",
            "elements": [e.as_form_field() for e in self.elements],
        }
        if self.action:
            j["block_id"] = self.action
        return j


@dataclass
class BlockView:
    blocks: list[BaseBlock]

    def delete_block(self, action: str):
        self.blocks = [b for b in self.blocks if b.action != action]

    def add_block(self, block: BaseBlock):
        self.blocks.append(block)

    def set_initial_values(self, values: dict):
        for block in self.blocks:
            if block.action in values:
                block.element.initial_value = values[block.action]

    def set_options(self, options: dict[str, list[SelectorOption]]):
        for block in self.blocks:
            if block.action in options:
                block.element.options = options[block.action]

    def set_conversations_include_private(self, include_private: bool):
        """Apply the private-channel policy to every conversations picker in this view.

        The form templates in :mod:`slack.forms` are module-level constants, so the
        policy cannot be baked into them at import time — it would go stale as soon
        as an operator changed the setting. Call this on the deep copy instead.
        """
        for block in self.blocks:
            element = getattr(block, "element", None)
            if isinstance(element, ConversationsSelectElement):
                element.include_private = include_private

    def as_form_field(self) -> list[dict]:
        return [b.as_form_field() for b in self.blocks]

    def get_selected_values(self, body) -> dict:
        values = body["view"]["state"]["values"]
        view_blocks = body["view"]["blocks"]

        selected_values = {}
        for block in self.blocks:
            if isinstance(block, InputBlock):
                selected_values[block.action] = block.get_selected_value(values)
            elif isinstance(block, ContextBlock) and block.action:
                selected_values[block.action] = block.get_selected_value(view_blocks, block.action)

        return selected_values

    def post_modal(
        self,
        client: Any,
        trigger_id: str,
        title_text: str,
        callback_id: str,
        submit_button_text: str | None = "Submit",
        parent_metadata: dict = None,
        close_button_text: str = "Close",
        notify_on_close: bool = False,
        new_or_add: str = "new",
        body: dict | None = None,
    ) -> Any | None:
        """Open or push this form as a modal. Returns the Slack API response or ``None``."""
        blocks = self.as_form_field()

        view = {
            "type": "modal",
            "callback_id": callback_id,
            "title": {"type": "plain_text", "text": title_text},
            "close": {"type": "plain_text", "text": close_button_text},
            "notify_on_close": notify_on_close,
            "blocks": blocks,
        }
        if parent_metadata:
            view["private_metadata"] = json.dumps(parent_metadata)

        if submit_button_text:
            view["submit"] = {"type": "plain_text", "text": submit_button_text}

        return open_or_push_view(client, trigger_id, view, new_or_add=new_or_add, body=body)

    def publish_home_tab(self, client: Any, user_id: str):
        """Publish a Home tab view for the given user."""
        blocks = self.as_form_field()
        client.views_publish(
            user_id=user_id,
            view={"type": "home", "blocks": blocks},
        )

    def update_modal(
        self,
        client: Any,
        view_id: str,
        title_text: str,
        callback_id: str,
        submit_button_text: str | None = "Submit",
        parent_metadata: dict = None,
        close_button_text: str = "Close",
        notify_on_close: bool = False,
    ):
        blocks = self.as_form_field()

        view = {
            "type": "modal",
            "callback_id": callback_id,
            "title": {"type": "plain_text", "text": title_text},
            "close": {"type": "plain_text", "text": close_button_text},
            "notify_on_close": notify_on_close,
            "blocks": blocks,
        }
        if submit_button_text:
            view["submit"] = {"type": "plain_text", "text": submit_button_text}
        if parent_metadata:
            view["private_metadata"] = json.dumps(parent_metadata)

        client.views_update(view_id=view_id, view=view)

    def as_ack_update(
        self,
        title_text: str,
        callback_id: str,
        submit_button_text: str = "Submit",
        parent_metadata: dict = None,
        close_button_text: str = "Close",
    ) -> dict:
        """Build a modal view dict suitable for ack(response_action="update")."""
        blocks = self.as_form_field()
        view: dict = {
            "type": "modal",
            "callback_id": callback_id,
            "title": {"type": "plain_text", "text": title_text},
            "close": {"type": "plain_text", "text": close_button_text},
            "blocks": blocks,
        }
        if submit_button_text != "None":
            view["submit"] = {"type": "plain_text", "text": submit_button_text}
        if parent_metadata:
            view["private_metadata"] = json.dumps(parent_metadata)
        return view


@dataclass
class ImageBlock(BaseBlock):
    image_url: str = None
    alt_text: str = None

    def as_form_field(self):
        j = {
            "type": "image",
            "image_url": self.image_url,
            "alt_text": self.alt_text,
        }
        if self.action:
            j["block_id"] = self.action
        if self.label:
            j["title"] = self.make_label_field()
        return j
