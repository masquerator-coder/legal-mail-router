"""
数据库模型定义
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float, Index
)
from sqlalchemy.orm import relationship
from app.database import Base


class EmailAccount(Base):
    """监控的邮箱账户"""
    __tablename__ = "email_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, comment="账户名称")
    imap_host = Column(String(200), nullable=False, comment="IMAP服务器")
    imap_port = Column(Integer, default=993, comment="IMAP端口")
    use_ssl = Column(Boolean, default=True, comment="使用SSL")
    provider_type = Column(String(20), default="auto", comment="auto/163/gmail/qq/standard")
    username = Column(String(200), nullable=False, comment="邮箱账号")
    password_encrypted = Column(Text, nullable=False, comment="加密的授权码")
    check_interval = Column(Integer, default=30, comment="检查间隔(分钟)")
    filter_sender = Column(String(500), default="", comment="发件人过滤(逗号分隔,空=不过滤)")
    download_attachments = Column(Boolean, default=True, comment="是否下载附件")
    enabled = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    logs = relationship("EmailLog", back_populates="account", cascade="all, delete-orphan")


class LLMConfig(Base):
    """LLM API 配置"""
    __tablename__ = "llm_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), default="默认配置")
    api_url = Column(String(500), nullable=False, comment="API地址(OpenAI兼容)")
    api_key_encrypted = Column(Text, nullable=False, comment="加密的API Key")
    model_name = Column(String(100), nullable=False, comment="模型名称")
    analysis_prompt = Column(Text, default="", comment="自定义分析Prompt(空=使用默认)")
    max_tokens = Column(Integer, default=2000)
    temperature = Column(Float, default=0.3)
    is_active = Column(Boolean, default=True, comment="是否激活")
    model_type = Column(String(20), default="unknown", comment="模型类型: text/multimodal/unknown")
    model_type_locked = Column(Boolean, default=False, comment="模型类型由人工指定，自动检测不覆盖")
    config_role = Column(String(20), default="analyzer", comment="[废弃] 模型用途字段 — 已由 LLM 配置页的「模型角色分配」替代，仅保留兼容旧数据")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class OCRConfig(Base):
    """OCR 识别配置"""
    __tablename__ = "ocr_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), default="OCR配置")
    provider_type = Column(String(20), default="paddleocr", comment="paddleocr/openai-vision/custom")
    api_url = Column(String(500), nullable=False, comment="OCR API地址")
    api_key_encrypted = Column(Text, default="", comment="加密的API Key(可为空)")
    model_name = Column(String(100), default="", comment="模型名称(vision模型需要)")
    is_active = Column(Boolean, default=True, comment="是否激活")
    connectivity_ok = Column(Boolean, nullable=True, comment="连通性测试是否通过: true/false/null(未测)")
    pdf_capable = Column(Boolean, nullable=True, comment="是否支持PDF直读: true/false/null(未测)")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class RoutingRule(Base):
    """转发路由规则"""
    __tablename__ = "routing_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_ids = Column(
        Text, default="",
        comment="关联邮箱账户ID(逗号分隔,空=全局规则)"
    )
    doc_type = Column(String(50), nullable=False, default="", comment="[废弃] 文书类型 — 当前版本未启用按类型匹配")
    keywords = Column(Text, default="", comment="[废弃] 关键词(逗号分隔) — 当前版本未启用关键词匹配")
    target_email = Column(String(200), nullable=False, comment="目标邮箱")
    target_name = Column(String(100), default="", comment="目标律师姓名")
    priority = Column(Integer, default=0, comment="[废弃] 优先级 — 当前版本未使用")
    enabled = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_routing_rules_enabled_priority", "enabled", "priority"),
    )


class DefaultConfig(Base):
    """默认配置(兜底转发、SMTP等)"""
    __tablename__ = "default_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, default="")


class EmailLog(Base):
    """邮件处理记录"""
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("email_accounts.id"), nullable=False)
    message_id = Column(String(500), unique=True, comment="邮件Message-ID(去重用)")
    subject = Column(String(500), comment="邮件主题")
    sender = Column(String(300), comment="发件人")
    recipient = Column(String(300), comment="收件人(To+Cc)")
    received_at = Column(DateTime, comment="接收时间")
    body_preview = Column(Text, comment="正文摘要")
    body_text = Column(Text, comment="邮件完整正文")
    involved_parties = Column(String(500), comment="涉及方")
    doc_type = Column(String(50), comment="文书类型")
    case_summary = Column(Text, comment="案件摘要")
    ai_interpretation = Column(Text, comment="AI初步审核解读")
    revision_instructions = Column(Text, comment="结构化修订指令JSON数组")
    urgency = Column(String(10), comment="紧急程度: high/medium/low")
    key_date = Column(String(50), comment="关键日期")
    case_number = Column(String(100), comment="案号")
    target_email = Column(String(200), comment="转发目标邮箱")
    status = Column(String(20), default="pending", comment="pending/analyzed/forwarded/failed/skipped")
    llm_raw_response = Column(Text, comment="LLM原始响应")
    doc_types = Column(Text, comment="多附件时所有文书类型列表(逗号分隔)")
    error_message = Column(Text, comment="错误信息")
    created_at = Column(DateTime, default=datetime.now)

    account = relationship("EmailAccount", back_populates="logs")
    attachments = relationship("Attachment", back_populates="email_log", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_email_logs_created_at", "created_at"),
        Index("ix_email_logs_status", "status"),
        Index("ix_email_logs_account_id", "account_id"),
    )


class Attachment(Base):
    """邮件附件"""
    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    log_id = Column(Integer, ForeignKey("email_logs.id"), nullable=False)
    filename = Column(String(500), comment="文件名")
    file_path = Column(String(1000), comment="存储路径")
    file_size = Column(Integer, default=0, comment="文件大小(字节)")
    created_at = Column(DateTime, default=datetime.now)

    email_log = relationship("EmailLog", back_populates="attachments")
