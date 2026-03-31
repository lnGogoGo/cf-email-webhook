# -*- coding: utf-8 -*-
"""
storage.py — 负责邮件数据的读取与持久化写入。
使用文件锁保证并发安全，自动处理文件不存在 / JSON 损坏等异常情况。
"""

import json
import os
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 全局写锁，防止多线程并发写入时数据损坏
_write_lock = threading.Lock()


def _ensure_file(filepath: str) -> None:
    """确保数据文件和父目录存在；若 JSON 损坏则重置为空数组。"""
    dirpath = os.path.dirname(filepath)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)

    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([], f)
        logger.info("Created new email storage file: %s", filepath)
        return

    # 文件存在时校验 JSON 完整性
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Root element is not a list")
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Corrupted JSON detected (%s), resetting to empty list.", e)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([], f)


def load_emails(filepath: str) -> list:
    """读取所有邮件，返回列表；出错时返回空列表。"""
    _ensure_file(filepath)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load emails: %s", e)
        return []


def save_email(filepath: str, email: dict) -> None:
    """将单封邮件追加写入 JSON 文件，线程安全。"""
    with _write_lock:
        _ensure_file(filepath)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                emails = json.load(f)
        except Exception as e:
            logger.error("Read before write failed (%s), starting fresh.", e)
            emails = []

        emails.append(email)

        # 原子写：先写临时文件再替换，防止写到一半崩溃导致数据丢失
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(emails, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        logger.info("Email saved: id=%s", email.get("id"))


def get_email_by_id(filepath: str, email_id: str) -> dict | None:
    """按 id 查找单封邮件；未找到返回 None。"""
    emails = load_emails(filepath)
    for email in emails:
        if email.get("id") == email_id:
            return email
    return None
