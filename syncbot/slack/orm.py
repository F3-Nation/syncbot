import contextvars
import json
import re
from dataclasses import dataclass, field
from typing import Any

from helpers import get_team_id_from_body, safe_get
from helpers.slack_api import slack_error_code as _slack_error_code
from logger import log_debug, log_error
from slack.actions import MODAL_DENIED_CALLBACK

_MODAL_UPDATED: contextvars.ContextVar[bool] = contextvars.ContextVar("modal_updated", default=False)
_EXTERNAL_ID_SAFE = re.compile(r"[^A-Za-z0-9_]")


def build_modal_external_id(team_id: str, trigger_id: str) -> str:
    """Slack ``external_id`` for this click. Unique per team, at most 255 characters."""
    raw = f"{team_id}_{trigger_id}"
    return _EXTERNAL_ID_SAFE.sub("_", raw)[:255]


def reset_modal_updated() -> None:
    """Clear the work-phase flag before a modal handler runs."""
    _MODAL_UPDATED.set(False)


def modal_was_updated() -> bool:
    """True after this request's work phase called ``views.update``."""
    return _MODAL_UPDATED.get()


def _mark_modal_updated() -> None:
    _MODAL_UPDATED.set(True)


def open_or_push_view(
    client: Any,
    trigger_id: str,
    view: dict,
    *,
    new_or_add: str = "new",
) -> Any | None:
    """Open or push a Slack modal. Returns the Slack API response, or ``None`` on failure."""
    callback_id = view.get("callback_id") if isinstance(view, dict) else None
    try:
        if new_or_add == "add":
            return client.views_push(trigger_id=trigger_id, view=view)
        return client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:
        if _slack_error_code(e) == "duplicate_external_id":
            return None
        log_error("modal_open_or_push_failed", callback_id=callback_id, mode=new_or_add, error=str(e))
        log_debug("modal_view_payload", view=json.dumps(view, indent=2))
        return None


def _update_by_external_id(client: Any, external_id: str, view: dict) -> Any | None:
    """Replace the loading view. ``not_found`` is quiet."""
    payload = {**view, "external_id": external_id}
    try:
        result = client.views_update(external_id=external_id, view=payload)
    except Exception as exc:
        if _slack_error_code(exc) == "not_found":
            log_debug("modal_update_not_found")
            _mark_modal_updated()
            return None
        log_error("modal_update_failed", error=str(exc))
        log_debug("modal_view_payload", view=json.dumps(payload, indent=2))
        return None
    _mark_modal_updated()
    return result


def update_opened_view(client: Any, body: dict | None, trigger_id: str, view: dict) -> Any | None:
    """Replace the loading view for this click."""
    team_id = (get_team_id_from_body(body) if body else "") or ""
    return _update_by_external_id(client, build_modal_external_id(team_id, trigger_id), view)


def update_denied_modal(client: Any, body: dict | None, trigger_id: str) -> None:
    """Replace the loading view when the handler does not fill a modal."""
    update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "callback_id": MODAL_DENIED_CALLBACK,
            "title": {"type": "plain_text", "text": "SyncBot"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ":lock: You can't open that."},
                }
            ],
        },
    )


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

        Call this at render time from ``allow_private_channels(team_id)``. The
        filter is advisory; callers still validate the selected channel on submit.
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
        body: dict | None = None,
    ) -> Any | None:
        """Fill the loading modal opened for this ``trigger_id``."""
        team_id = (get_team_id_from_body(body) if body else "") or ""
        return self.update_modal(
            client,
            None,
            title_text,
            callback_id,
            submit_button_text=submit_button_text,
            parent_metadata=parent_metadata,
            close_button_text=close_button_text,
            notify_on_close=notify_on_close,
            external_id=build_modal_external_id(team_id, trigger_id),
        )

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
        view_id: str | None,
        title_text: str,
        callback_id: str,
        submit_button_text: str | None = "Submit",
        parent_metadata: dict = None,
        close_button_text: str = "Close",
        notify_on_close: bool = False,
        external_id: str | None = None,
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

        if external_id:
            return _update_by_external_id(client, external_id, view)
        client.views_update(view_id=view_id, view=view)
        return None

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
