"""
RoutingRules — 转发规则两种匹配方式（按邮箱 / 按文书类型）测试

覆盖：
- 按邮箱规则行为与升级前完全一致（回归保护）
- 按文书类型规则的单类型命中、多类型命中、不命中
- 两种方式取并集且同一目标邮箱只转发一次
- 监控邮箱范围对两种方式均生效
- 无任何规则命中且未配置默认邮箱 → 不转发（failed）
- 表单校验：一条规则只能使用一种匹配方式
"""
# -*- coding: utf-8 -*-
import pytest

from app.services.scheduler import (
    _collect_mail_doc_types,
    _match_rule_doc_types,
    _get_forward_targets,
)


# ── 测试用最小对象（避免依赖真实 DB / IMAP） ──

class _Account:
    def __init__(self, id=1, username="monitor@example.com", imap_host="imap.example.com"):
        self.id = id
        self.username = username
        self.imap_host = imap_host
        self.password_encrypted = "enc"
        self.name = f"账户{id}"


class _Log:
    def __init__(self, doc_types=None, doc_type=None):
        self.doc_types = doc_types
        self.doc_type = doc_type
        self.status = "analyzed"
        self.error_message = None
        self.target_email = None


@pytest.fixture
def rule_db():
    """独立内存库（不触碰真实 data 目录）"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import RoutingRule, DefaultConfig, EmailAccount  # noqa: F401  # 注册模型

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def _add_account(db, account_id=1, name=None):
    from app.models import EmailAccount
    db.add(EmailAccount(
        id=account_id,
        name=name or f"账户{account_id}",
        imap_host="imap.example.com",
        username=f"monitor{account_id}@example.com",
        password_encrypted="enc",
    ))
    db.commit()


def _add_rule(db, target_email, rule_type="account", doc_type="",
              account_ids="", enabled=True):
    from app.models import RoutingRule
    rule = RoutingRule(
        rule_type=rule_type,
        doc_type=doc_type,
        target_email=target_email,
        account_ids=account_ids,
        enabled=enabled,
    )
    db.add(rule)
    db.commit()
    return rule


def _set_default_email(db, email):
    from app.models import DefaultConfig
    db.add(DefaultConfig(key="default_forward_email", value=email))
    db.commit()


def _targets(result):
    return [t["email"] for t in result]


def _analysis(doc_type="合同协议", confidence=0.9):
    return {"doc_type": doc_type, "confidence": confidence}


# ── 邮件文书类型集合收集 ──

class TestCollectMailDocTypes:
    def test_prefers_log_doc_types(self):
        log = _Log(doc_types="合同协议,起诉状")
        types = _collect_mail_doc_types(_analysis("合同协议"), log)
        assert types == {"合同协议", "起诉状"}

    def test_falls_back_to_analysis(self):
        log = _Log(doc_types=None)
        assert _collect_mail_doc_types(_analysis("判决书"), log) == {"判决书"}

    def test_empty_when_nothing_available(self):
        assert _collect_mail_doc_types(None, _Log()) == set()

    def test_blank_doc_types_treated_as_empty(self):
        log = _Log(doc_types="  ,  ")
        assert _collect_mail_doc_types(_analysis("传票"), log) == {"传票"}


# ── 类型匹配判定 ──

class TestMatchRuleDocTypes:
    class _Rule:
        def __init__(self, doc_type):
            self.doc_type = doc_type

    def test_single_type_hit(self):
        assert _match_rule_doc_types(self._Rule("合同协议"), {"合同协议"})

    def test_multi_type_rule_any_hit(self):
        assert _match_rule_doc_types(self._Rule("合同协议,起诉状"), {"起诉状"})

    def test_no_intersection(self):
        assert not _match_rule_doc_types(self._Rule("合同协议"), {"起诉状"})

    def test_empty_rule_types_never_hits(self):
        assert not _match_rule_doc_types(self._Rule(""), {"合同协议"})
        assert not _match_rule_doc_types(self._Rule("  ,  "), {"合同协议"})

    def test_empty_mail_types_never_hits(self):
        assert not _match_rule_doc_types(self._Rule("合同协议"), set())


# ── 转发目标决策 ──

class TestForwardTargetsByAccount:
    """按邮箱规则：与升级前行为一致（回归保护）"""

    def test_global_rule_matches_any_account(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "lawyer@example.com", rule_type="account")
        result = _get_forward_targets(_analysis(), False, _Account(id=7), rule_db, _Log())
        assert _targets(result) == ["lawyer@example.com"]

    def test_account_scoped_rule_matches_only_listed(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "a@example.com", rule_type="account", account_ids="3,4")
        # 命中
        assert _targets(_get_forward_targets(_analysis(), False, _Account(id=3), rule_db, _Log())) \
            == ["a@example.com"]
        # 不命中 → 无默认邮箱 → 失败不转发
        log = _Log()
        assert _get_forward_targets(_analysis(), False, _Account(id=9), rule_db, log) == []
        assert log.status == "failed"

    def test_legacy_rule_without_rule_type_treated_as_account(self, rule_db):
        """存量规则 rule_type 为空/None 时按邮箱处理（升级兼容）"""
        from app.models import RoutingRule
        _add_default_smtp(rule_db)
        rule_db.add(RoutingRule(rule_type=None, target_email="legacy@example.com",
                                account_ids="", enabled=True))
        rule_db.commit()
        result = _get_forward_targets(_analysis(), False, _Account(id=1), rule_db, _Log())
        assert _targets(result) == ["legacy@example.com"]

    def test_multi_target_email_split(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, " a@x.com , b@x.com ", rule_type="account")
        result = _get_forward_targets(_analysis(), False, _Account(), rule_db, _Log())
        assert _targets(result) == ["a@x.com", "b@x.com"]


class TestForwardTargetsByDocType:
    """按文书类型规则"""

    def test_single_type_hit(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        result = _get_forward_targets(_analysis("合同协议"), False, _Account(), rule_db, _Log())
        assert _targets(result) == ["contract@example.com"]

    def test_type_not_matched_no_default_means_no_forward(self, rule_db):
        """未命中类型规则且未配置默认邮箱 → 不转发（failed）"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        log = _Log()
        result = _get_forward_targets(_analysis("起诉状"), False, _Account(), rule_db, log)
        assert result == []
        assert log.status == "failed"
        assert "未配置转发目标" in log.error_message

    def test_type_not_matched_falls_back_to_default(self, rule_db):
        """未命中有规则，但配置了默认邮箱 → 兜底默认"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        _set_default_email(rule_db, "default@example.com")
        result = _get_forward_targets(_analysis("起诉状"), False, _Account(), rule_db, _Log())
        assert _targets(result) == ["default@example.com"]

    def test_multi_doc_email_hits_each_type_rule(self, rule_db):
        """一封邮件含多份不同类型文书 → 各类型规则的目标都收到（并集）"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        _add_rule(rule_db, "pleading@example.com", rule_type="doc_type", doc_type="起诉状")
        log = _Log(doc_types="合同协议,起诉状")
        result = _get_forward_targets(_analysis("合同协议"), False, _Account(), rule_db, log)
        assert set(_targets(result)) == {"contract@example.com", "pleading@example.com"}
        assert log.target_email == "contract@example.com,pleading@example.com"

    def test_multi_type_rule_hits_on_any_listed_type(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "litigation@example.com", rule_type="doc_type",
                  doc_type="起诉状,判决书")
        result = _get_forward_targets(_analysis("判决书"), False, _Account(), rule_db, _Log())
        assert _targets(result) == ["litigation@example.com"]

    def test_same_target_deduped(self, rule_db):
        """两条类型规则命中同一目标邮箱 → 只转发一次"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "shared@example.com", rule_type="doc_type", doc_type="合同协议")
        _add_rule(rule_db, "shared@example.com", rule_type="doc_type", doc_type="起诉状")
        log = _Log(doc_types="合同协议,起诉状")
        result = _get_forward_targets(_analysis("合同协议"), False, _Account(), rule_db, log)
        assert _targets(result) == ["shared@example.com"]

    def test_disabled_rule_ignored(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type",
                  doc_type="合同协议", enabled=False)
        log = _Log()
        result = _get_forward_targets(_analysis("合同协议"), False, _Account(), rule_db, log)
        assert result == []
        assert log.status == "failed"

    def test_account_scope_applies_to_doc_type_rule(self, rule_db):
        """类型规则同样受监控邮箱范围限制"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type",
                  doc_type="合同协议", account_ids="2")
        # 命中范围
        assert _targets(_get_forward_targets(_analysis("合同协议"), False,
                                             _Account(id=2), rule_db, _Log())) \
            == ["contract@example.com"]
        # 超出范围 → 不转发
        log = _Log()
        assert _get_forward_targets(_analysis("合同协议"), False,
                                    _Account(id=5), rule_db, log) == []
        assert log.status == "failed"


class TestForwardTargetsUnionOfBothModes:
    """两种匹配方式取并集"""

    def test_account_and_doc_type_rules_both_apply(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "account-route@example.com", rule_type="account")
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        result = _get_forward_targets(_analysis("合同协议"), False, _Account(), rule_db, _Log())
        assert set(_targets(result)) == {"account-route@example.com", "contract@example.com"}

    def test_doc_type_rule_does_not_suppress_account_rule(self, rule_db):
        """类型规则不命中，不影响同邮箱下按邮箱规则继续生效"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "account-route@example.com", rule_type="account")
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        result = _get_forward_targets(_analysis("传票"), False, _Account(), rule_db, _Log())
        assert _targets(result) == ["account-route@example.com"]


class TestJunkFilterUnchanged:
    """垃圾过滤优先于路由匹配（回归保护）"""

    def test_low_confidence_non_legal_skipped(self, rule_db):
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "account-route@example.com", rule_type="account")
        log = _Log()
        result = _get_forward_targets(
            {"doc_type": "非法律文书", "confidence": 0.1}, False, _Account(), rule_db, log
        )
        assert result == []
        assert log.status == "skipped"


def _add_default_smtp(db):
    """注入可用的默认 SMTP，避免决策在 SMTP 缺失处提前返回 failed"""
    from app.models import DefaultConfig
    for key, value in (
        ("default_smtp_host", "smtp.example.com"),
        ("default_smtp_port", "587"),
        ("default_smtp_username", "sender@example.com"),
        ("default_smtp_password", "enc"),
    ):
        db.add(DefaultConfig(key=key, value=value))
    db.commit()


class TestClassifyFailedBlocksRouting:
    """类型识别失败（兜底值）不得进入路由：否则会误命中规则或给出误导性的失败原因"""

    def test_does_not_hit_matching_rule(self, rule_db):
        """兜底值恰好等于某条规则的类型时也不得命中（这是失败值，不是模型判断）"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "other-docs@example.com", rule_type="doc_type",
                  doc_type="其他法律文书")
        log = _Log()
        result = _get_forward_targets(
            {"doc_type": "其他法律文书", "confidence": 0.5, "classify_failed": True},
            False, _Account(), rule_db, log,
        )
        assert result == []
        assert log.status == "failed"
        assert "识别失败" in log.error_message
        assert log.target_email is None

    def test_does_not_fall_back_to_default_email(self, rule_db):
        """识别失败时即便配了默认邮箱也不得转发（默认邮箱是兜底路由，不是兜底类型）"""
        _add_default_smtp(rule_db)
        _set_default_email(rule_db, "default@example.com")
        log = _Log()
        result = _get_forward_targets(
            {"doc_type": "其他法律文书", "confidence": 0.5, "classify_failed": True},
            False, _Account(), rule_db, log,
        )
        assert result == []
        assert log.status == "failed"

    def test_error_message_distinguishes_from_no_target(self, rule_db):
        """错误信息必须区别于「未配置转发目标」，避免把识别失败误诊为路由配置问题"""
        _add_default_smtp(rule_db)
        log = _Log()
        _get_forward_targets(
            {"doc_type": "其他法律文书", "confidence": 0.5, "classify_failed": True},
            False, _Account(), rule_db, log,
        )
        assert "未配置转发目标" not in log.error_message
        assert "max_tokens" in log.error_message

    def test_model_chosen_other_legal_still_routes_normally(self, rule_db):
        """模型主动判定「其他法律文书」时不受影响，仍按正常路由规则走"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "other-docs@example.com", rule_type="doc_type",
                  doc_type="其他法律文书")
        log = _Log()
        result = _get_forward_targets(
            {"doc_type": "其他法律文书", "confidence": 0.85},
            False, _Account(), rule_db, log,
        )
        assert _targets(result) == ["other-docs@example.com"]

    def test_petition_type_routes_to_petition_rule(self, rule_db):
        """识别为「信访件」时应命中信访件专属规则"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "xinfang@example.com", rule_type="doc_type", doc_type="信访件")
        _add_rule(rule_db, "notice@example.com", rule_type="doc_type", doc_type="通知书")
        log = _Log()
        result = _get_forward_targets(
            {"doc_type": "信访件", "confidence": 0.85}, False, _Account(), rule_db, log,
        )
        assert _targets(result) == ["xinfang@example.com"]


class TestMultiGroupClassifyFailed:
    """多附件分组：任一组类型识别失败时，兜底值不得进入类型规则匹配

    回归背景：分组时 analysis 只是按 confidence 选出的**一组**，单看它无法发现
    别的组识别失败；而 log.doc_types 含全部组，失败组的兜底值「其他法律文书」
    会命中该类型规则 → 转发到错误邮箱。
    """

    FAILED_GROUP = {"doc_type": "其他法律文书", "confidence": 0.5, "classify_failed": True}
    OK_GROUP = {"doc_type": "合同协议", "confidence": 0.9}

    def test_failed_group_type_excluded_from_matching_set(self):
        """失败组的兜底类型必须从匹配集合中剔除，成功组的类型保留"""
        log = _Log(doc_types="其他法律文书,合同协议")
        types = _collect_mail_doc_types(
            self.OK_GROUP, log, analyses=[self.FAILED_GROUP, self.OK_GROUP]
        )
        assert types == {"合同协议"}

    def test_single_group_failure_excludes_type(self):
        """单组失败（未传 analyses 时由 analysis 自身兜底判定）"""
        log = _Log(doc_types="其他法律文书")
        types = _collect_mail_doc_types(self.FAILED_GROUP, log, analyses=[self.FAILED_GROUP])
        assert types == set()

    def test_successful_groups_keep_all_types(self):
        """全部组均成功时，类型集合不受影响（不引入新的行为变化）"""
        other = {"doc_type": "起诉状", "confidence": 0.8}
        log = _Log(doc_types="合同协议,起诉状")
        types = _collect_mail_doc_types(self.OK_GROUP, log, analyses=[self.OK_GROUP, other])
        assert types == {"合同协议", "起诉状"}

    def test_any_failed_group_blocks_forwarding(self, rule_db):
        """任一组的识别失败 → 不转发（即使别的组识别成功）"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        _add_rule(rule_db, "other-docs@example.com", rule_type="doc_type",
                  doc_type="其他法律文书")
        log = _Log(doc_types="其他法律文书,合同协议")
        result = _get_forward_targets(
            self.OK_GROUP, False, _Account(), rule_db, log,
            analyses=[self.FAILED_GROUP, self.OK_GROUP],
        )
        assert result == []
        assert log.status == "failed"
        assert "识别失败" in log.error_message
        assert "其他法律文书" in log.error_message

    def test_all_groups_ok_still_routes(self, rule_db):
        """全部组识别成功时照常路由（防止上面的拦截过度扩大）"""
        _add_default_smtp(rule_db)
        _add_rule(rule_db, "contract@example.com", rule_type="doc_type", doc_type="合同协议")
        log = _Log(doc_types="合同协议")
        result = _get_forward_targets(
            self.OK_GROUP, False, _Account(), rule_db, log,
            analyses=[self.OK_GROUP],
        )
        assert _targets(result) == ["contract@example.com"]
        assert log.status == "analyzed"


# ── 表单校验：一条规则只能使用一种匹配方式 ──

class TestNormalizeRuleForm:
    def test_account_mode_clears_doc_type(self):
        from app.routes.routing import _normalize_rule_form
        rule_type, doc_type, err = _normalize_rule_form("account", ["合同协议"])
        assert (rule_type, doc_type, err) == ("account", "", None)

    def test_doc_type_mode_requires_at_least_one_type(self):
        from app.routes.routing import _normalize_rule_form
        rule_type, doc_type, err = _normalize_rule_form("doc_type", [])
        assert rule_type == "doc_type" and err is not None

    def test_doc_type_mode_accepts_selection(self):
        from app.routes.routing import _normalize_rule_form
        rule_type, doc_type, err = _normalize_rule_form("doc_type", ["合同协议", "起诉状"])
        assert (rule_type, doc_type, err) == ("doc_type", "合同协议,起诉状", None)

    def test_doc_type_deduped_and_trimmed(self):
        from app.routes.routing import _normalize_rule_form
        _, doc_type, _ = _normalize_rule_form("doc_type", [" 合同协议 ", "合同协议", ""])
        assert doc_type == "合同协议"

    def test_unknown_rule_type_falls_back_to_account(self):
        from app.routes.routing import _normalize_rule_form
        rule_type, doc_type, err = _normalize_rule_form("garbage", ["合同协议"])
        assert (rule_type, doc_type, err) == ("account", "", None)