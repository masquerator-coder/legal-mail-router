"""Flash 消息工具 — 基于 SessionMiddleware 的一次性提示"""

FLASH_KEY = "_flash_messages"


def flash(request, message: str, category: str = "success"):
    """添加一条 flash 消息（success / error / warning）"""
    session = request.session
    if FLASH_KEY not in session:
        session[FLASH_KEY] = []
    session[FLASH_KEY].append({"message": message, "category": category})


def get_flash_messages(request) -> list[dict]:
    """取出并清除所有 flash 消息（幂等：多次调用仅首次返回消息）"""
    if hasattr(request.state, "_flash_cache"):
        return request.state._flash_cache
    session = request.session
    messages = session.pop(FLASH_KEY, [])
    request.state._flash_cache = messages
    return messages
