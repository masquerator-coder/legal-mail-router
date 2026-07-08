"""
邮箱账户配置路由
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db, db_retry_commit
from app.models import EmailAccount
from app.config import encrypt
from app.scheduler import add_check_job, remove_check_job, scheduler
from app.flash import flash
from app.csrf import check_csrf
router = APIRouter(prefix="/email-config", tags=["邮箱配置"])


@router.get("")
async def list_accounts(request: Request, db: Session = Depends(get_db)):
    accounts = db.query(EmailAccount).order_by(EmailAccount.created_at.desc()).all()
    from app.models import DefaultConfig
    default_cfg = db.query(DefaultConfig).filter_by(key="default_check_interval").first()
    default_interval = int(default_cfg.value) if default_cfg and default_cfg.value else 30
    return request.app.state.templates.TemplateResponse(request, "email_config.html", {
        "request": request,
        "active_page": "email_config",
        "accounts": accounts,
        "default_interval": default_interval,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_account(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    imap_host: str = Form(...),
    imap_port: int = Form(993),
    provider_type: str = Form("auto"),
    username: str = Form(...),
    password: str = Form(...),
    check_interval: int = Form(30),
    filter_sender: str = Form(""),
    download_attachments: str = Form("false"),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    _download_attachments = download_attachments.lower() in ("true", "on", "1")
    _enabled = enabled.lower() in ("true", "on", "1")
    account = EmailAccount(
        name=name,
        imap_host=imap_host,
        imap_port=imap_port,
        use_ssl=True,
        provider_type=provider_type,
        username=username,
        password_encrypted=encrypt(password),
        check_interval=check_interval,
        filter_sender=filter_sender,
        download_attachments=_download_attachments,
        enabled=_enabled,
    )
    db.add(account)
    db_retry_commit(db)

    if _enabled:
        add_check_job(account.id, check_interval)

    flash(request, f"邮箱账户「{name}」已添加", "success")
    return RedirectResponse(url="/email-config", status_code=303)


@router.post("/edit/{account_id}")
async def edit_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    imap_host: str = Form(...),
    imap_port: int = Form(993),
    provider_type: str = Form("auto"),
    username: str = Form(...),
    password: str = Form(""),
    check_interval: int = Form(30),
    filter_sender: str = Form(""),
    download_attachments: str = Form("false"),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    _download_attachments = download_attachments.lower() in ("true", "on", "1")
    _enabled = enabled.lower() in ("true", "on", "1")
    account = db.query(EmailAccount).filter_by(id=account_id).first()
    if not account:
        return RedirectResponse(url="/email-config", status_code=303)

    account.name = name
    account.imap_host = imap_host
    account.imap_port = imap_port
    account.provider_type = provider_type
    account.username = username
    if password.strip():  # 只有输入了新密码才更新
        account.password_encrypted = encrypt(password)
    account.check_interval = check_interval
    account.filter_sender = filter_sender
    account.download_attachments = _download_attachments
    account.enabled = _enabled
    db_retry_commit(db)

    # 更新调度任务
    if _enabled:
        add_check_job(account.id, check_interval)
    else:
        remove_check_job(account.id)

    flash(request, f"邮箱账户「{name}」已更新", "success")
    return RedirectResponse(url="/email-config", status_code=303)


@router.post("/delete/{account_id}")
async def delete_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    account = db.query(EmailAccount).filter_by(id=account_id).first()
    if account:
        remove_check_job(account_id)
        db.delete(account)
        db_retry_commit(db)
        flash(request, f"邮箱账户「{account.name}」已删除", "success")
    return RedirectResponse(url="/email-config", status_code=303)


@router.post("/toggle/{account_id}")
async def toggle_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    account = db.query(EmailAccount).filter_by(id=account_id).first()
    if account:
        account.enabled = not account.enabled
        db_retry_commit(db)
        if account.enabled:
            add_check_job(account.id, account.check_interval)
        else:
            remove_check_job(account.id)
        flash(request, f"账户「{account.name}」已{'启用' if account.enabled else '停用'}", "success")
    return RedirectResponse(url="/email-config", status_code=303)


@router.post("/test/{account_id}")
async def test_connection(account_id: int, request: Request, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """测试邮箱连接 — 仅连接+列出邮件，不改动状态"""
    check_csrf(request, form_csrf)
    import asyncio
    account = db.query(EmailAccount).filter_by(id=account_id).first()
    if not account:
        return {"success": False, "message": "账户不存在"}

    try:
        result = await asyncio.to_thread(
            _test_connection_only,
            account.imap_host,
            account.imap_port,
            account.username,
            account.password_encrypted,
            account.provider_type,
        )
        return result
    except Exception as e:
        return {"success": False, "message": f"连接失败：{str(e)}"}


def _test_connection_only(host, port, username, password_encrypted, provider_type):
    """轻量测试：只连接、列出、断开，不标记已读，不下载附件"""
    import ssl
    import socket
    import re
    from email.header import decode_header
    from datetime import datetime, timedelta
    from app.config import decrypt

    password = decrypt(password_encrypted)
    is_163 = provider_type == "163" or "163.com" in host
    _IMAP_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    ctx = ssl.create_default_context()
    sock = socket.create_connection((host, port), timeout=15)
    ssock = ctx.wrap_socket(sock, server_hostname=host)

    def rl():
        line = b""
        while not line.endswith(b"\r\n"):
            ch = ssock.recv(1)
            if not ch:
                return None
            line += ch
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    def cmd(cb, tag):
        ssock.sendall(cb + b"\r\n")
        lines = []
        while True:
            line = rl()
            if line is None:
                break
            lines.append(line)
            if line.startswith(tag + " "):
                break
        return lines

    def dh(v):
        if v is None:
            return ""
        parts = decode_header(v)
        r = []
        for p, cs in parts:
            if isinstance(p, bytes):
                try:
                    r.append(p.decode(cs or "utf-8", errors="replace"))
                except Exception:
                    r.append(p.decode("utf-8", errors="replace"))
            else:
                r.append(str(p))
        return "".join(r)

    try:
        rl()  # greeting
        cmd(b"A1 CAPABILITY", "A1")

        if is_163:
            cmd(b'A2 ID ("name" "Thunderbird" "version" "128.0")', "A2")

        lines = cmd(f'A3 LOGIN "{username}" "{password}"'.encode(), "A3")
        if not any("A3 OK" in line for line in lines):
            return {"success": False, "message": "登录失败"}

        lines = cmd(b'A4 SELECT "INBOX"', "A4")
        if not any("A4 OK" in line for line in lines):
            last_lines = [line for line in lines[-3:] if line]
            detail = "; ".join(last_lines) if last_lines else "无响应"
            return {"success": False, "message": f"无法选择收件箱: {detail}"}

        # 统计总数和最近的邮件
        lines = cmd(b"A5 SEARCH ALL", "A5")
        total = 0
        for line in lines:
            m = re.search(r"\* SEARCH (.+)", line)
            if m and m.group(1).strip():
                total = len(m.group(1).split())

        # 搜最近3天的邮件（只读摘要）
        since = datetime.now() - timedelta(days=3)
        since_str = f"{since.day:02d}-{_IMAP_MONTHS[since.month-1]}-{since.year}"
        lines = cmd(f"A6 SEARCH SINCE {since_str}".encode(), "A6")

        recent_ids = []
        for line in lines:
            m = re.search(r"\* SEARCH (.+)", line)
            if m and m.group(1).strip():
                recent_ids = m.group(1).split()

        # 最多拉5封的摘要
        emails = []
        for msg_id in sorted(recent_ids, key=int)[-5:]:
            tag = f"F{msg_id}"
            ssock.sendall(f'{tag} FETCH {msg_id} (BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])\r\n'.encode())
            line = rl()
            lit = re.search(r"\{(\d+)\}", line) if line else None
            if lit:
                size = int(lit.group(1))
                data = b""
                while len(data) < size:
                    chunk = ssock.recv(min(4096, size - len(data)))
                    if not chunk:
                        break
                    data += chunk
                rl()  # trailing line
                rl()  # tagged response

                # Parse headers
                subj = from_addr = date = ""
                for hdr_line in data.decode("utf-8", errors="replace").split("\r\n"):
                    if hdr_line.lower().startswith("subject:"):
                        subj = hdr_line[8:].strip()
                    elif hdr_line.lower().startswith("from:"):
                        from_addr = hdr_line[5:].strip()
                    elif hdr_line.lower().startswith("date:"):
                        date = hdr_line[5:].strip()

                emails.append({
                    "subject": dh(subj),
                    "sender": dh(from_addr),
                    "date": date,
                    "attachments": 0,
                })

        cmd(b"A9 LOGOUT", "A9")
        ssock.close()

        return {
            "success": True,
            "message": f"连接成功！收件箱共 {total} 封邮件，最近3天 {len(recent_ids)} 封",
            "count": len(recent_ids),
            "total": total,
            "emails": emails,
        }
    except Exception as e:
        try:
            ssock.close()
        except Exception:
            pass
        return {"success": False, "message": str(e)}
