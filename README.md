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

### 4. `http://` 上游 + `HTTPS_PROXY` 指向 MITM → ECONNRESET / connection dropped

**这是最隐蔽的一个：代理本身完全正常，但 Claude Code 一启动就报 connection dropped。**

`wire.py`（被 SessionStart hook 调用）会无条件把 `HTTPS_PROXY` 写成 MITM 前端
（`http://127.0.0.1:5590`）。MITM 前端只终止 TLS：它只接受 `CONNECT`，对明文
HTTP 请求直接关掉连接（`mitm_frontend._read_connect_line`）。

Claude Code 只有在目标 URL 是 `https://` 时才发 `CONNECT`。当上游是明文
`http://`（自建网关常见，如 `http://116.205.124.216:3456`）时，Claude Code 发的是
普通绝对路径 `POST`，MITM 读到首行发现不是 CONNECT 就关闭 →
`ECONNRESET: The socket connection was closed unexpectedly` → 用户看到
"connection dropped" / PROXYERROR。

**诊断特征：**
- 代理日志里完全没有 `[MSG]` 记录（请求根本没到 rolling-context）
- `curl http://127.0.0.1:5588/v1/messages` 返回 200（代理正常）
- `curl -x http://127.0.0.1:5590 ...` 返回 000（MITM 丢弃明文）
- 改 `settings.json` 的 `HTTPS_PROXY=5588` 后一启动又被改回 5590（wire.py 覆写）

**修复：** 打 `patches/wire-http-upstream.patch`。它让 `wire.py` 按终端上游的
scheme 选择入口：`https` → MITM（可拦截重写）；`http` → core（5588，明文也能
压缩转发）。

```bash
cd ~/.claude/plugins/cache/nestor-plugins/rolling-context/1.13.4/proxy
cp -p wire.py wire.py.orig
patch -p1 < ~/rolling-context-bug-fix/patches/wire-http-upstream.patch
```

验证：

```bash
python3 wire.py --name rolling-context --settings ~/.claude/settings.json
python3 -c "import json;print(json.load(open('/home/user/.claude/settings.json'))['env']['HTTPS_PROXY'])"
# 应输出 http://127.0.0.1:5588
```

### 5. 请求进入 RAW 分支、缓存 token 漏计或重复计数（2026-11-12）

新增补丁：`patches/messages-routing-and-usage.patch`，基于 rolling-context **v1.13.4**，包含：

- 识别 HTTP 代理的绝对 URL 请求，将 `/v1/messages?beta=true` 交给压缩处理器，保留查询参数。
- 合并 `message_start` / `message_delta` 中的输入和缓存用量，最终事件只包含输出用量时不丢失先前统计。
- 支持 `data:` 后无空格的 SSE，以及非流式 JSON 响应的用量解析。
- 原生 Anthropic 输入、缓存读取、缓存写入按互斥字段相加；旧 cc-router 若保留 OpenAI `input_tokens_details`，按显式总输入计数，不重复相加。没有明确字段标记时不猜测协议语义。
- 对非法、缺失、字符串和浮点 token 用量容错，不把未知缓存状态当作零命中。
- 新增 `proxy/usage.py`、8 项用量测试与 14 项路由/本地 mock 端到端测试；测试不调用真实模型 API。

此补丁与上面的 `wire-http-upstream.patch` 互补：前者修复 **server 路由与统计**，后者选择 **HTTP/HTTPS 代理入口**。不能只修改 cc-router 就认为插件路由问题也已修复。

#### 应用到未打过本节补丁的 v1.13.4

在插件根目录（不是 `proxy` 子目录）执行；替换路径中的 marketplace 名称以匹配实际安装：

```bash
PLUGIN="$HOME/.claude/plugins/cache/nestor-plugins/rolling-context/1.13.4"
PATCH="$HOME/rolling-context-bug-fix/patches/messages-routing-and-usage.patch"
cd "$PLUGIN"
patch --dry-run --forward -p1 < "$PATCH"
# 仅在 dry-run 成功后执行：
cp -p proxy/server.py "proxy/server.py.bak-$(date +%Y%m%d-%H%M%S)"
patch --forward -p1 < "$PATCH"
python -I tests/test_usage.py
python -I tests/test_absolute_uri.py
```

若已安装本仓库的部分或全部路由/用量修复，dry-run 可能失败。不要强制套用或忽略冲突，应先比较本地改动；升级到其他插件版本也需要重新检查。

#### 验证结果与加载边界

2026-11-12：本地 8 项用量测试、14 项路由/压缩测试、67 项开关检查以及既有 wire/transport-flip 检查通过。mock 下一轮会话从 **21,536 → 2,414 字符**，并验证 800 总输入 / 600 缓存不被错算成 1,400 而提前触发压缩。

补丁应用后需重启代理才能加载，**不会自动重启或更改 settings.json**。代理正在压缩或持有内存摘要时，不要直接杀进程；先等待在途任务结束，理解重启会清空未持久化的摘要。插件缓存更新可能覆盖本地补丁。

相关 cc-router 修复见用户仓库 `qq422162318/cc-router`：Responses/Chat Completions 输出互斥 Anthropic 输入字段，反向 Responses 还原总输入；转发、探测与 OAuth 统一从设置读取出口代理，并在成功重启代理服务时原子替换客户端。这不代表远程实例已部署新代码。

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
    └── ...
patches/
    └── wire-http-upstream.patch # http 上游时 HTTPS_PROXY 指向 core 而非 MITM
    └── wsl-autostart.sh        # Windows 计划任务入口包装
```

## 已知环境信息

- 上游网关：`http://116.205.124.216:3456`（siliconflow `Qwen/Qwen3-8B`、gemini `入梦 Flash`）
- 代理：5588（明文入口）+ 5590（MITM 前端，HTTPS_PROXY 实际指向）
- WSL1：PID1 为 `/init`，无 systemd；`ss -tln` 可能不显示端口，用 curl 探测为准
