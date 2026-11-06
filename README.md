# rolling-context-bug-fix

## 问题背景
- Termux 上 / 子进程随脚本退出被回收，导致 proxyerror
- 修复方案：用 runit (runsvdir) 托管 proxy，解决跨调用存活

## 修复内容
1. Interpreter selection：优先有  的 python3.14
2. runit integration： 服务在 
3. hooks/start-proxy.sh：优先 ok: run: rc-proxy: (pid 13406) 410s，降级到 plain nohup

## 验证结果
- 5588/5590 双端 OPEN
- 上游连通 code=200
- 自愈：kill 代理后 runit 5s 自动重启
- 跨调用存活验证通过
