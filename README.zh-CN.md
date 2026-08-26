# cursor2api

[English](README.md)

用 Anthropic Messages API 和 OpenAI Chat Completions API 调用已登录 Cursor
账号里的模型。

非官方代理：把 Cursor 的 agent 协议接到 Anthropic / OpenAI 接口上，并带 HTTP
CONNECT 出站、未结束工具轮次的 usage 估算、工具调用活会话续接，以及 **Grok Bot
（`sand`）额度池切换**。

- `POST /v1/messages`、`POST /v1/chat/completions`，支持流式和非流式
- 账号能用的所有模型，`GET /v1/models` 列出
- 工具调用、图片、PDF、thinking/reasoning、用量统计
- 用 Cursor API key 或浏览器授权登录
- 可选 `sand/` / `bot/` 模型名前缀，走 Grok Bot 独立周额度

## 免责声明

本项目是独立的、非官方的实验性工具，与 Cursor（Anysphere）、Anthropic、OpenAI、
xAI 没有任何关联，也未获得它们的认可或支持；相关名称仅用于描述接口格式。

它依赖的是**私有、未公开且带版本校验的协议**。Cursor 可以随时改协议、收紧客户端
身份校验，或处理账号。使用它可能与 Cursor 以及背后模型提供方的服务条款冲突。
如何使用、以及账号风险，由你自行承担。本项目不提供任何形式的担保，详见
[LICENSE](LICENSE)。

**不要把这个代理暴露到公网。** 绑定 localhost，设置 `API_KEY`，永远不要把凭据
提交进 git。

## 安装

```bash
git clone https://github.com/nekopara-ai/cursor2api.git
cd cursor2api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

需要 Python 3.9+ 和 `h2`。

## 授权

三选一：

```bash
# 1. https://cursor.com/dashboard 上的 API key
export CURSOR_API_KEY=crsr_...

# 2. 浏览器 PKCE 登录（写入 ~/.config/cursor2api/credentials.json，权限 0600）
python -m cursor2api login

# 3. 复用本机 Cursor CLI 的 ~/.config/cursor/auth.json
#    默认开启：CURSOR2API_USE_CLI_AUTH=1
```

`serve` 自己也能发起登录：没有任何凭证时会打印授权链接。后台服务则通过
`GET /login` 拿链接，授权完成后自动生效。

```bash
python -m cursor2api status    # 当前用的是哪种凭证
python -m cursor2api logout    # 删除本地凭证文件
```

## 使用

```bash
python -m cursor2api serve            # http://127.0.0.1:8787
```

Anthropic 客户端（含 Claude Code）：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=sk-local

curl -s localhost:8787/v1/messages -H 'content-type: application/json' -d '{
  "model": "claude-sonnet-4-5", "max_tokens": 256,
  "messages": [{"role": "user", "content": "hello"}]}'
```

OpenAI 客户端，同一个端口：

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=sk-local

curl -s localhost:8787/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "gpt-5.6-sol", "stream": true,
  "messages": [{"role": "user", "content": "hello"}]}'
```

路由：`POST /v1/messages`、`POST /v1/messages/count_tokens`、
`POST /v1/chat/completions`、`GET /v1/models`、`GET /v1/models/{id}`、
`GET /login`、`GET /health`。

## 额度池：`cli` 与 `sand`（Grok Bot）

Cursor 按请求宣告的**客户端身份**记账，与模型名、命中哪个主机名无关。

| `x-cursor-client-type` | 含义 | 记账 |
|---|---|---|
| `cli`（默认） | Cursor CLI / 本代理原先的身份 | 套餐 included + bonus |
| `sand` | Grok Bot 桌面端（`com.anysphere.sand`） | 独立的 Grok Bot 周额度 |

同一个 Cursor access token，同一条 `agent.v1.AgentService/Run` 流，只改
client-type 头。

按请求在模型名前加前缀：

```bash
# 套餐额度（默认）
{"model": "claude-opus-5", ...}

# Grok Bot 周额度 —— 套餐 API 额度打满时仍然可用
{"model": "sand/claude-opus-5", ...}
{"model": "bot/gpt-5.2", ...}
{"model": "grokbot/composer-2.5", ...}

# 即使默认改成了 sand，也能强制走套餐池
{"model": "cli/grok-4.6", ...}
```

或者设进程级默认：

```bash
export CURSOR2API_CLIENT_TYPE=sand
```

**硬约束：** `x-cursor-client-version` 必须继续是 **Cursor CLI** 的版本号
（默认 `cli-2026.08.11-e8db854`，可用 `CURSOR_CLI_VERSION` 覆盖）。在这条流上填
Grok Bot 桌面端的 `0.18.0` 会直接 `permission_denied`。服务端用版本头校验传输
通道，用 client-type 选额度桶，两者是独立判断。

Grok Bot 权限（2026-08-26 起）覆盖 Cursor Pro / Pro+ / Ultra 以及对应的
SuperGrok 套餐。用自己的 token 确认：

```bash
curl -s -X POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandAccessStatus \
  -H "authorization: Bearer $CURSOR_ACCESS_TOKEN" \
  -H "content-type: application/json" \
  -H "connect-protocol-version: 1" \
  -H "x-cursor-client-type: sand" \
  -d '{}'

curl -s -X POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus \
  -H "authorization: Bearer $CURSOR_ACCESS_TOKEN" \
  -H "content-type: application/json" \
  -H "connect-protocol-version: 1" \
  -H "x-cursor-client-type: sand" \
  -d '{}'
```

`GetSandAccessStatus` 应为 `SAND_ACCESS_STATE_GRANTED`。
`GetSandUsageStatus` 返回 `usagePercent`、`nextResetTimestampUtc`（每周重置）
和 `grokPlanLabel`。included 用尽后可能转到 on-demand 实付，用之前请到 Cursor
的 spending 页面确认硬限额。

这是非官方的客户端身份切换。如果 Cursor 开始把 `sand` 和真实 machine checksum、
或桌面端版本号绑死，这个前缀会立刻失效。

细节见 [docs/usage-pools.md](docs/usage-pools.md)。

## 模型

`GET /v1/models` 返回的是账号自己的模型目录。`model` 支持这些写法：

| 写法 | 例子 |
|---|---|
| 基础模型 | `claude-fable-5`、`gpt-5.6-sol`、`gemini-3.1-pro`、`kimi-k3` |
| Cursor 变体 | `claude-fable-5-thinking-xhigh`、`composer-2.5-fast` |
| 别名 | `fable`、`sonnet-latest`、`opus`、`codex` |
| 显式参数 | `claude-sonnet-5[thinking=false,effort=max]` |
| 其它厂商 id | `claude-3-5-sonnet-20241022`、`gpt-4o`（映射到最接近的模型） |
| 额度池前缀 | `sand/claude-opus-5`、`bot/grok-4.6`、`cli/composer-2.5` |

不认识的 id 回落到 `DEFAULT_MODEL`。

## 配置

见 [.env.example](.env.example)。常用变量：

| 变量 | 默认 | 含义 |
|---|---|---|
| `BIND` / `PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `API_KEY` | 空 | 要求本地客户端带 `x-api-key` / bearer |
| `DEFAULT_MODEL` | `claude-fable-5` | 回落模型 |
| `CURSOR_API_KEY` | — | dashboard 的 `crsr_...` |
| `CURSOR2API_CREDENTIALS` | `~/.config/cursor2api/credentials.json` | OAuth 存储 |
| `CURSOR2API_CLIENT_TYPE` | `cli` | 默认额度池 |
| `CURSOR_CLI_VERSION` | `cli-2026.08.11-e8db854` | `x-cursor-client-version` |
| `CURSOR2API_PROXY` / `https_proxy` | — | HTTP/2 流的 HTTP CONNECT 代理 |
| `CURSOR2API_LIVE_TTL` | `150` | 工具调用活会话保留秒数 |
| `CURSOR2API_WEB` | `1` | 打开 Cursor 服务端搜索/抓取 |
| `CURSOR2API_THINKING` | `auto` | 何时请求 reasoning |
| `IDLE_STOP` | `180` | 兜底：流带不关闭时强制结束 |
| `FIRST_TIMEOUT` | `90` | 上游从未给出任何回复就切断 |
| `FIRST_OUTPUT_TIMEOUT` | `240` | 流已经热了但没有 text/thinking/tool_use 就切断 |

## 架构（简）

```
client (Anthropic / OpenAI JSON)
        │  HTTP/1.1  :8787
        ▼
cursor2api.server  ── 工具调用活会话 (_live_sessions)
        │  Connect+protobuf, HTTP/2
        ▼
agentn.global.api5.cursor.sh  /agent.v1.AgentService/Run
```

- `h2stream.py`：双向 HTTP/2（Run RPC 在半关闭请求上只会心跳）。可选
  `CURSOR2API_PROXY` HTTP CONNECT。
- `session.py`：protobuf 字段号、client-type 头、工具/沙箱循环。
- `server.py`：Anthropic + OpenAI 门面；`turn_ended` 未到时按字符估算 usage；
  `tool_use` 轮次的活流续接。
- `auth.py`：API key 兑换或 PKCE；token 从不写入仓库。
- `models.py`：`AvailableModels` 目录与别名。

协议字段号见 [docs/protocol.md](docs/protocol.md)。

## 说明与已知限制

- `temperature`、`top_p`、`top_k`、`cache_control`、`n`、`seed`、
  `response_format` 在上游没有对应项，直接忽略。
- thinking 内容是 Cursor 给的摘要，Anthropic 的 `signature` 恒为 `""`。
- 联网搜索用的是 Cursor 自己的服务端工具（`server_tool_use` + 标题和链接）。
- 每个**新**会话都有 Cursor agent harness（大约 12k–25k input tokens）。活会话
  续接命中时，工具循环的下一轮不再重放这段 harness。
- 极小图片（如 16×16）可能被 429；正常截图可以。
- 限流来自 Cursor 账号，以 `429` + `retry-after` 返回。
- 账号未开通的模型会返回 `403 permission_error`。

## 测试

先启动服务，然后：

```bash
python tests/test_api.py
python tests/test_openai.py
```

测试会打真实 Cursor 账号。套餐额度用尽时会失败，除非把 `MODEL` 设成账号还能用
的 `sand/...`。

## 许可

MIT，见 [LICENSE](LICENSE)。
