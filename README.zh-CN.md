# cursor2api

[English](README.md)

`cursor2api` 是一个实验性的本地网关：它把已登录 Cursor 账号的一部分能力转换为
Anthropic Messages API 和 OpenAI Chat Completions API 的常见接口形式。

> [!WARNING]
> 这是独立、非官方的项目，与 Cursor（Anysphere）、Anthropic、OpenAI、xAI
> 以及任何模型提供方均无隶属或背书关系。项目依赖私有、未公开、对版本敏感的
> 上游协议，可能随时失效。使用前请自行确认账号所适用的服务条款。

本项目面向本地开发、互操作实验和协议研究。它不是任一厂商 API 的完整替代实现，
也不适合作为公网或多租户服务运行。

## 项目提供什么

- Anthropic 风格的 `POST /v1/messages`，包括流式响应、工具调用、图片、文档和
  reasoning 摘要。
- OpenAI 风格的 `POST /v1/chat/completions`，包括流式响应和函数调用。
- 通过 `GET /v1/models` 获取当前账号对应的模型目录。
- Cursor API key 兑换、浏览器登录，以及可选复用 Cursor CLI 凭据。
- 面向 Cursor agent 流的双向 HTTP/2 传输，并支持可选的 HTTP CONNECT 出站。
- 在及时返回完整匹配的工具结果集合时，续接仍存活的工具调用会话。
- 通过模型名前缀进行实验性的逐请求客户端身份路由。

## 能力边界

| 范围 | 状态 | 重要边界 |
|---|---|---|
| Anthropic Messages | 支持常用子集 | 转换常见消息、内容块、工具和 SSE 结构，不代表完整 API 兼容。 |
| OpenAI Chat Completions | 支持常用子集 | 常见 chat 与 function-calling 结构会先转换到项目内部的 Anthropic 风格表示。 |
| `max_tokens` 与停止序列 | 本地近似 | 使用文本匹配和约四字符一个 token 的上限估算在本地截断。 |
| Token 计数与 usage | 本地近似 | Cursor 并非在每个工具轮次都提供等价的单请求计数，因此部分结果会被估算或钳制。 |
| 采样和响应控制 | 接受但无等价行为 | `temperature`、`top_p`、`seed`、penalties、`n`、`response_format` 等参数没有对应的上游控制。 |
| 模型别名 | 尽力解析 | 未知模型名会回退到 `DEFAULT_MODEL`，而不是返回参数校验错误。 |
| 客户端身份前缀 | 实验性 | `sand/`、`bot/`、`grokbot/`、`cli/` 会改变声明的客户端类型，上游不保证长期接受。 |
| 上游协议 | 不稳定 | 字段号、必需请求头和行为都可能随 Cursor 版本变化。 |

准确的请求与响应行为见 [API 参考](docs/api-reference.md)。

## 快速开始

需要 Python 3.9 或更高版本、可访问 Cursor 的网络环境，以及你有权使用的 Cursor
账号。

```bash
git clone https://github.com/nekopara-ai/cursor2api.git
cd cursor2api
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

选择一种上游凭据来源：

```bash
# 方式 A：Cursor API key
export CURSOR_API_KEY='crsr_...'

# 方式 B：浏览器授权，凭据存到 ~/.config/cursor2api/
python -m cursor2api login
```

启动只监听本机的服务，并用本地 key 保护 POST 请求：

```bash
export API_KEY='local-development-key'
python -m cursor2api serve
```

服务默认监听 `http://127.0.0.1:8787`。仓库中的
[`.env.example`](.env.example) 只是配置参考模板；程序**不会自动加载** `.env`
文件。请在启动前把变量导出到进程环境，或交给进程管理器加载。

查看当前模型 ID：

```bash
curl -s http://127.0.0.1:8787/v1/models
```

当前实现中，`GET` 和 `HEAD` 路由不受 `API_KEY` 保护。即使配置了本地 API key，
也应只在可信接口上监听。

## 发送请求

Anthropic Messages：

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: local-development-key' \
  -d '{
    "model": "claude-fable-5",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

OpenAI Chat Completions：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer local-development-key' \
  -d '{
    "model": "claude-fable-5",
    "stream": true,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

请把示例模型替换为 `/v1/models` 返回的 ID。模型目录请求成功，不代表具体模型调用
一定已获授权；当上游目录请求失败时，该端点还可能返回内置的 fallback 目录。

## 文档导航

| 文档 | 内容 |
|---|---|
| [上手指南](docs/getting-started.md) | 安装、认证、CLI 命令和首次请求 |
| [配置参考](docs/configuration.md) | 完整环境变量和凭据优先级 |
| [API 参考](docs/api-reference.md) | 路由、请求结构、流式响应、错误和兼容边界 |
| [架构](docs/architecture.md) | 组件、请求流程、工具调用、活会话和信任边界 |
| [运维](docs/operations.md) | 部署边界、健康检查、日志、超时与故障排查 |
| [协议笔记](docs/protocol.md) | 对版本敏感的 Connect/protobuf 传输说明 |
| [实验性客户端身份路由](docs/usage-pools.md) | `cli`、`sand` 路由行为和风险 |
| [贡献指南](CONTRIBUTING.md) | 开发流程和 Pull Request 要求 |
| [安全策略](SECURITY.md) | 私密报告方式和部署安全建议 |

详细文档目前以英文维护；中英文 README 的功能、风险和入口保持对应。

## 安全边界

- 保持默认 `BIND=127.0.0.1`；服务本身不提供 TLS。
- 为 POST 路由设置 `API_KEY`，但不要把它视为 GET 或 HEAD 路由的保护。
- `/health` 只表示进程能够响应，不会验证凭据、模型目录或上游推理链路。
- 凭据必须留在仓库外。浏览器登录默认写入
  `~/.config/cursor2api/credentials.json`，文件权限为 `0600`。
- 不要在不可信流量下启用 `SANDBOX_SHELL=1`。兼容性 sandbox 不是操作系统级
  隔离边界。

发现漏洞时，请按 [SECURITY.md](SECURITY.md) 私密报告。

## 开发与测试

离线回归不需要 Cursor 账号：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python tests/test_timeouts_offline.py
```

`tests/test_api.py` 和 `tests/test_openai.py` 是在线集成脚本，需要另行启动服务、
可用凭据、网络访问和账号容量。提交修改前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可与商标

代码采用 [MIT License](LICENSE)。Cursor、Anthropic、OpenAI、xAI 及相关产品名
属于各自权利人，本项目仅用它们标识互操作目标。
