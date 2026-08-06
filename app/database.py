"""
SQLAlchemy 数据库引擎和会话管理
"""
import time
import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError, PendingRollbackError
from app.config import DB_PATH

logger = logging.getLogger(__name__)

MAX_DB_RETRIES = 3
DB_RETRY_DELAY = 1.0  # seconds

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite 多线程
    echo=False,
)

# Enable WAL mode for better concurrency
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30秒写入重试，防止定时任务与Web操作冲突
    cursor.close()


def db_retry_commit(db, max_retries: int = MAX_DB_RETRIES) -> None:
    """带重试的 commit，处理 SQLite 并发写入冲突"""
    for attempt in range(max_retries):
        try:
            db.commit()
            return
        except (OperationalError, PendingRollbackError, Exception) as e:
            is_locked = "database is locked" in str(e)
            is_pending_rollback = isinstance(e, PendingRollbackError) or "rolled back" in str(e)
            if (is_locked or is_pending_rollback) and attempt < max_retries - 1:
                logger.warning(
                    f"DB commit retry {attempt + 1}/{max_retries}: {e}"
                )
                # 必须先 rollback 清除会话的 pending rollback 状态，再重试
                try:
                    db.rollback()
                except Exception:
                    pass
                time.sleep(min(DB_RETRY_DELAY * (2 ** attempt), 8) + 0.1)
            else:
                raise


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _get_schema_version(conn) -> int:
    """从 PRAGMA user_version 读取当前 schema 版本号"""
    row = conn.execute(text("PRAGMA user_version")).fetchone()
    return row[0] if row else 0


def _set_schema_version(conn, version: int):
    conn.execute(text(f"PRAGMA user_version = {version}"))


def _migrate_doc_templates(conn):
    """自动迁移：为 doc_templates 表补齐新增的 account_id 列"""
    # 检查列是否存在
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(doc_templates)"))}
    if "account_id" not in cols:
        logger.info("迁移: doc_templates 添加 account_id 列")
        conn.execute(text(
            "ALTER TABLE doc_templates ADD COLUMN account_id INTEGER "
            "REFERENCES email_accounts(id) ON DELETE CASCADE"
        ))
        conn.commit()


def _migrate_routing_rules_account_ids(conn):
    """自动迁移：routing_rules.account_id (Integer FK) → account_ids (Text 逗号分隔)"""
    cols = {row[1]: row[2] for row in conn.execute(text("PRAGMA table_info(routing_rules)"))}

    if "account_ids" in cols:
        return  # 已迁移

    if "account_id" not in cols:
        return  # 旧列不存在，无需迁移

    logger.info("迁移: routing_rules 添加 account_ids 列并复制数据")
    # Step 1: 添加新列
    conn.execute(text("ALTER TABLE routing_rules ADD COLUMN account_ids TEXT DEFAULT ''"))

    # Step 2: 复制数据 (account_id → account_ids 文本化)
    conn.execute(text(
        "UPDATE routing_rules SET account_ids = CAST(account_id AS TEXT) "
        "WHERE account_id IS NOT NULL"
    ))

    # Step 3: 删除旧索引
    conn.execute(text("DROP INDEX IF EXISTS idx_routing_rules_account_id"))

    conn.commit()
    logger.info("迁移 routing_rules.account_ids 完成")


def _migrate_email_log_doc_types(conn):
    """自动迁移：为 email_logs 表补齐新增的 doc_types 列"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(email_logs)"))}
    if "doc_types" not in cols:
        logger.info("迁移: email_logs 添加 doc_types 列")
        conn.execute(text("ALTER TABLE email_logs ADD COLUMN doc_types TEXT"))
        conn.commit()


def _migrate_email_log_revision_instructions(conn):
    """自动迁移：为 email_logs 表补齐新增的 revision_instructions 列"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(email_logs)"))}
    if "revision_instructions" not in cols:
        logger.info("迁移: email_logs 添加 revision_instructions 列")
        conn.execute(text("ALTER TABLE email_logs ADD COLUMN revision_instructions TEXT"))
        conn.commit()


def _migrate_email_log_recipient(conn):
    """自动迁移：为 email_logs 表补齐新增的 recipient 列"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(email_logs)"))}
    if "recipient" not in cols:
        logger.info("迁移: email_logs 添加 recipient 列")
        conn.execute(text("ALTER TABLE email_logs ADD COLUMN recipient TEXT"))
        conn.commit()


def _migrate_email_account_forward_to(conn):
    """自动迁移：为 email_accounts 表补齐新增的 forward_to 列"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(email_accounts)"))}
    if "forward_to" not in cols:
        logger.info("迁移: email_accounts 添加 forward_to 列")
        conn.execute(text("ALTER TABLE email_accounts ADD COLUMN forward_to TEXT DEFAULT ''"))
        conn.commit()


def _migrate_ocr_config_capabilities(conn):
    """自动迁移：为 ocr_config 表补齐 connectivity_ok 和 pdf_capable 列"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(ocr_config)"))}
    if "connectivity_ok" not in cols:
        logger.info("迁移: ocr_config 添加 connectivity_ok 列")
        conn.execute(text("ALTER TABLE ocr_config ADD COLUMN connectivity_ok BOOLEAN"))
    if "pdf_capable" not in cols:
        logger.info("迁移: ocr_config 添加 pdf_capable 列")
        conn.execute(text("ALTER TABLE ocr_config ADD COLUMN pdf_capable BOOLEAN"))
    conn.commit()


def _migrate_llm_config_model_type_locked(conn):
    """自动迁移：为 llm_config 表补齐 model_type_locked 列（人工指定模型类型）"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(llm_config)"))}
    if "model_type_locked" not in cols:
        logger.info("迁移: llm_config 添加 model_type_locked 列")
        conn.execute(text(
            "ALTER TABLE llm_config ADD COLUMN model_type_locked BOOLEAN DEFAULT 0"
        ))
        conn.commit()


def get_db():
    """FastAPI 依赖：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """创建所有表并添加关键索引 + 自动迁移（基于 SQLite PRAGMA user_version）"""
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        current_version = _get_schema_version(conn)

        # ── 版本 1: 基础列迁移 ──
        if current_version < 1:
            _migrate_doc_templates(conn)
            _migrate_routing_rules_account_ids(conn)
            _migrate_email_log_doc_types(conn)
            _migrate_email_log_revision_instructions(conn)
            _migrate_email_account_forward_to(conn)
            _migrate_ocr_config_capabilities(conn)
            _set_schema_version(conn, 1)

        # ── 版本 2: email_logs 收件人列（幂等，兼容已升到 v1 的存量库） ──
        if current_version < 2:
            _migrate_email_log_recipient(conn)
            _set_schema_version(conn, 2)

        # ── 版本 3: llm_config 人工锁定模型类型 ──
        if current_version < 3:
            _migrate_llm_config_model_type_locked(conn)
            _set_schema_version(conn, 3)

        # 添加查询性能索引和 UNIQUE 约束（SQLite 用 IF NOT EXISTS 安全幂等）
        sqls = [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_logs_message_id ON email_logs(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_email_logs_created_at ON email_logs(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_email_logs_status ON email_logs(status)",
            "CREATE INDEX IF NOT EXISTS idx_email_logs_account_id ON email_logs(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_log_id ON attachments(log_id)",
            "CREATE INDEX IF NOT EXISTS idx_routing_rules_enabled_priority ON routing_rules(enabled, priority)",
            "CREATE INDEX IF NOT EXISTS idx_doc_templates_account_id ON doc_templates(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_doc_templates_doc_type ON doc_templates(doc_type)",
            "CREATE INDEX IF NOT EXISTS idx_doc_templates_is_default ON doc_templates(is_default)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_templates_global_unique ON doc_templates(doc_type) WHERE account_id IS NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_templates_account_type_unique ON doc_templates(account_id, doc_type) WHERE account_id IS NOT NULL",
        ]
        for sql in sqls:
            conn.execute(text(sql))
        conn.commit()
