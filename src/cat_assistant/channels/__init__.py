"""Inbound/outbound chat channel adapters.

The channel layer deliberately knows nothing about models or tools.  It only
translates platform events to :class:`Utterance` and delivers text replies.
"""

from cat_assistant.channels.base import ChannelMessage, MessageChannel
from cat_assistant.channels.bridge import ChannelBridge
from cat_assistant.channels.onebot import OneBotV11Channel
from cat_assistant.channels.wechat import WeChatWebhookChannel
from cat_assistant.channels.server import WebhookServer
from cat_assistant.channels.telegram import TelegramChannel
from cat_assistant.channels.discord import DiscordChannel
from cat_assistant.channels.slack import SlackChannel
from cat_assistant.channels.feishu import FeishuChannel
from cat_assistant.channels.dingtalk import DingTalkChannel

__all__ = [
    "ChannelMessage",
    "MessageChannel",
    "ChannelBridge",
    "OneBotV11Channel",
    "WeChatWebhookChannel",
    "WebhookServer",
    "TelegramChannel", "DiscordChannel", "SlackChannel", "FeishuChannel", "DingTalkChannel",
]
