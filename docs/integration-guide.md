# DeepseekHarness 集成指南

本指南把 `qwen-voice-mcp` 接入 DeepseekHarness（DSH），从零到可调用。

---

## 0. 前置条件

| 项 | 要求 | 自检 |
|----|------|------|
| Python | 3.10+ | `python --version` |
| 依赖 | 已安装（`mcp` 必须 <2） | `qwen-voice-mcp doctor` |
| Qwen API Key | 有效 | 见 §2 |
| DSH | 已安装并可打开 | — |

---

## 1. 安装

```powershell
pipx install .          # 或：uv tool install .
qwen-voice-mcp doctor   # 每一项都应为 OK / 已配置 / 已注册
```

装成命令行工具后，客户端配置里只写 `qwen-voice-mcp`，不需要解释器绝对路径——
换机器、换虚拟环境都不会失效。

只有从源码调试时才需要知道项目路径：

```powershell
cd F:\MCP_Server\Qwen-TTS-MCP-DSH
python -c "import pathlib; print(pathlib.Path('server.py').resolve())"
```

---

## 2. 配置 API 密钥

推荐一条命令（隐藏输入、默认先联网验证、写入加密库）：

```powershell
qwen-voice-mcp setup
```

以下方式任选其一，优先级从高到低：

### 方式 A：命令行（推荐）

```powershell
qwen-voice-mcp setup                    # 隐藏输入
qwen-voice-mcp config set-key --stdin   # 从管道读，适合脚本
qwen-voice-mcp balance                  # 验证并查看余额
qwen-voice-mcp config show              # 查看生效配置
qwen-voice-mcp doctor                   # 体检：密钥来源、密钥库强度、客户端注册状态
```

### 方式 B：GUI 配置中心

```powershell
qwen-voice-mcp gui
```

界面说明：

```
Qwen Voice MCP — 配置中心
┌─ API 配置 ─────────────────────────────────┐
│ API Key:  [••••••••••••••••••••]   [验证]  │
│ 状态:     ● 已验证 — 账户余额 ¥12.50      │
└────────────────────────────────────────────┘
┌─ 服务配置 ─────────────────────────────────┐
│ 传输方式:  (o) stdio   ( ) streamable-http │
│ 监听端口:  [8765        ]  (HTTP 模式)     │
│ 默认音色:  [longanyang  ▼]                 │
│ 余额阈值:  [¥1.00       ]  (低于则告警)    │
└────────────────────────────────────────────┘
[保存配置] [测试连接] [清除密钥] [刷新模型]
```

点「验证」会先向 DashScope 发一次轻量请求确认密钥有效，通过后才写入加密库。
密钥以 AES-256-GCM 加密存于 `%APPDATA%\qwen-voice-mcp\vault.enc`，主密钥由
Windows 凭据管理器保管；没有凭据管理器的环境（WSL/Docker/CI）自动回退到同目录
密钥文件，`doctor` 会如实标明强度。

### 方式 C：环境变量 / .env

```powershell
$env:DASHSCOPE_API_KEY = "sk-xxxxxxxxxxxx"
```

也可以写在当前目录或配置目录的 `.env` 里（`QWEN_VOICE_NO_DOTENV=1` 可关闭）。
**环境变量优先级最高**：一旦设置，加密库里的值不会被使用。

---

## 3. 选择传输方式并接入 DSH

### 3.1 stdio 模式（本地，推荐）

DSH 会自己拉起子进程，无需手动启动服务。

最省事的写法（要求 `pipx install .` 装过，`qwen-voice-mcp` 在 PATH 里）：

```yaml
mcp_servers:
  qwen-voice:
    command: qwen-voice-mcp
    args: ["run", "--transport", "stdio"]
```

也可以让 `install` 直接打印这段：`qwen-voice-mcp print-config --client dsh`。

从源码运行时才需要写解释器路径（与本仓库的 `.mcp.json` 等价）：

```yaml
mcp_servers:
  qwen-voice:
    command: python
    args: ["-m", "qwen_voice_mcp", "run", "--transport", "stdio"]
```

### 3.2 streamable-http 模式（远程/常驻）

先手动启动服务：

```powershell
# 建议设置 Bearer Token
$env:QWEN_VOICE_HTTP_AUTH_TOKEN = "your-strong-random-token"
python server.py --transport streamable-http --port 8765
```

再配置 DSH：

```yaml
mcp_servers:
  qwen-voice:
    url: "http://127.0.0.1:8765/mcp"
    transport: "streamable-http"
    headers:
      Authorization: "Bearer your-strong-random-token"
```

> 服务默认只绑定 `127.0.0.1`。若确需监听其他地址，须显式开启非环回放行，
> 并务必配置 `QWEN_VOICE_HTTP_AUTH_TOKEN`。

### 3.3 通过 DSH 界面配置

1. 打开 Harness **设置 → MCP**
2. 点「添加服务器」
3. 按 §3.1 或 §3.2 填写 command / args / env（或 url / headers）
4. 保存并启用

---

## 4. 验证接入

在 DSH 会话中依次尝试：

| 输入 | 期望触发 | 期望结果 |
|------|---------|---------|
| 列出可用的语音模型 | `list_voice_models` | 返回模型清单（含 `verified` 标记） |
| 请把"你好世界"转成语音 | `synthesize_speech` | 返回 `audio_path`、时长、消耗估算 |
| 查询账户余额 | `check_balance` | 返回余额与阈值状态 |
| 用这个音频克隆音色（附文件） | `clone_voice` | 返回 `voice_id` 与克隆状态 |

不想占用额度时，可先用不发网络请求的方式确认工具已注册：

```powershell
python server.py --selftest
```

输出应包含四个工具名并打印 `SELFTEST OK`。

---

## 5. 工具映射

| MCP Tool | DSH 显示名称 | 参数 | 是否计费 |
|----------|-------------|------|---------|
| `list_voice_models` | 查询可用语音模型 | 无 | 否 |
| `synthesize_speech` | 文字转语音 | `text`(必), `model`, `voice`, `output_format` | 是 |
| `clone_voice` | 声音克隆 | `audio_path`(必), `voice_name`(必), `model`, `language_hints` | 是 |
| `check_balance` | 查询账户余额 | 无 | 否 |

`audio_path` 支持三种形式：本地文件路径、`data:audio/wav;base64,...` URI、裸 base64 字符串。

---

## 6. 模型清单与真实性

说明书里列举的模型 ID 未经离线证实，因此本服务**不把它们当作事实**：

- 内置清单在 `config.py` 的 `DEFAULT_MODELS` 中，可自行增删；
- `list_voice_models` 返回的每条都带 `verified` 字段：`null` = 未探测，`true/false` = 探测结果；
- 用真实密钥探测：

```powershell
qwen-voice-mcp verify-models
```

若某个模型 ID 在当前账户不可用，该条会标为 `verified: false`，调用时返回
`model_not_supported`，而不是抛未捕获异常。

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `auth_error` | 密钥无效 / 未配置 / 无该模型权限 | `qwen-voice-mcp setup`，或检查 `QWEN_VOICE_HTTP_AUTH_TOKEN` |
| `insufficient_balance` | 余额 ≤ 阈值 | 按返回消息中的提示为 Qwen 账户充值，或调低 `QWEN_VOICE_BALANCE_THRESHOLD` |
| `balance_unavailable` | 所有余额端点探测失败且 `strict_balance=true` | 关闭严格模式，或检查网络/接口可用性 |
| `model_not_supported` | 模型不在清单中 | 用 `list_voice_models` 查看可用项，或把它加入 `DEFAULT_MODELS` |
| `invalid_input` | 文本为空，或音频不合规 | 音频需 5–60 秒、wav/mp3/m4a/flac/ogg/aac、≤20MB |
| `rate_limited` | 触发限流 | 稍后重试 |
| `network_error` | 网络/超时 | 检查代理与连通性 |
| `upstream_error` | DashScope 服务端错误 | 稍后重试；错误详情在服务端日志 |
| DSH 里看不到工具 | 服务启动失败 / 路径错误 / 未注册 | 跑 `qwen-voice-mcp doctor`，再 `qwen-voice-mcp print-config --client dsh` 对齐配置 |
| `doctor` 显示 `mcp 包 │ 不兼容` | 装到了 mcp 2.x（`FastMCP` 已改名） | `pip install "mcp>=1.2,<2"` |
| `doctor` 显示 `未配置` 但明明存过密钥 | 进程里存在过期的 `DASHSCOPE_API_KEY`，它优先于加密库 | `Remove-Item Env:\DASHSCOPE_API_KEY` 后重开终端 |
| HTTP 模式 401 | Token 不一致 | 确认 `QWEN_VOICE_HTTP_AUTH_TOKEN` 与请求头一致 |

### 日志去哪里看

- **stdio 模式**：日志只写 **stderr**（stdout 保留给协议数据）。DSH 的 MCP 日志面板可见。
- **HTTP 模式**：日志输出到启动它的终端。
- 日志中的密钥一律以 `sk-abc…6789` 形式脱敏。

---

## 8. 安全建议

1. 用 GUI 或 `config set-key` 把密钥存入加密 vault，而不是写进明文配置或脚本。
2. HTTP 模式务必设置 `QWEN_VOICE_HTTP_AUTH_TOKEN`，并保持只绑定 `127.0.0.1`。
3. 不要与不可信进程共享 `DASHSCOPE_API_KEY` 环境变量。
4. 克隆音色涉及他人声音时，须先取得对方授权。
5. 定期清理输出目录中的合成音频（服务启动时会自动清理 24 小时前的临时文件）。
