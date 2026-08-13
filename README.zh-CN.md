# cursor2api

[English](README.md)

用 Anthropic Messages API 和 OpenAI Chat Completions API 调用你 Cursor 账号里的模型。

- `POST /v1/messages`、`POST /v1/chat/completions`，支持流式和非流式
- 账号能用的所有模型，`GET /v1/models` 列出
- 工具调用、图片、PDF、thinking/reasoning、用量统计
- 用 Cursor API key 或浏览器授权登录

## 安装

    git clone <你的仓库>
    cd cursor2api
    pip install -r requirements.txt

需要 Python 3.9+ 和 `h2`。

## 授权

在 cursor.com/dashboard 建一个 key：

    export CURSOR_API_KEY=crsr_...

或者用浏览器授权：

    python -m cursor2api login

`serve` 自己也能做这件事：没有任何凭证时它会打印一个授权链接并等你确认。如果是后台
启动的服务，则通过 `GET /login` 拿链接，授权完成后自动生效：

    curl -s localhost:8787/login

token 存在 `~/.config/cursor2api/credentials.json`（权限 0600）。`status` 查看当前用的
是哪种凭证，`logout` 删除。

## 使用

    python -m cursor2api serve            # http://127.0.0.1:8787

Anthropic 客户端（含 Claude Code）：

    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=sk-local claude

    curl -s localhost:8787/v1/messages -H 'content-type: application/json' -d '{
      "model": "claude-sonnet-4-5", "max_tokens": 256,
      "messages": [{"role": "user", "content": "hello"}]}'

OpenAI 客户端，同一个端口：

    OPENAI_BASE_URL=http://127.0.0.1:8787/v1 OPENAI_API_KEY=sk-local

    curl -s localhost:8787/v1/chat/completions -H 'content-type: application/json' -d '{
      "model": "gpt-5.6-sol", "stream": true,
      "messages": [{"role": "user", "content": "hello"}]}'

路由：`POST /v1/messages`、`POST /v1/messages/count_tokens`、
`POST /v1/chat/completions`、`GET /v1/models`、`GET /v1/models/{id}`、`GET /login`、
`GET /health`。

常用配置：`PORT`、`BIND`、`API_KEY`（要求本地客户端带 `x-api-key`）、`DEFAULT_MODEL`。

## 模型

`GET /v1/models` 返回的是账号自己的模型目录。`model` 支持这些写法：

| 写法 | 例子 |
|---|---|
| 基础模型 | `claude-fable-5`、`gpt-5.6-sol`、`gemini-3.1-pro`、`kimi-k3` |
| Cursor 变体 | `claude-fable-5-thinking-xhigh`、`composer-2.5-fast` |
| 别名 | `fable`、`sonnet-latest`、`opus`、`codex` |
| 显式参数 | `claude-sonnet-5[thinking=false,effort=max]` |
| 其它厂商 id | `claude-3-5-sonnet-20241022`、`gpt-4o`（映射到最接近的模型） |

不认识的 id 回落到 `DEFAULT_MODEL`。

## 说明

- `temperature`、`top_p`、`top_k`、`cache_control`、`n`、`seed`、`response_format`
  在上游没有对应项，直接忽略；`stop_sequences`、`max_tokens`、`tool_choice` 是本地近似实现。
- thinking 内容是 Cursor 给的摘要，Anthropic 的 `signature` 恒为 `""`。
- 联网搜索用的是 Cursor 自己的服务端工具，转成 `server_tool_use` +
  `web_search_tool_result`，只有标题和链接。
- Cursor 的 agent 系统提示始终存在，会抬高 `input_tokens`。
- 限流来自 Cursor 账号，会以 `429` + `retry-after` 返回。
- 账号未开通的模型（部分模型需要在 Cursor 后台接受数据保留政策）会返回
  `403 permission_error`。

协议细节和字段号见 [docs/protocol.md](docs/protocol.md)。

## 测试

先启动服务，然后：

    python tests/test_api.py
    python tests/test_openai.py

## 免责声明

本项目是独立的、非官方的实验性工具，与 Cursor（Anysphere）、Anthropic、OpenAI 没有任何
关联，也未获得它们的认可或支持；相关名称仅用于描述接口格式。

它依赖的是私有、未公开且带版本校验的协议，随时可能失效。使用它可能与 Cursor 的服务条款
以及背后模型提供方的条款相冲突；如何使用、以及你自己账号的风险，由你自行承担。本项目不
提供任何形式的担保，详见 [LICENSE](LICENSE)。

## 许可

MIT。

## 仓库 topics

    cursor cursor-ai cursor-api anthropic-api claude openai-api openai-compatible
    anthropic-compatible llm-proxy api-proxy reverse-engineering protobuf connect-rpc
    python sse streaming function-calling claude-code
