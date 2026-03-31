# -*- coding: utf-8 -*-
"""
viewer.py — Flask 应用，监听 8089 端口，提供邮件列表和详情的 HTML 页面。

从 8080 的 API 接口读取数据，渲染为 HTML 页面供浏览器访问。
"""

import logging
import os

import requests
from flask import Flask, abort, render_template

logger = logging.getLogger(__name__)

# webhook 服务的 API 地址，可通过环境变量覆盖
WEBHOOK_API = os.environ.get("WEBHOOK_API_BASE", "http://127.0.0.1:8080")

viewer_app = Flask(__name__, template_folder="../templates")


def _fetch_emails() -> list:
    """从 webhook 服务的 API 获取所有邮件列表。"""
    try:
        resp = requests.get(f"{WEBHOOK_API}/api/emails", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to fetch email list: %s", e)
        return []


def _fetch_email(email_id: str) -> dict | None:
    """从 webhook 服务的 API 获取单封邮件详情。"""
    try:
        resp = requests.get(f"{WEBHOOK_API}/api/emails/{email_id}", timeout=5)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to fetch email %s: %s", email_id, e)
        return None


@viewer_app.get("/")
def index():
    """邮件列表页。"""
    emails = _fetch_emails()
    return render_template("index.html", emails=emails)


@viewer_app.get("/email/<email_id>")
def email_detail(email_id: str):
    """邮件详情页。"""
    email = _fetch_email(email_id)
    if email is None:
        abort(404)
    return render_template("email_detail.html", email=email)


@viewer_app.errorhandler(404)
def not_found(e):
    return "<h2>404 - 邮件不存在</h2><p><a href='/'>返回列表</a></p>", 404
