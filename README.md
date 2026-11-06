# rolling-context-bug-fix

## 问题背景

在 Windows / Linux / macOS 等 x86 环境中，`rolling-context` 插件运行时经常挂掉，报 **PROXYERROR**。

根因：`start-proxy.sh` 用 `( cd DIR && nohup python server.py & )` 的方式启动代理。当 hook 脚本退出时，子进程被系统回收，代理进程随之死亡——端口 5588/5590 关闭，所有请求都报 PROXYERROR。

## 修复方案

### 1. 解释器选择（Windows / Linux / macOS 通用）

优先选择能 `import cryptography` 的 Python 解释器（MITM 前端需要它来生成本地 CA），避免选到装了 cryptography 但没装 cryptography 的 python3。

### 2. 长驻进程托管（平台无关）

| 平台 | 推荐方案 | 说明 |
|------|----------|------|
| **Linux / macOS** | `systemd` 或 `launchd` | 建服务，开机自启，崩溃自动重启 |
| **Windows** | Windows 服务 / `nssm` | 或用 Task Scheduler |
| **Termux** | `runit` (`runsvdir`) | Termux Boot 自启 |

本项目提供 `runit` 服务定义（Termux）和 `systemd` 服务模板（Linux），Windows 用户可参考 README 中的 NSSM 配置。

### 3. hooks/start-proxy.sh

- 优先 `sv start rc-proxy`（runit），降级到 plain `nohup`（非 Termux）
- 将启动窗口从 8s 提高到 20s（冷启动 python3.14 + cryptography 需要 ~5-9s）
- 启动后额外校验 MITM 前端（:5590）是否真正监听，避免"5588 健康但 5590 死"的假阳性

## 验证结果

- 5588 / 5590 双端口 OPEN
- 上游连通 code=200
- 自愈：kill 代理后 runit 5s 自动重启
- 跨调用存活验证通过

## 目录结构

```
bug-fixing/
├── README.md
├── hooks/start-proxy.sh     # 补丁后的 hook 脚本
├── runit/rc-proxy/run       # runit 服务定义（Termux）
└── systemd/                 # systemd 服务模板（Linux）
```

## 快速开始

### Termux

```bash
# 1. 确保 runit 在跑
termux-wake-lock
nohup runsvdir /data/data/com.termux/files/usr/var/service >> ~/.termux/boot/boot.log 2>&1 &

# 2. 创建服务
mkdir -p /data/data/com.termux/files/usr/var/service/rc-proxy/log/supervise
cp runit/rc-proxy/run /data/data/com.termux/files/usr/var/service/rc-proxy/run
chmod +x /data/data/com.termux/files/usr/var/service/rc-proxy/run
sv start rc-proxy
```

### Linux (systemd)

```bash
sudo cp systemd/rolling-context.service /etc/systemd/system/
sudo systemctl enable --now rolling-context
```

### Windows

```powershell
# 使用 nssm 或 Task Scheduler 托管
nssm install rolling-context "python" "server.py"
nssm start rolling-context
```

## 许可证

MIT
