"""
Database — schema 自动迁移测试

注意：导入 app.database 会触发模块级副作用（app.settings 创建 data/ 目录、
app.crypto 可能生成 .encryption_key），但本测试使用独立内存库，不写入任何 DB 文件。
"""
import pytest
from sqlalchemy import create_engine, text
from app.database import _migrate_email_log_recipient


@pytest.fixture
def sqlite_conn():
    """独立内存库连接，不触碰全局 engine / 真实 data 目录"""
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect()
    yield conn
    conn.close()
    engine.dispose()


def _create_email_logs(conn, with_recipient: bool = False):
    cols = "id INTEGER PRIMARY KEY, sender TEXT"
    if with_recipient:
        cols += ", recipient TEXT"
    conn.execute(text(f"CREATE TABLE email_logs ({cols})"))
    conn.commit()


def _email_log_columns(conn) -> set:
    return {row[1] for row in conn.execute(text("PRAGMA table_info(email_logs)"))}


class TestMigrateEmailLogRecipient:
    """email_logs.recipient 列自动迁移"""

    def test_adds_recipient_column(self, sqlite_conn):
        _create_email_logs(sqlite_conn, with_recipient=False)
        _migrate_email_log_recipient(sqlite_conn)
        assert "recipient" in _email_log_columns(sqlite_conn)

    def test_idempotent(self, sqlite_conn):
        _create_email_logs(sqlite_conn, with_recipient=False)
        _migrate_email_log_recipient(sqlite_conn)
        _migrate_email_log_recipient(sqlite_conn)  # 重复执行不报错、不重复加列
        assert "recipient" in _email_log_columns(sqlite_conn)

    def test_skips_when_column_already_exists(self, sqlite_conn):
        _create_email_logs(sqlite_conn, with_recipient=True)
        _migrate_email_log_recipient(sqlite_conn)
        assert "recipient" in _email_log_columns(sqlite_conn)
