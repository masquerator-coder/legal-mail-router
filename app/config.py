"""
配置管理 + 加密工具
"""
import os
import base64
import logging
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

KEY_FILE = DATA_DIR / ".encryption_key"
DB_PATH = DATA_DIR / "legal_mail.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


def _generate_key() -> bytes:
    """生成加密密钥（基于机器信息 + 随机盐）"""
    import platform
    import uuid
    machine_id = f"{platform.node()}-{uuid.getnode()}".encode()
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(machine_id))
    # Store salt + key
    KEY_FILE.write_bytes(salt + key)
    KEY_FILE.chmod(0o600)
    return key


def _load_key() -> bytes:
    """加载或生成加密密钥"""
    if KEY_FILE.exists():
        data = KEY_FILE.read_bytes()
        _, key = data[:16], data[16:]
        return key
    return _generate_key()


# 全局加密器实例
_fernet = Fernet(_load_key())


def encrypt(value: str) -> str:
    """加密字符串"""
    return _fernet.encrypt(value.encode()).decode()


def decrypt(encrypted: str) -> str:
    """解密字符串"""
    return _fernet.decrypt(encrypted.encode()).decode()


# ========== 全局系统设置缓存 ==========

SYSTEM_NAME = "文书分发系统"
SYSTEM_PORT = "8888"


def load_system_settings():
    """从数据库加载系统设置到全局缓存（应用启动时调用）"""
    global SYSTEM_NAME, SYSTEM_PORT
    try:
        from app.database import SessionLocal
        from app.models import DefaultConfig
        db = SessionLocal()
        try:
            for cfg in db.query(DefaultConfig).filter(
                DefaultConfig.key.in_(["system_name", "system_port"])
            ).all():
                if cfg.key == "system_name" and cfg.value:
                    SYSTEM_NAME = cfg.value
                elif cfg.key == "system_port" and cfg.value:
                    SYSTEM_PORT = cfg.value
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to load system settings: %s", e)


def set_system_name(name: str):
    """更新系统名称缓存"""
    global SYSTEM_NAME
    SYSTEM_NAME = name or "文书分发系统"
