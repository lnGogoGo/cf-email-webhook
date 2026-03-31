# cf-email-webhook

接收 Cloudflare 邮件转发 Webhook 并提供网页预览的 Python 服务。

## 项目结构

```
cf-email-webhook/
├── app/
│   ├── __init__.py
│   ├── storage.py      # 数据读写（线程安全 JSON 持久化）
│   ├── webhook.py      # Flask 应用 - 8080 端口，接收 webhook
│   └── viewer.py       # Flask 应用 - 8089 端口，网页预览
├── data/
│   └── emails.json     # 邮件数据（自动创建）
├── templates/
│   ├── index.html      # 邮件列表页
│   └── email_detail.html  # 邮件详情页
├── main.py             # 入口：同时启动两个 Flask 服务
├── requirements.txt
└── README.md
```

---

## 安装依赖

```bash
cd cf-email-webhook
pip install -r requirements.txt
```

推荐使用虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 启动服务

```bash
python main.py
```

启动后会同时监听两个端口：

| 服务 | 地址 | 说明 |
|------|------|------|
| Webhook 接收 | `http://0.0.0.0:8080` | 接收 Cloudflare 转发的邮件 |
| 网页预览 | `http://0.0.0.0:8089` | 浏览器访问查看邮件 |

### 环境变量（可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WEBHOOK_PORT` | `8080` | Webhook 服务端口 |
| `VIEWER_PORT` | `8089` | 预览服务端口 |
| `EMAIL_DATA_PATH` | `data/emails.json` | 邮件数据文件路径 |
| `WEBHOOK_API_BASE` | `http://127.0.0.1:8080` | 预览服务调用 API 的地址 |

示例：

```bash
WEBHOOK_PORT=8080 VIEWER_PORT=8089 EMAIL_DATA_PATH=/var/data/emails.json python main.py
```

---

## Cloudflare 邮件 Webhook 对接

### 方式一：Cloudflare Email Workers

1. 在 Cloudflare Dashboard → **Email** → **Email Routing** 中开启邮件路由。
2. 创建一个 **Email Worker**，在 Worker 中将邮件转发到本服务：

```javascript
export default {
  async email(message, env, ctx) {
    // 读取邮件内容
    const rawEmail = await new Response(message.raw).text();

    // 构造 webhook 请求体
    const payload = {
      from: message.from,
      to: message.to,
      subject: message.headers.get("subject") || "",
      body_text: rawEmail,   // 可进一步解析 MIME
    };

    await fetch("https://your-server.com:8080/webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }
};
```

3. 将 Worker 路由绑定到目标邮件地址。

### 方式二：第三方邮件 Webhook 服务

若使用 Mailgun、SendGrid、Postmark 等服务转发，将 webhook URL 设置为：

```
http://your-server-ip:8080/webhook
```

本服务兼容以下常见字段名，自动适配：

- 发件人：`from` / `sender` / `from_address` / `fromAddress`
- 收件人：`to` / `recipient` / `to_address` / `toAddress`
- 主题：`subject` / `Subject` / `title`
- 纯文本：`body_text` / `bodyText` / `text` / `plain` / `body`
- HTML：`body_html` / `bodyHtml` / `html`

---

## 访问网页预览

启动服务后，在浏览器中打开：

```
http://your-server-ip:8089/
```

- **邮件列表**：`http://your-server-ip:8089/`
- **邮件详情**：`http://your-server-ip:8089/email/<id>`

---

## 接口说明

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/webhook` | 接收邮件 webhook（JSON body）|
| `GET` | `/api/emails` | 获取所有邮件列表（JSON，倒序）|
| `GET` | `/api/emails/<id>` | 获取单封邮件详情 |
| `GET` | `/health` | 健康检查 |

---

## 测试 Webhook（curl 示例）

### 基础测试

```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "from": "Alice <alice@example.com>",
    "to": "bob@mysite.com",
    "subject": "Hello from Cloudflare",
    "body_text": "你好，这是一封测试邮件。\n第二行内容。",
    "body_html": "<p>你好，这是一封<b>测试邮件</b>。</p>"
  }'
```

成功响应：

```json
{
  "id": "1743390000_ab12cd",
  "message": "email saved",
  "success": true
}
```

### 带 CC/BCC 测试

```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"email": "alice@example.com", "name": "Alice"},
    "to": {"email": "bob@mysite.com", "name": "Bob"},
    "subject": "带抄送的邮件",
    "body_text": "正文内容",
    "cc": [{"email": "cc1@example.com", "name": "CC Person"}],
    "bcc": ["bcc@example.com"]
  }'
```

### 查询邮件列表

```bash
curl http://localhost:8080/api/emails
```

### 查询单封邮件

```bash
curl http://localhost:8080/api/emails/1743390000_ab12cd
```

---

## 生产部署建议

### 使用 systemd 管理进程

创建 `/etc/systemd/system/cf-email-webhook.service`：

```ini
[Unit]
Description=Cloudflare Email Webhook Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/cf-email-webhook
ExecStart=/opt/cf-email-webhook/venv/bin/python main.py
Restart=always
RestartSec=5
Environment=EMAIL_DATA_PATH=/var/data/emails.json

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable cf-email-webhook
systemctl start cf-email-webhook
```

### 使用 Nginx 反向代理（推荐）

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 网页预览
    location / {
        proxy_pass http://127.0.0.1:8089;
        proxy_set_header Host $host;
    }

    # webhook 接收（可单独配置域名或路径）
    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 注意事项

- 邮件数据以明文 JSON 存储，请确保服务器文件权限设置合理。
- 本服务无鉴权，建议在内网或通过 Nginx + Basic Auth 保护 8089 预览端口。
- 8080 Webhook 端口建议通过防火墙只开放给 Cloudflare IP。
