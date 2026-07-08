"""
配置管理 + 加密工具
"""
import os
import base64
import logging
from pathlib import Path
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

KEY_FILE = DATA_DIR / ".encryption_key"
DB_PATH = DATA_DIR / "legal_mail.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_attachment_path(file_path: str) -> Path:
    """解析附件完整路径，兼容绝对路径和传统相对路径两种存储格式"""
    p = Path(file_path)
    if p.is_absolute():
        return p
    # 相对路径格式：attachments/account/date/file.docx
    return ATTACHMENTS_DIR.parent / p


def _generate_key() -> bytes:
    """生成纯随机加密密钥（Fernet.generate_key），存储到 KEY_FILE"""
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)
    logger.info("已生成新的加密密钥，请确保备份 %s", KEY_FILE)
    return key


def _load_key() -> bytes:
    """加载或生成加密密钥

    兼容旧版本：如果 KEY_FILE 存在且内容格式为 16字节salt + key（旧PBKDF2格式），
    自动迁移为纯 Fernet key 格式。
    """
    if KEY_FILE.exists():
        data = KEY_FILE.read_bytes()
        if len(data) >= 44:
            # ── 优先尝试新格式（纯 Fernet key，44 字节） ──
            # 先试原样，保护正确的新格式密钥不被误判为旧格式迁移所覆盖
            is_valid_new = False
            try:
                Fernet(data)
                is_valid_new = True
            except Exception:
                pass

            if is_valid_new:
                return data

            # ── 新格式验证失败，尝试旧格式迁移 ──
            # 旧格式总是 salt(16) + base64url_key(44) = 60+ bytes
            if len(data) >= 60:
                key_candidate = data[16:]
                # 旧格式的 key 部分必须精确为 44 字节（Fernet key 固定长度）
                if len(key_candidate) == 44:
                    try:
                        Fernet(key_candidate)
                        logger.info("检测到旧格式加密密钥（PBKDF2派生），已自动迁移")
                        KEY_FILE.write_bytes(key_candidate)
                        return key_candidate
                    except Exception:
                        pass

            # 新格式和旧格式均验证失败 → 文件损坏
            logger.critical(
                "加密密钥文件已损坏，所有已加密数据将无法恢复！"
                "如果这是首次启动，将自动生成新密钥。"
                "如有备份，请将 %s 替换为备份文件后重启。",
                KEY_FILE
            )
        else:
            # 文件太小，无效
            logger.critical("加密密钥文件无效，将生成新密钥（旧加密数据将无法恢复）")

    # 无密钥或密钥无效 → 生成新的
    return _generate_key()


# 全局加密器实例
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


# ========== 全局系统设置缓存 ==========

VERSION = "v1.0"
SYSTEM_NAME = "文书分发系统"
SYSTEM_PORT = "8020"


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


def set_system_port(port: str):
    """更新系统端口缓存"""
    global SYSTEM_PORT
    SYSTEM_PORT = port or "8020"
