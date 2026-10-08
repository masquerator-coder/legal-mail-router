"""删除邮件日志时的外键级联测试。

背景：新增 forward_records 表后，删除 email_logs 会因外键约束失败
（连接启用 PRAGMA foreign_keys=ON），前端表现为 500 + 非 JSON 响应。
本测试确保删除日志时子表记录被一并清理，不残留孤儿。
"""
# -*- coding: utf-8 -*-
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Attachment, Base, EmailAccount, EmailLog, ForwardRecord


@pytest.fixture
def db(tmp_path):
    """独立 SQLite 库，显式开启外键约束（与生产一致）"""
    from sqlalchemy import event

    db_file = tmp_path / "del_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _seed(s, n_logs=3, with_forward=True, with_att=True, age_days=0):
    acct = EmailAccount(
        name="测试账户", imap_host="imap.test.com", username="t@test.com",
        password_encrypted="x",
    )
    s.add(acct)
    s.flush()
    ids = []
    for i in range(n_logs):
        log = EmailLog(
            account_id=acct.id,
            subject=f"邮件{i}",
            sender="a@b.com",
            status="success",
            created_at=datetime.now() - timedelta(days=age_days),
        )
        s.add(log)
        s.flush()
        ids.append(log.id)
        if with_att:
            s.add(Attachment(log_id=log.id, filename=f"f{i}.docx",
                             file_path=f"x/{i}.docx", file_size=10))
        if with_forward:
            s.add(ForwardRecord(log_id=log.id, target_email=f"t{i}@x.com",
                                target_name="律师", doc_type="合同协议",
                                doc_types="合同协议", subject=f"邮件{i}",
                                success=True, created_at=datetime.now()))
    s.commit()
    return ids


class TestForeignKeyCascade:
    def test_seed_creates_forward_records(self, db):
        ids = _seed(db)
        assert db.query(ForwardRecord).count() == 3
        assert db.query(EmailLog).count() == 3

    def test_deleting_log_without_children_fails(self, db):
        """反证：不先删子表会触发外键错误（这就是线上 500 的根因）。"""
        from sqlalchemy.exc import IntegrityError
        ids = _seed(db, n_logs=1)
        with pytest.raises(IntegrityError):
            db.execute(
                EmailLog.__table__.delete().where(EmailLog.id.in_(ids))
            )
            db.commit()
        db.rollback()

    def test_delete_selected_order_works(self, db):
        """正确顺序：先 forward_records / attachments，再 email_logs。"""
        ids = _seed(db, n_logs=3)
        db.query(ForwardRecord).filter(ForwardRecord.log_id.in_(ids)).delete(
            synchronize_session=False)
        db.query(Attachment).filter(Attachment.log_id.in_(ids)).delete(
            synchronize_session=False)
        db.query(EmailLog).filter(EmailLog.id.in_(ids)).delete(
            synchronize_session=False)
        db.commit()

        assert db.query(EmailLog).count() == 0
        assert db.query(ForwardRecord).count() == 0
        assert db.query(Attachment).count() == 0

    def test_partial_delete_leaves_others_intact(self, db):
        ids = _seed(db, n_logs=3)
        target = ids[:1]
        db.query(ForwardRecord).filter(ForwardRecord.log_id.in_(target)).delete(
            synchronize_session=False)
        db.query(Attachment).filter(Attachment.log_id.in_(target)).delete(
            synchronize_session=False)
        db.query(EmailLog).filter(EmailLog.id.in_(target)).delete(
            synchronize_session=False)
        db.commit()

        assert db.query(EmailLog).count() == 2
        assert db.query(ForwardRecord).count() == 2
        assert db.query(ForwardRecord).filter(
            ForwardRecord.log_id == ids[1]).count() == 1

    def test_clear_all_order_works(self, db):
        _seed(db, n_logs=4)
        db.query(ForwardRecord).delete()
        db.query(Attachment).delete()
        db.query(EmailLog).delete()
        db.commit()
        assert db.query(EmailLog).count() == 0
        assert db.query(ForwardRecord).count() == 0

    def test_retention_cleanup_order_works(self, db):
        """保留期清理：逐条删除时同样必须先清子表。"""
        ids = _seed(db, n_logs=3, age_days=999)
        cutoff = datetime.now() - timedelta(days=30)
        old = db.query(EmailLog).filter(EmailLog.created_at < cutoff).all()
        assert len(old) == 3

        for entry in old:
            db.query(ForwardRecord).filter_by(log_id=entry.id).delete()
            db.query(Attachment).filter_by(log_id=entry.id).delete()
            db.delete(entry)
            db.commit()

        assert db.query(EmailLog).count() == 0
        assert db.query(ForwardRecord).count() == 0

    def test_log_without_children_deletes_fine(self, db):
        """没有 forward_records 的历史日志（升级前）删除不受影响。"""
        ids = _seed(db, n_logs=2, with_forward=False)
        db.query(ForwardRecord).filter(ForwardRecord.log_id.in_(ids)).delete(
            synchronize_session=False)
        db.query(Attachment).filter(Attachment.log_id.in_(ids)).delete(
            synchronize_session=False)
        db.query(EmailLog).filter(EmailLog.id.in_(ids)).delete(
            synchronize_session=False)
        db.commit()
        assert db.query(EmailLog).count() == 0


class TestRouteWiring:
    """确认路由与定时任务代码里确实先删子表（防回归）。"""

    def _src(self, rel):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        return (root / rel).read_text(encoding="utf-8")

    def test_delete_selected_removes_forward_records(self):
        src = self._src("app/routes/logs.py")
        i = src.index("async def delete_selected_logs")
        body = src[i:i + 3000]
        assert "ForwardRecord" in body, "delete-selected 必须清理 forward_records"
        # 子表删除必须出现在 email_logs 删除之前
        assert body.index("ForwardRecord") < body.index("EmailLog.id.in_(ids)")

    def test_clear_removes_forward_records(self):
        src = self._src("app/routes/logs.py")
        i = src.index("async def clear_logs")
        body = src[i:i + 2500]
        assert "ForwardRecord" in body
        assert body.index("ForwardRecord") < body.index("EmailLog).delete()")

    def test_retention_cleanup_removes_forward_records(self):
        src = self._src("app/services/scheduler.py")
        i = src.index("def cleanup_old_attachments")
        body = src[i:i + 4000]
        assert "ForwardRecord" in body, "保留期清理必须清理 forward_records"
        assert body.index("ForwardRecord") < body.index("db.delete(log_entry)")