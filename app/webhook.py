# -*- coding: utf-8 -*-
"""
webhook.py — Flask 应用，监听 8080 端口，接收 Cloudflare 邮件转发的 webhook 请求。

Cloudflare Email Workers 会将邮件以 HTTP POST 发送到此端点。
字段解析兼容多种常见格式，避免因字段名不一致导致信息丢失。
"""

import logging
import os
import random
import string
import time
from datetime import datetime

from flask import Flask, jsonify, request

from app.storage import get_email_by_id, load_emails, save_email

logger = logging.getLogger(__name__)

# 邮件数据文件路径，可通过环境变量 EMAIL_DATA_PATH 覆盖
DATA_PATH = os.environ.get("EMAIL_DATA_PATH", "data/emails.json")

webhook_app = Flask(__name__)


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _generate_id() -> str:
    """生成唯一邮件 ID：时间戳 + 6位随机字母数字。"""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{int(time.time())}_{suffix}"


def _parse_address(value) -> tuple[str, str]:
    """
    从地址字段中提取 email 和 name。
    支持以下格式：
      - "Name <email@example.com>"
      - "email@example.com"
      - {"email": "...", "name": "..."}
      - [{"email": "...", "name": "..."}, ...]  → 仅取第一个
    """
    if not value:
        return "", ""

    # 字典格式：{"email": "...", "name": "..."}
    if isinstance(value, dict):
        return value.get("email", ""), value.get("name", "")

    # 列表格式：取第一个元素
    if isinstance(value, list):
        if value:
            return _parse_address(value[0])
        return "", ""

    # 字符串格式
    value = str(value).strip()
    if "<" in value and ">" in value:
        name = value[:value.index("<")].strip().strip('"').strip("'")
        email = value[value.index("<") + 1:value.index(">")].strip()
        return email, name
    return value, ""


def _parse_address_list(value) -> list[dict]:
    """
    解析地址列表（用于 cc / bcc），返回 [{"email": ..., "name": ...}, ...] 格式。
    """
    if not value:
        return []

    # 已经是列表
    if isinstance(value, list):
        result = []
        for item in value:
            email, name = _parse_address(item)
            if email:
                result.append({"email": email, "name": name})
        return result

    # 逗号分隔的字符串
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        result = []
        for part in parts:
            email, name = _parse_address(part)
            if email:
                result.append({"email": email, "name": name})
        return result

    return []


def _extract_email_fields(payload: dict) -> dict:
    """
    从原始 payload 中提取标准化邮件字段。
    兼容 Cloudflare Email Workers 及其他常见 webhook 字段命名。
    """
    # 发件人：尝试多个可能的字段名
    raw_from = (
        payload.get("from")
        or payload.get("sender")
        or payload.get("from_address")
        or payload.get("fromAddress")
        or ""
    )
    from_email, from_name = _parse_address(raw_from)

    # 若字段中单独给出 from_name
    if not from_name:
        from_name = payload.get("from_name") or payload.get("fromName") or ""

    # 收件人
    raw_to = (
        payload.get("to")
        or payload.get("recipient")
        or payload.get("to_address")
        or payload.get("toAddress")
        or ""
    )
    to_email, to_name = _parse_address(raw_to)
    if not to_name:
        to_name = payload.get("to_name") or payload.get("toName") or ""

    # 主题
    subject = (
        payload.get("subject")
        or payload.get("Subject")
        or payload.get("title")
        or "(无主题)"
    )

    # 正文：优先 text/plain，降级 html
    body_text = (
        payload.get("body_text")
        or payload.get("bodyText")
        or payload.get("text")
        or payload.get("plain")
        or payload.get("body")
        or ""
    )
    body_html = (
        payload.get("body_html")
        or payload.get("bodyHtml")
        or payload.get("html")
        or ""
    )

    # 如果只有 html 而没有纯文本，做简单降级：去除 HTML 标签作为摘要
    if not body_text and body_html:
        import re
        body_text = re.sub(r"<[^>]+>", "", body_html).strip()

    # 抄送 / 密送
    cc = _parse_address_list(payload.get("cc") or payload.get("CC") or [])
    bcc = _parse_address_list(payload.get("bcc") or payload.get("BCC") or [])

    return {
        "from_email": from_email,
        "from_name": from_name,
        "to_email": to_email,
        "to_name": to_name,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "cc": cc,
        "bcc": bcc,
    }


# ─── 路由 ─────────────────────────────────────────────────────────────────────

@webhook_app.post("/webhook")
def receive_webhook():
    """接收 Cloudflare 转发的邮件 webhook。"""
    # 必须是 JSON body
    if not request.is_json:
        # 尝试强制解析，兼容部分不带 Content-Type 的客户端
        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            payload = {}
    else:
        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}

    if not payload and not request.data:
        logger.warning("Received empty webhook request")
        return jsonify({"success": False, "message": "empty body"}), 400

    logger.info("Webhook received, content-type=%s, size=%d bytes",
                request.content_type, len(request.data))

    try:
        fields = _extract_email_fields(payload)
    except Exception as e:
        logger.error("Field extraction failed: %s", e)
        return jsonify({"success": False, "message": "parse error"}), 400

    email = {
        "id": _generate_id(),
        **fields,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw_payload": payload,
    }

    try:
        save_email(DATA_PATH, email)
    except Exception as e:
        logger.error("Save email failed: %s", e)
        return jsonify({"success": False, "message": "storage error"}), 500

    return jsonify({"success": True, "message": "email saved", "id": email["id"]}), 200


@webhook_app.get("/api/emails")
def api_list_emails():
    """返回所有邮件列表，按保存时间倒序。"""
    emails = load_emails(DATA_PATH)
    # 倒序：最新的在最前
    emails_sorted = sorted(emails, key=lambda e: e.get("saved_at", ""), reverse=True)
    return jsonify(emails_sorted), 200


@webhook_app.get("/api/emails/<email_id>")
def api_get_email(email_id: str):
    """返回指定 id 的邮件详情。"""
    email = get_email_by_id(DATA_PATH, email_id)
    if email is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(email), 200


@webhook_app.get("/health")
def health():
    """健康检查端点。"""
    return jsonify({"status": "ok"}), 200
