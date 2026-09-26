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
    """带重试的 commit，处理 SQLite 并发写入冲突

    注意：commit 失败后 rollback 会丢弃会话中未提交的变更；
    若重试时变更已随 rollback 丢失，必须抛出异常而不是静默报告成功，
    否则调用方会以为数据已持久化。
    """
    had_pending = bool(db.new or db.dirty or db.deleted)
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
                # rollback 已丢弃未提交变更：此时重试提交的是空事务，
                # 若之前确有 pending 变更，必须报错让调用方整体重做
                if had_pending and not (db.new or db.dirty or db.deleted):
                    raise RuntimeError(
                        "数据库锁冲突，本次未提交的变更已随 rollback 丢失"
                        f"（首次错误: {e}），请重试整个操作"
                    ) from e
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


def _migrate_llm_config_role(conn):
    """自动迁移：为 llm_config 表补齐 config_role 列（analyzer=文书分析 / classifier=文书类型识别）"""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(llm_config)"))}
    if "config_role" not in cols:
        logger.info("迁移: llm_config 添加 config_role 列")
        conn.execute(text(
            "ALTER TABLE llm_config ADD COLUMN config_role VARCHAR(20) DEFAULT 'analyzer'"
        ))
        conn.commit()


def _migrate_routing_rules_rule_type(conn):
    """自动迁移：为 routing_rules 表补齐 rule_type 列（account=按邮箱 / doc_type=按文书类型）

    存量规则一律视为按邮箱匹配，保证升级后行为不变；doc_type 列早已存在（历史遗留），
    本版本起正式启用为类型规则的匹配内容。
    """
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(routing_rules)"))}
    if "rule_type" not in cols:
        logger.info("迁移: routing_rules 添加 rule_type 列（存量规则默认按邮箱匹配）")
        conn.execute(text(
            "ALTER TABLE routing_rules ADD COLUMN rule_type VARCHAR(20) DEFAULT 'account'"
        ))
        # 显式回填，兼容 DEFAULT 未生效的历史行（NULL → account）
        conn.execute(text(
            "UPDATE routing_rules SET rule_type = 'account' "
            "WHERE rule_type IS NULL OR rule_type = ''"
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

        # ── 版本 4: 移除已废弃的 doc_templates 表（DocTemplate 功能已清理） ──
        if current_version < 4:
            conn.execute(text("DROP TABLE IF EXISTS doc_templates"))
            _set_schema_version(conn, 4)

        # ── 版本 5: llm_config 用途字段（两阶段分析: 文书分析/类型识别） ──
        if current_version < 5:
            _migrate_llm_config_role(conn)
            _set_schema_version(conn, 5)

        # ── 版本 6: routing_rules 匹配方式（按邮箱 / 按文书类型） ──
        if current_version < 6:
            _migrate_routing_rules_rule_type(conn)
            _set_schema_version(conn, 6)

        # 添加查询性能索引和 UNIQUE 约束（SQLite 用 IF NOT EXISTS 安全幂等）
        sqls = [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_logs_message_id ON email_logs(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_email_logs_created_at ON email_logs(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_email_logs_status ON email_logs(status)",
            "CREATE INDEX IF NOT EXISTS idx_email_logs_account_id ON email_logs(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_log_id ON attachments(log_id)",
            "CREATE INDEX IF NOT EXISTS idx_routing_rules_enabled_priority ON routing_rules(enabled, priority)",
        ]
        for sql in sqls:
            conn.execute(text(sql))
        conn.commit()
