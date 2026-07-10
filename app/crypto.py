"""
加密工具 — Fernet 密钥管理、加密/解密
从 app.config 拆分而来
"""
import os
import logging
from pathlib import Path
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# ── 密钥路径（在 crypto 模块中使用绝对路径，避免循环依赖） ──
_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".encryption_key"


def _generate_key() -> bytes:
    """生成纯随机加密密钥（Fernet.generate_key），存储到 KEY_FILE"""
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except Exception:
        pass
    logger.info("已生成新的加密密钥，请确保备份 %s", _KEY_FILE)
    return key


def _load_key() -> bytes:
    """加载或生成加密密钥"""
    if _KEY_FILE.exists():
        data = _KEY_FILE.read_bytes()
        if len(data) >= 44:
            is_valid_new = False
            try:
                Fernet(data)
                is_valid_new = True
            except Exception:
                pass
            if is_valid_new:
                return data
            # 旧格式迁移：salt(16) + base64url_key(44)
            if len(data) >= 60:
                key_candidate = data[16:]
                if len(key_candidate) == 44:
                    try:
                        Fernet(key_candidate)
                        logger.info("检测到旧格式加密密钥（PBKDF2派生），已自动迁移")
                        _KEY_FILE.write_bytes(key_candidate)
                        return key_candidate
                    except Exception:
                        pass
            logger.critical(
                "加密密钥文件已损坏，所有已加密数据将无法恢复！"
                "如果这是首次启动，将自动生成新密钥。"
                "如有备份，请将 %s 替换为备份文件后重启。",
                _KEY_FILE
            )
        else:
            logger.critical("加密密钥文件无效，将生成新密钥（旧加密数据将无法恢复）")
    return _generate_key()


_fernet = Fernet(_load_key())


def reload_fernet():
    """重新加载加密密钥（当用户通过 Web UI 更换密钥文件后调用）"""
    global _fernet
    _fernet = Fernet(_load_key())
    logger.info("加密密钥已重新加载")


def encrypt(value: str) -> str:
    """加密字符串"""
    return _fernet.encrypt(value.encode()).decode()


def decrypt(encrypted: str) -> str:
    """解密字符串"""
    return _fernet.decrypt(encrypted.encode()).decode()
