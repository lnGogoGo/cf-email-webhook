# -*- coding: utf-8 -*-
"""
main.py — 入口文件，同时启动 webhook 服务（8080）和预览服务（8089）。

使用 Python 标准库 threading 在两个独立线程中运行两个 Flask 应用，
无需额外进程管理工具，适合直接在 Linux 服务器上运行。
"""

import logging
import os
import sys
import threading

# ─── 日志配置（在导入 app 模块之前配置，确保所有模块共享同一日志格式）──────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

from app.webhook import webhook_app
from app.viewer import viewer_app
from app.storage import _ensure_file

# 端口配置，可通过环境变量覆盖
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", 8080))
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", 8089))
DATA_PATH = os.environ.get("EMAIL_DATA_PATH", "data/emails.json")


def run_webhook():
    """在子线程中启动 webhook 服务。"""
    logger.info("Starting webhook service on 0.0.0.0:%d", WEBHOOK_PORT)
    # use_reloader=False 避免在线程中启动时触发 Flask 的双进程 reloader
    webhook_app.run(host="0.0.0.0", port=WEBHOOK_PORT, use_reloader=False, threaded=True)


def run_viewer():
    """在子线程中启动网页预览服务。"""
    logger.info("Starting viewer service on 0.0.0.0:%d", VIEWER_PORT)
    viewer_app.run(host="0.0.0.0", port=VIEWER_PORT, use_reloader=False, threaded=True)


if __name__ == "__main__":
    # 预先确保数据文件存在
    _ensure_file(DATA_PATH)
    logger.info("Email data file: %s", os.path.abspath(DATA_PATH))

    # 启动两个线程，分别运行两个 Flask 应用
    t_webhook = threading.Thread(target=run_webhook, daemon=True, name="webhook-thread")
    t_viewer = threading.Thread(target=run_viewer, daemon=True, name="viewer-thread")

    t_webhook.start()
    t_viewer.start()

    logger.info("Both services are running.")
    logger.info("  Webhook API  → http://0.0.0.0:%d/webhook", WEBHOOK_PORT)
    logger.info("  Email List   → http://0.0.0.0:%d/", VIEWER_PORT)

    # 主线程阻塞等待，Ctrl+C 可优雅退出
    try:
        t_webhook.join()
        t_viewer.join()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)
