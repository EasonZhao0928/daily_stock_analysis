# -*- coding: utf-8 -*-
"""
===================================
平台适配器模块
===================================

包含各平台的 Webhook 处理和消息解析逻辑。

支持两种接入模式：
1. Webhook 模式：需要公网 IP，配置回调 URL
2. Stream 模式：无需公网 IP，通过 WebSocket 长连接（钉钉、飞书支持）
"""

from bot.platforms.base import BotPlatform
from bot.platforms.dingtalk import DingtalkPlatform

# 所有可用平台（Webhook 模式）
ALL_PLATFORMS = {
    'dingtalk': DingtalkPlatform,
}

# 钉钉 Stream 模式（可选）
try:
    from bot.platforms.dingtalk_stream import (
        DingtalkStreamClient,
        DingtalkStreamHandler,
        get_dingtalk_stream_client,
        start_dingtalk_stream_background,
        DINGTALK_STREAM_AVAILABLE,
    )
except ImportError:
    DINGTALK_STREAM_AVAILABLE = False
    DingtalkStreamClient = None
    DingtalkStreamHandler = None
    get_dingtalk_stream_client = lambda: None
    start_dingtalk_stream_background = lambda: False

# 飞书 Stream 模式（可选）
try:
    from bot.platforms.feishu_stream import (
        FeishuStreamClient,
        FeishuStreamHandler,
        FeishuReplyClient,
        get_feishu_stream_client,
        start_feishu_stream_background,
        FEISHU_SDK_AVAILABLE,
    )
except ImportError:
    FEISHU_SDK_AVAILABLE = False
    FeishuStreamClient = None
    FeishuStreamHandler = None
    FeishuReplyClient = None
    get_feishu_stream_client = lambda: None
    start_feishu_stream_background = lambda: False

# Personal WeChat iLink channel (feature-flagged; no SDK import required).
try:
    from bot.platforms.wechat_ilink import (
        FakeILinkServer,
        WeChatChannelAdapter,
        WeChatILinkClient,
        parse_ilink_messages,
    )
except ImportError:
    FakeILinkServer = None
    WeChatChannelAdapter = None
    WeChatILinkClient = None
    parse_ilink_messages = None

__all__ = [
    'BotPlatform',
    'DingtalkPlatform',
    'ALL_PLATFORMS',
    # 钉钉 Stream 模式
    'DingtalkStreamClient',
    'DingtalkStreamHandler',
    'get_dingtalk_stream_client',
    'start_dingtalk_stream_background',
    'DINGTALK_STREAM_AVAILABLE',
    # 飞书 Stream 模式
    'FeishuStreamClient',
    'FeishuStreamHandler',
    'FeishuReplyClient',
    'get_feishu_stream_client',
    'start_feishu_stream_background',
    'FEISHU_SDK_AVAILABLE',
    # Personal WeChat iLink channel
    'WeChatChannelAdapter',
    'WeChatILinkClient',
    'FakeILinkServer',
    'parse_ilink_messages',
]
