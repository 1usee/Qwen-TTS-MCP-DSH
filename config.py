"""Leaf constants for qwen-voice-mcp.

Dependency rule (spec §4): this module is a **leaf** — it must not import any
other module from this project. Everything here is a literal default that the
higher layers (`core.*`, `tools.*`, `gui.*`) consume.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# DashScope endpoints
# --------------------------------------------------------------------------
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com"

#: Balance probe chain, tried in order until one yields a parsable balance.
BALANCE_ENDPOINTS = [
    f"{DASHSCOPE_BASE_URL}/api/v1/account/quota",
    f"{DASHSCOPE_BASE_URL}/api/v1/billing/query",
    f"{DASHSCOPE_BASE_URL}/api/v1/account/balance",
]

RECHARGE_URL = "https://bailian.console.aliyun.com/"

# --------------------------------------------------------------------------
# Server defaults
# --------------------------------------------------------------------------
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "cosyvoice-v2"
DEFAULT_VOICE = "longanyang"
DEFAULT_OUTPUT_FORMAT = "mp3"

# --------------------------------------------------------------------------
# Balance gate defaults
# --------------------------------------------------------------------------
BALANCE_THRESHOLD = 1.00
STRICT_BALANCE = False
BALANCE_TIMEOUT_SECONDS = 8.0

# --------------------------------------------------------------------------
# Audio reference defaults
# --------------------------------------------------------------------------
AUDIO_MIN_SECONDS = 5.0
AUDIO_MAX_SECONDS = 60.0
AUDIO_MAX_BYTES = 20 * 1024 * 1024
AUDIO_ALLOWED_EXTENSIONS = ("wav", "mp3", "m4a", "flac", "ogg", "aac")

# --------------------------------------------------------------------------
# Billing
# --------------------------------------------------------------------------
#: Flat CNY unit price for one voice-cloning request.
CLONE_UNIT_PRICE = 0.20

LOG_LEVEL = "INFO"

# --------------------------------------------------------------------------
# Configuration plumbing
# --------------------------------------------------------------------------
CONFIG_HOME_ENV = "QWEN_VOICE_HOME"
ENV_PREFIX = "QWEN_VOICE_"
API_KEY_ENV = "DASHSCOPE_API_KEY"
KEYRING_SERVICE = "qwen-voice-mcp"
KEYRING_USERNAME = "vault-master-key"

# --------------------------------------------------------------------------
# Built-in model catalogue
# --------------------------------------------------------------------------
#: Declared — **not** verified — model catalogue (spec §3, deviation D5).
#: ``pricing_per_1k_chars`` is the declared CNY price per 1,000 characters and
#: feeds ``ModelRegistry.estimated_cost``. ``verified`` stays ``None`` until a
#: live ``--verify-models`` probe annotates it.
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "name": "cosyvoice-v2",
        "type": "voice_cloning_and_tts",
        "description": "CosyVoice v2 语音合成与声音克隆模型，音色自然、情感表现力强，支持中文及多语种。",
        "capabilities": ["voice_clone", "text_to_speech"],
        "pricing": "约 ¥0.26 / 千字符（声音克隆另计 ¥0.20 / 次）",
        "pricing_per_1k_chars": 0.26,
    },
    {
        "name": "cosyvoice-v1",
        "type": "voice_cloning_and_tts",
        "description": "CosyVoice v1 语音合成与声音克隆模型，稳定版，适合对成本与效果折中的场景。",
        "capabilities": ["voice_clone", "text_to_speech"],
        "pricing": "约 ¥0.26 / 千字符（声音克隆另计 ¥0.20 / 次）",
        "pricing_per_1k_chars": 0.26,
    },
    {
        "name": "qwen-tts",
        "type": "text_to_speech",
        "description": "通义千问 TTS 模型，支持多音色与多语种朗读，不支持声音克隆。",
        "capabilities": ["text_to_speech"],
        "pricing": "约 ¥0.20 / 千字符",
        "pricing_per_1k_chars": 0.20,
    },
    {
        "name": "sambert-zhichu-v1",
        "type": "text_to_speech",
        "description": "Sambert 知初中文女声合成模型，成本最低，适合大批量中文朗读。",
        "capabilities": ["text_to_speech"],
        "pricing": "约 ¥0.10 / 千字符",
        "pricing_per_1k_chars": 0.10,
    },
]
