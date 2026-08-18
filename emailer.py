"""免费邮件发送：用你自己的邮箱 SMTP 发信（Gmail 等），不花钱。
如果没配置 SMTP，就只打印日志、不崩溃 —— 系统依然免费可用。"""
import os
import smtplib
from email.message import EmailMessage

# 连接超时：避免 SMTP 无响应时同步阻塞整个请求线程（原本无 timeout 会一直挂起）。
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "15"))


def send_email(to: str, subject: str, body: str, html_body: str = None) -> tuple:
    """Returns (success: bool, error_msg: str|None).
    任何一项缺失都无法真正发信：打印日志并安全返回，绝不崩溃。
    html_body: 可选 HTML 版本（不配置则发纯文本）。"""
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    if not host or not to or not user:
        missing = []
        if not host: missing.append("SMTP_HOST")
        if not user: missing.append("SMTP_USER")
        msg = f"SMTP 未配置（缺 {','.join(missing)}）"
        print(f"[邮件未配置] 本应发给 {to}：{subject}")
        return False, msg
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except (ValueError, TypeError):
        port = 587
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as s:
            s.starttls()
            s.login(user, pwd or "")
            s.send_message(msg)
        print(f"[邮件已发] {to}：{subject}")
        return True, None
    except Exception as e:
        print(f"[邮件失败] {e}")
        return False, str(e)
