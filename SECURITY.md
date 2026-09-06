# Security Policy

## 项目边界

PicoClaw 当前是本地学习原型，不应直接部署到不受信任的多用户生产环境。

## 密钥

- 不要把真实 API Key 写入源码、README、Trace、Report 或 Git 历史。
- 使用当前进程的环境变量传递 Provider 密钥。
- `.env` 已被 Git 忽略；`.env.example` 只能包含占位值。

## 工具与审批

- 真实仓库默认使用 `--approval ask`。
- `--approval auto` 仅用于可丢弃、已隔离的演示工作区。
- MCP Server 配置属于操作员信任边界；不要接入来源不明的命令或 Server。
- Verifier 命令仍受 Workspace 开发命令白名单限制。

## 报告问题

公开仓库建立后，请通过 GitHub Security Advisory 私下报告可能导致路径逃逸、未授权命令执行、密钥泄漏或审批绕过的问题。不要在公开 Issue 中附带真实密钥或敏感仓库内容。
