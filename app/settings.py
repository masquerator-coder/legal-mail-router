"""
配置管理 — 路径定义、全局系统设置缓存
从 app.config 拆分而来
"""
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 项目目录 ──
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "legal_mail.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_attachment_path(file_path: str) -> Path:
    """解析附件完整路径，兼容绝对路径和传统相对路径两种存储格式"""
    p = Path(file_path)
    if p.is_absolute():
        return p
    return ATTACHMENTS_DIR.parent / p


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
