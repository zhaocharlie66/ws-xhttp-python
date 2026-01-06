# Python XHTTP High-Performance Proxy

这是一个基于 Python `aiohttp` 和 `asyncio` 构建的高性能 XHTTP/WebSocket 代理服务器。

本项目是原 Node.js 高级版本（V27.0）的 **1:1 Python 复刻版**。它完美继承了所有的核心流控逻辑，旨在解决极其复杂的并发网络环境下的兼容性问题。

## 🌟 核心特性

* **全模式兼容**：完美支持 Xray 的 `stream-up`、`stream-none`、`packet-up` 和 `auto` 模式。
* **智能确认与分流 (Ack & Divert)**：针对 Packet 模式的短连接特征，实现了自动 ACK 和 GET 通道回退机制，彻底解决握手超时问题。
* **全量广播握手 (Total Broadcast)**：握手包同时写入 GET 和 POST 通道，确保“宁可多发，不可漏发”。
* **极致静默**：控制台日志级别设为 `CRITICAL`，生产环境运行无任何输出，隐蔽性极强。
* **伪装服务**：支持自定义 `index.html`，访问根路径时返回伪装页面，表现如同普通 Web 服务。
* **异步高性能**：基于 Python 原生 `asyncio` 事件循环，轻松处理高并发连接。

## 🛠 环境要求

* **Python**: 3.8 或更高版本
* **依赖库**: `aiohttp`

## 🚀 快速部署指南

### 方式一：Docker 部署（推荐）

Docker 是最干净、最快速的部署方式。

1. **准备文件**
在项目根目录创建 `Dockerfile`：
```dockerfile
FROM python:3.9-slim

WORKDIR /app

# 安装依赖
RUN pip install aiohttp

# 复制源代码和伪装页面
COPY server.py .
COPY index.html .

# 暴露端口
EXPOSE 3241

# 启动命令
CMD ["python", "server.py"]

```


*建议在同级目录下放一个 `index.html` 文件作为伪装页面。*
2. **构建镜像**
```bash
docker build -t xhttp-python .

```


3. **启动容器**
*请将 `your-uuid-here` 替换为你生成的 UUID。*
```bash
docker run -d \
  --name xhttp-server \
  --restart always \
  -p 3241:3241 \
  -e UUID="b389e09c-4e31-40da-a56c-433f507e615a" \
  -e PORT=3241 \
  xhttp-python

```



---

### 方式二：Linux Systemd 守护进程（VPS 推荐）

适合在 Ubuntu/Debian/CentOS 等 VPS 上长期运行。

1. **上传代码**
将 `server.py` 和 `index.html` (可选) 上传到服务器，例如 `/opt/xhttp/` 目录。
2. **安装依赖**
```bash
pip3 install aiohttp

```


3. **创建服务文件**
创建文件 `/etc/systemd/system/xhttp.service`：
```ini
[Unit]
Description=XHTTP Python Proxy Service
After=network.target

[Service]
Type=simple
User=root
# 请修改为你实际的 UUID
Environment="UUID=b389e09c-4e31-40da-a56c-433f507e615a"
Environment="PORT=3241"
# 如果不设置 WSPATH，默认取 UUID 前8位
# Environment="WSPATH=path"

# 确保工作目录正确，以便程序能读取 index.html
WorkingDirectory=/opt/xhttp
ExecStart=/usr/bin/python3 /opt/xhttp/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target

```


4. **启动服务**
```bash
systemctl daemon-reload
systemctl enable xhttp
systemctl start xhttp
systemctl status xhttp

```



---

### 方式三：手动运行（测试用）

1. **安装依赖**
```bash
pip install aiohttp

```


2. **准备伪装页面 (可选)**
在 `server.py` 同级目录下创建一个 `index.html` 文件。
3. **设置环境变量并运行**
* **Linux/Mac**:
```bash
export UUID="b389e09c-4e31-40da-a56c-433f507e615a"
export PORT=3241
python server.py

```


* **Windows (CMD)**:
```cmd
set UUID=b389e09c-4e31-40da-a56c-433f507e615a
set PORT=3241
python server.py

```





## ⚙️ 环境变量说明

| 变量名 | 说明 | 默认值 | 示例 |
| --- | --- | --- | --- |
| `UUID` | **必填**，用于鉴权的 UUID | (内置测试UUID) | `550e8400-e29b-41d4-a716-446655440000` |
| `PORT` | 服务监听端口 | `3241` | `8080` |
| `WSPATH` | XHTTP/WS 的路径 | UUID 前8位 | `mypath` |

## 🌐 伪装页面配置

服务端会自动检测同级目录下的 `index.html` 文件。

* **如果存在**：访问 `http://your-ip:port/` 时将显示该 HTML 内容。
* **如果不存在**：将显示默认的纯文本 "NodeJS Proxy Server is Running."。

建议随便下载一个简单的静态网页模板重命名为 `index.html` 放在旁边，以迷惑主动探测。

## 📱 客户端配置参考 (Xray/V2Ray)

本服务端支持 **VLESS** 和 **Trojan** 协议。

### VLESS + XHTTP (推荐)

这是性能最好且兼容性最强的配置方式，`mode` 建议设为 `auto`。

```json
{
  "outbounds": [
    {
      "protocol": "vless",
      "settings": {
        "vnext": [
          {
            "address": "你的服务器IP",
            "port": 3241,
            "users": [
              {
                "id": "b389e09c-4e31-40da-a56c-433f507e615a",
                "encryption": "none"
              }
            ]
          }
        ]
      },
      "streamSettings": {
        "network": "xhttp",
        "xhttpSettings": {
          "path": "/b389e09c", 
          "mode": "auto" 
        }
      }
    }
  ]
}

```

*注意：`path` 默认为 UUID 的前 8 位（不含横杠）。如果使用了 `WSPATH` 环境变量，请填写该变量的值。*

### WebSocket 模式

如果你需要使用传统的 WebSocket 模式，本服务端也完全兼容。

```json
"streamSettings": {
  "network": "ws",
  "wsSettings": {
    "path": "/b389e09c"
  }
}

```

## ⚠️ 注意事项

1. **静默运行**：启动后控制台**不会有任何输出**，这是正常现象（为了隐蔽）。请通过 `netstat -tlnp` 或 `docker ps` 确认端口是否在监听。
2. **安全性**：请务必修改默认的 `UUID`。
3. **防火墙**：请确保服务器防火墙放行了配置的 `PORT`。

## 📄 License

MIT License
