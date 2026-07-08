"""
已知 LLM 模型的上下文窗口大小配置。

这个文件是配置文件，用户可以在此添加或修改已知模型的上下文窗口大小。
当 `get_effective_context_window()` 自动探测失败时，这里的数据作为第一后备。

格式：{"模型名关键词": 上下文窗口大小 (tokens)}
匹配方式：不区分大小写，模型名中包含关键词即匹配（如 "gpt-4o" 匹配
"gpt-4o-2024-08-06" 等变体）。

新模型添加方式：
  1. 在此字典中添加一行，格式如 "模型名（或关键词）": 窗口大小
  2. 无需重启服务（仅在首次探测时加载）
"""

# 已知模型的上下文窗口（用于自动探测时的精确匹配）
KNOWN_MODEL_WINDOWS: dict[str, int] = {
    # ---------- OpenAI ----------
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8192,
    "gpt-4-32k": 32768,
    "gpt-3.5-turbo": 16385,
    "gpt-3.5-turbo-16k": 16385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
    # ---------- Anthropic ----------
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3.5-sonnet": 200_000,
    "claude-3.5-haiku": 200_000,
    "claude-4-sonnet": 200_000,
    # ---------- DeepSeek ----------
    "deepseek-v3": 131_072,
    "deepseek-v4": 131_072,
    "deepseek-r1": 131_072,
    "deepseek-chat": 131_072,
    "deepseek-coder": 131_072,
    "deepseek-reasoner": 131_072,
    # ---------- Google ----------
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-pro": 2_097_152,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    # ---------- Qwen (通义千问) ----------
    "qwen-max": 32768,
    "qwen-plus": 131_072,
    "qwen-turbo": 131_072,
    "qwen3": 131_072,
    "qwen2.5": 131_072,
    "qwq": 131_072,
    # ---------- GLM (智谱) ----------
    "glm-4": 128_000,
    "glm-4-plus": 128_000,
    "glm-4-flash": 128_000,
    "glm-4v": 128_000,
    # ---------- Moonshot / Kimi ----------
    "moonshot-v1": 131_072,
    "kimi": 131_072,
    # ---------- Yi (零一万物) ----------
    "yi-large": 32768,
    "yi-medium": 16384,
    # ---------- Mistral ----------
    "mistral-large": 131_072,
    "mistral-medium": 32768,
    "mistral-small": 32768,
}

# 自动探测失败时的回退值（128K）
DEFAULT_CONTEXT_WINDOW: int = 131_072
