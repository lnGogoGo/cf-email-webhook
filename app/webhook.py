# -*- coding: utf-8 -*-
"""
webhook.py — Flask 应用，监听 8080 端口，接收 Cloudflare 邮件转发的 webhook 请求。

Cloudflare Email Workers 会将邮件以 HTTP POST 发送到此端点。
字段解析兼容多种常见格式，避免因字段名不一致导致信息丢失。
支持解析 raw MIME 原文（含 base64 + GBK 等非 UTF-8 编码）。
"""

import email as _email_lib
import email.header
import email.policy
import logging
import os
import random
import re
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


def _decode_mime_header(value: str) -> str:
    """
    解码 MIME 编码的邮件头，例如 =?GBK?B?...?= 或 =?GBK?Q?...?=。
    返回 Unicode 字符串。
    """
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            decoded.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(raw)
    return "".join(decoded).strip()


def _parse_mime_raw(raw_mime: str) -> dict:
    """
    解析完整的 MIME 原文（payload["raw"] 字段），提取：
    - from_email / from_name
    - to_email / to_name
    - subject
    - body_text（text/plain，支持 base64 + 任意 charset）
    - body_html（text/html，支持 base64 + 任意 charset）
    - cc / bcc

    兼容 GBK、GB2312、UTF-8、base64、quoted-printable 等常见编码组合。
    """
    result = {
        "from_email": "", "from_name": "",
        "to_email": "", "to_name": "",
        "subject": "",
        "body_text": "", "body_html": "",
        "cc": [], "bcc": [],
    }

    try:
        # compat32 policy 保留原始字节级信息，避免自动解码丢失数据
        msg = _email_lib.message_from_string(raw_mime, policy=email.policy.compat32)
    except Exception as e:
        logger.error("MIME parse failed: %s", e)
        return result

    # ── 解析头部字段 ──────────────────────────────────────────────────
    raw_from = msg.get("From", "")
    from_email, from_name = _parse_address(_decode_mime_header(raw_from))
    result["from_email"] = from_email
    result["from_name"] = from_name

    raw_to = msg.get("To", "")
    to_email, to_name = _parse_address(_decode_mime_header(raw_to))
    result["to_email"] = to_email
    result["to_name"] = to_name

    result["subject"] = _decode_mime_header(msg.get("Subject", ""))

    raw_cc = msg.get("Cc", "") or msg.get("CC", "")
    if raw_cc:
        result["cc"] = _parse_address_list(_decode_mime_header(raw_cc))

    raw_bcc = msg.get("Bcc", "") or msg.get("BCC", "")
    if raw_bcc:
        result["bcc"] = _parse_address_list(_decode_mime_header(raw_bcc))

    # ── 遍历 MIME Part，提取正文 ──────────────────────────────────────
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue

        # 跳过附件
        disposition = part.get("Content-Disposition", "")
        if "attachment" in disposition:
            continue

        # 获取字符集，默认 utf-8
        charset = part.get_content_charset() or "utf-8"

        try:
            payload_bytes = part.get_payload(decode=True)  # 自动处理 base64/qp
            if payload_bytes is None:
                continue
            text = payload_bytes.decode(charset, errors="replace")
        except Exception as e:
            logger.warning("Decode part (%s, charset=%s) failed: %s", content_type, charset, e)
            continue

        if content_type == "text/plain" and not result["body_text"]:
            result["body_text"] = text
        elif content_type == "text/html" and not result["body_html"]:
            result["body_html"] = text

    return result


def _extract_email_fields(payload: dict) -> dict:
    """
    从原始 payload 中提取标准化邮件字段。

    策略（优先级从高到低）：
    1. 若 payload 中含 "raw" 字段（完整 MIME 原文），优先解析 MIME。
       MIME 中缺失的字段再从 payload 顶层补充（from/to/subject 等）。
    2. 无 raw 时，从 payload 顶层字段兼容提取。
    """
    # ── Step 1: 尝试解析 raw MIME ─────────────────────────────────────
    raw_mime = payload.get("raw") or payload.get("rawEmail") or payload.get("raw_email") or ""
    mime_fields: dict = {}
    if raw_mime:
        logger.info("Found raw MIME field, parsing MIME content")
        mime_fields = _parse_mime_raw(raw_mime)

    # ── Step 2: 从 payload 顶层读取备用值 ────────────────────────────
    raw_from = (
        payload.get("from")
        or payload.get("sender")
        or payload.get("from_address")
        or payload.get("fromAddress")
        or ""
    )
    fallback_from_email, fallback_from_name = _parse_address(raw_from)

    raw_to = (
        payload.get("to")
        or payload.get("recipient")
        or payload.get("to_address")
        or payload.get("toAddress")
        or ""
    )
    fallback_to_email, fallback_to_name = _parse_address(raw_to)

    fallback_subject = (
        payload.get("subject")
        or payload.get("Subject")
        or payload.get("title")
        or ""
    )

    fallback_body_text = (
        payload.get("body_text")
        or payload.get("bodyText")
        or payload.get("text")
        or payload.get("plain")
        or payload.get("body")
        or ""
    )
    fallback_body_html = (
        payload.get("body_html")
        or payload.get("bodyHtml")
        or payload.get("html")
        or ""
    )

    fallback_cc = _parse_address_list(payload.get("cc") or payload.get("CC") or [])
    fallback_bcc = _parse_address_list(payload.get("bcc") or payload.get("BCC") or [])

    # ── Step 3: 合并，MIME 优先，顶层字段兜底 ────────────────────────
    from_email = mime_fields.get("from_email") or fallback_from_email
    from_name  = mime_fields.get("from_name")  or fallback_from_name or payload.get("from_name") or ""
    to_email   = mime_fields.get("to_email")   or fallback_to_email
    to_name    = mime_fields.get("to_name")    or fallback_to_name  or payload.get("to_name") or ""
    subject    = mime_fields.get("subject")    or fallback_subject  or "(无主题)"
    body_text  = mime_fields.get("body_text")  or fallback_body_text
    body_html  = mime_fields.get("body_html")  or fallback_body_html
    cc         = mime_fields.get("cc")         or fallback_cc
    bcc        = mime_fields.get("bcc")        or fallback_bcc

    # 只有 HTML 没有纯文本时，剥离标签生成摘要文本
    if not body_text and body_html:
        body_text = re.sub(r"<[^>]+>", "", body_html).strip()

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
