# rolling-context-bug-fix

## 问题背景

`rolling-context` 插件（v1.13.4，来自 `NodeNestor/nestor-plugins`）在 x86 环境安装后，
Claude Code 经常报 **PROXYERROR**。本文档记录已验证的根因与修复。

## 根因（按发现顺序）

### 1. 代理进程被会话回收（所有平台）

`start-proxy.sh` 用 `( cd DIR && nohup python server.py & )` 启动代理。hook 脚本退出时，
子进程被系统回收，代理随之死亡 —— 端口 5588/5590 关闭，所有请求报 PROXYERROR。

- **Termux**：用 `runit`（`runsvdir`）托管，服务随 `runsvdir` 长驻。
- **WSL / Linux / macOS**：`nohup ... &` 在 SSH 会话退出时仍会被回收。
  必须用 `setsid ... < /dev/null &` 让进程进入独立会话。见 `scripts/fix-proxy.sh`。

### 2. venv 缺 `certifi` —— TLS 信任库为空（WSL / 部分 Linux、macOS）

代理 venv（`~/.claude/rolling-context-venv`）若只装了 `cryptography` 而没有 `certifi`，
且该 Python 构建不加载 OS CA store，则：

```
ssl.create_default_context().cert_store_stats()  ->  {'x509_ca': 0}
```

代理连上游（`https://<gateway>`）时必然 `CERTIFICATE_VERIFY_FAILED`，
Claude Code 把它显示为 PROXYERROR。日志特征：

```
certifi CA bundle unavailable (No module named 'certifi'); trusting only the OS CA store for upstream TLS.
Outbound TLS trust store is EMPTY: ... Upstream requests will fail CERTIFICATE_VERIFY_FAILED
```

修复：`~/.claude/rolling-context-venv/bin/pip install certifi`，装完 `x509_ca` 应 > 0。

> 注意：不要只装到系统 `python3`。代理跑在 venv 里，必须装进 venv。

### 3. 上游网关模型名不匹配

部分网关（如 `116.205.124.216:3456`）不接受 `model-fable` 等名字，返回
`400 Model ... is not configured`。确认 `settings.json` 的 `model` 与
`ANTHROPIC_MODEL` 是网关实际支持的名字。

## 修复方案

| 平台 | 进程托管 | 关键命令 |
|------|----------|----------|
| Termux | runit (`runsvdir`) | `runit/rc-proxy/run` |
| WSL / Linux | `setsid` 或 systemd | `scripts/fix-proxy.sh` |
| macOS | launchd | 见下方示例 |
| Windows | 服务 / NSSM / 计划任务 | — |

### 一键修复（WSL / Linux）

```bash
cd ~/rolling-context-bug-fix
./scripts/fix-proxy.sh
```

脚本会：装 certifi → 校验 CA 根数 → 停掉假死进程 → 用 `setsid` 启动代理 → 健康检查。

### 开机自启（WSL）

WSL1 不支持 systemd（`systemctl` 为 offline）。可行做法是 Windows 计划任务，
在用户登录时执行：

```
wsl.exe -e bash -lc "cd /home/user/rolling-context-bug-fix && ./scripts/fix-proxy.sh"
```

`scripts/wsl-autostart.sh` 是该调用的包装。

### macOS launchd 示例

```xml
<!-- ~/Library/LaunchAgents/com.rolling-context.proxy.plist -->
<plist version="1.0"><dict>
  <key>Label</key><string>com.rolling-context.proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/&lt;you&gt;/.claude/rolling-context-venv/bin/python</string>
    <string>/Users/&lt;you&gt;/.claude/plugins/cache/nestor-plugins/rolling-context/1.13.4/proxy/server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/&lt;you&gt;/.claude/plugins/cache/nestor-plugins/rolling-context/1.13.4/proxy</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/rolling-context-proxy.log</string>
  <key>StandardErrorPath</key><string>/tmp/rolling-context-proxy.err.log</string>
</dict></plist>
```

## 验证

```bash
# 1) 代理直连
curl -s -o /dev/null -w "%{http_code}
" http://127.0.0.1:5588/

# 2) MITM 前端（5590）—— 只接受 CONNECT，明文 GET 返回空是正常的
python3 -c "import socket;s=socket.socket();s.settimeout(3);print(s.connect_ex(('127.0.0.1',5590))==0)"

# 3) 端到端（带 token）
curl -s -m 15 -w "
%{http_code}
" http://127.0.0.1:5588/v1/messages   -H "content-type: application/json" -H "x-api-key: <TOKEN>"   -d '{"model":"<MODEL>","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}'
```

**判读**：返回 200/400/401 都是链路通（代理错误会表现为连接被断开、空响应、
`AttributeError` 崩溃日志）；返回 000 或 `CERTIFICATE_VERIFY_FAILED` 才是未修复。

## 目录结构

```
rolling-context-bug-fix/
├── README.md
├── hooks/start-proxy.sh        # 补丁后的 hook（解释器选择 + runit/setsid）
├── runit/rc-proxy/run          # Termux runit 服务定义
└── scripts/
    ├── fix-proxy.sh            # WSL/Linux 一键修复（certifi + setsid）
    └── wsl-autostart.sh        # Windows 计划任务入口包装
```

## 已知环境信息

- 上游网关：`http://116.205.124.216:3456`（siliconflow `Qwen/Qwen3-8B`、gemini `入梦 Flash`）
- 代理：5588（明文入口）+ 5590（MITM 前端，HTTPS_PROXY 实际指向）
- WSL1：PID1 为 `/init`，无 systemd；`ss -tln` 可能不显示端口，用 curl 探测为准
