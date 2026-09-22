# WeCom Context

面向 macOS 的企业微信本地上下文 Agent 应用。项目从企业微信本地数据中构建经过身份校验的文本与图片上下文，并注入 Pi Agent，用于基于私有资料的问答和总结。

## 特性

- Tauri 2 + Rust 桌面应用
- Python Sidecar 负责本地数据解析、快照、上下文准备和图片处理
- Pi RPC Agent 持久会话与上下文注入
- 企业账户、数据集、快照和会话多级身份校验
- Pi Connector：在 Pi 中读取企业微信上下文
- 本地优先：不部署网络服务器，不自动读取或注入企业微信内容
- 文本和图片上下文支持，带缓存校验和结构化错误码

## 项目结构

```text
apps/desktop/       Tauri 前端和 Rust 后端
packages/           Pi Connector 源码
sidecar/            Python 数据处理层和测试
contracts/          IPC 与 Sidecar JSON Schema 契约
scripts/            构建、资源暂存和发布脚本
phase2/             Sidecar 辅助模块
third_party/        第三方依赖源码与许可声明
docs/               离线安装说明
```

## 开发环境

当前发布流程面向 Apple Silicon macOS，要求：

- macOS arm64
- Node.js >= 22.19.0
- Python 3.13
- Bun 或 npm
- Rust、Tauri CLI
- 已安装并可调用的 Pi Coding Agent
- 如需取钥和客户端图片处理，需要企业微信及对应 macOS 权限

## 本地检查

```bash
npm install
npm run check
python3 contracts/validate_contracts.py
```

Sidecar 测试需要先创建并安装项目约定的 Python 虚拟环境：

```bash
python3 -m venv sidecar/.venv
sidecar/.venv/bin/pip install -r sidecar/requirements.txt
sidecar/.venv/bin/python -m unittest discover -s sidecar/tests -p 'test_*.py'
```

## 构建 macOS 应用

发布脚本会从本机暂存 Pi Runtime、Pi Connector、Sidecar 和 Vault 资源；这些生成目录不纳入版本库：

```bash
python3 scripts/build_release.py
```

构建产物位于 `dist/`。发布前请确认没有把 API Key、企业微信密钥、快照、聊天记录、图片或本机配置加入提交。

## 隐私与安全

本项目处理的是用户本机企业微信数据。使用前必须确认：

1. 当前企业微信登录账户与目标数据集一致；
2. 仅选择明确授权的企业和会话；
3. 不将 `keys/`、`snapshots/`、`exports/`、聊天记录或图片上传到 GitHub；
4. 不在配置文件、日志、截图和 Issue 中发布 API Key 或数据库密钥；
5. 了解 macOS 辅助功能、自动化和完全磁盘访问权限的影响。

## 许可

第三方依赖和归属信息见 `apps/desktop/src-tauri/resources/THIRD_PARTY_NOTICES.txt` 与 `third_party/`。当前仓库包含个人学习、研究和非商业工作流限制声明；正式发布前请根据社区版目标确认项目级许可证和第三方依赖的再分发权限。
