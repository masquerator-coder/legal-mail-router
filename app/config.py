"""
配置管理 + 加密工具（兼容转发层）

此文件为兼容导入而保留，具体实现在:
  - app/crypto.py:  加密/解密 (encrypt, decrypt, reload_fernet)
  - app/settings.py: 路径配置、系统设置 (BASE_DIR, SYSTEM_NAME 等)

新代码应直接从 app.crypto 或 app.settings 导入，无需经过此层。
"""
# ── 从 crypto 转发 ──
from app.crypto import encrypt, decrypt, reload_fernet  # noqa: F401

# ── 从 settings 转发 ──
from app.settings import (  # noqa: F401
    BASE_DIR, DATA_DIR, DB_PATH, ATTACHMENTS_DIR,
    resolve_attachment_path,
    VERSION, SYSTEM_NAME, SYSTEM_PORT,
    load_system_settings, set_system_name, set_system_port,
)

# 注：KEY_FILE 由 crypto 管理（位于 DATA_DIR / ".encryption_key"）
# 此处从 settings 重新导出以保持兼容性
KEY_FILE = BASE_DIR / "data" / ".encryption_key"
