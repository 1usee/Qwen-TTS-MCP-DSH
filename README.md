# Qwen Voice MCP

一个基于 [Model Context Protocol](https://modelcontextprotocol.io) 的 Python MCP Server，把
Qwen（DashScope）语音能力接入 **DeepseekHarness（DSH）**：

| MCP Tool | DSH 显示名称 | 触发场景 |
|----------|-------------|----------|
| `list_voice_models` | 查询可用语音模型 | 用户询问"有哪些语音模型可用" |
| `synthesize_speech` | 文字转语音 | 用户提交文字并要求语音输出 |
| `clone_voice` | 声音克隆 | 用户上传语音文件要求复制音色 |
| `check_balance` | 查询账户余额 | 用户询问余额，或计费前自动检查 |

设计说明书见 [`Qwen-MCP.md`](Qwen-MCP.md)，实现规格见
[`docs/superpowers/specs/2026-10-14-qwen-voice-mcp-design.md`](docs/superpowers/specs/2026-10-14-qwen-voice-mcp-design.md)。

---

## 快速开始

### 1. 安装

```powershell
pip install -e .
```

依赖：`mcp`、`dashscope`、`httpx`、`cryptography`、`keyring`、`soundfile`、`pydantic`、
`filelock`、`python-dotenv`、`typer`、`rich`。

### 2. 配置 API 密钥

三种方式任选其一：

```powershell
# A. GUI 配置中心（推荐，密钥加密存储）
qwen-voice-mcp gui

# B. CLI 写入加密 vault
qwen-voice-mcp config set-key sk-xxxxxxxxxxxx

# C. 环境变量（优先级最高，不落盘）
$env:DASHSCOPE_API_KEY = "sk-xxxxxxxxxxxx"
```

### 3. 启动

```powershell
# stdio 模式（本地集成，推荐）
python server.py --transport stdio

# streamable-http 模式（远程集成）
python server.py --transport streamable-http --port 8765
```

---

## 在 DeepseekHarness 中接入

### stdio 模式

写入 Harness Profile 的 `cordis.patch.yml`：

```yaml
mcp_servers:
  qwen-voice:
    command: python
    args: ["F:/MCP_Server/Qwen-TTS-MCP-DSH/server.py", "--transport", "stdio"]
    env:
      DASHSCOPE_API_KEY: "${QWEN_API_KEY}"
```

### streamable-http 模式

```yaml
mcp_servers:
  qwen-voice:
    url: "http://127.0.0.1:8765/mcp"
    transport: "streamable-http"
    headers:
      Authorization: "Bearer ${MCP_AUTH_TOKEN}"
```

也可以直接在 DSH 的「设置 → MCP」中可视化管理。详细步骤见
[集成指南](docs/integration-guide.md)。

### 验证

在会话中输入「列出可用的语音模型」应触发 `list_voice_models`；输入「请把"你好世界"转成语音」
应触发 `synthesize_speech`。

---

## 架构

```
server.py ──► tools/*  ──► core/services.VoiceServices ──┬─► core/qwen_client ──► dashscope
(GUI ──► core/secret_manager, core/config_store)          ├─► core/balance_checker
                                                          ├─► core/audio_utils
                                                          └─► core/model_registry
```

- `core/services.py` 是唯一装配点，也是**余额闸门**的唯一实现。
- `tools/**` 只做参数适配，**不 import dashscope**，返回统一的 `ToolResult`。
- `config.py` 与 `core/models.py` 是叶子模块。

| 模块 | 职责 |
|------|------|
| `config.py` | 模型清单、API 端点、阈值等常量 |
| `core/models.py` | `Status` / `ToolResult` / `VoiceModel` / `AudioProbe` |
| `core/errors.py` | SDK 异常 → 统一错误分类，密钥脱敏 |
| `core/config_store.py` | 分层配置（CLI > 环境变量 > 文件 > 默认） |
| `core/secret_manager.py` | AES-256-GCM 加密密钥库 |
| `core/model_registry.py` | 模型清单与成本估算 |
| `core/audio_utils.py` | 音频探测、校验、临时文件（后端可插拔） |
| `core/balance_checker.py` | 余额多端点探测 + 阈值三档 |
| `core/qwen_client.py` | DashScope SDK 封装（TTS / 克隆 / 上传） |
| `core/services.py` | 装配 + 计费闸门 |
| `tools/` | 四个 MCP Tool 的薄适配 |
| `gui/config_window.py` | Tkinter 配置中心 |

---

## 余额策略

每次 `synthesize_speech` / `clone_voice` 之前**必须**先检查余额。

| 条件 | 行为 |
|------|------|
| 余额 > 阈值 + 预估消耗 | 正常执行 |
| 阈值 < 余额 ≤ 阈值 + 预估消耗 | 执行，附 `warnings` |
| 余额 ≤ 阈值 | 拒绝，返回 `insufficient_balance` |
| 余额未知 + `strict_balance=true` | 拒绝，返回 `balance_unavailable` |
| 余额未知 + `strict_balance=false`（默认） | 执行，附 `warnings` |

阈值默认 ¥1.00，可通过 `QWEN_VOICE_BALANCE_THRESHOLD` 或 GUI 调整。

> DashScope 未公开文档化余额查询接口，本实现按序探测多个候选端点；**全部失败时报告余额未知，
> 而不是假装余额为 0**——否则会误拦每一次调用。

---

## 配置项

优先级：**CLI 参数 > 环境变量（`QWEN_VOICE_*`） > `%APPDATA%\qwen-voice-mcp\config.json` > 默认值**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DASHSCOPE_API_KEY` | — | API 密钥 |
| `QWEN_VOICE_HOME` | `%APPDATA%\qwen-voice-mcp` | 配置与密钥库目录 |
| `QWEN_VOICE_TRANSPORT` | `stdio` | `stdio` / `streamable-http` / `sse` |
| `QWEN_VOICE_HOST` | `127.0.0.1` | HTTP 监听地址（非环回需显式放行） |
| `QWEN_VOICE_PORT` | `8765` | HTTP 端口 |
| `QWEN_VOICE_DEFAULT_VOICE` | `longanyang` | 默认音色 |
| `QWEN_VOICE_BALANCE_THRESHOLD` | `1.00` | 余额告警阈值 |
| `QWEN_VOICE_STRICT_BALANCE` | `false` | 余额未知时是否拒绝执行 |
| `QWEN_VOICE_HTTP_AUTH_TOKEN` | — | HTTP 模式 Bearer Token |

---

## 安全

| 维度 | 措施 |
|------|------|
| 密钥存储 | AES-256-GCM；主密钥由 Windows 凭据管理器保管，回退 PBKDF2-HMAC-SHA256（60 万次迭代） |
| 密钥传输 | 环境变量或加密通道；明文密钥不落盘 |
| 日志 | stdio 模式只写 stderr；密钥经 `redact()` 脱敏，永不进日志或错误消息 |
| HTTP 模式 | Bearer Token 校验；默认只绑定 `127.0.0.1` |
| 文件上传 | 后缀 + 魔数 + 大小 + 时长四重校验，隔离临时目录 |
| 错误消息 | 不含内部路径、密钥或堆栈 |

---

## 与说明书的偏差

实现以**实测 SDK 行为**为准；以下是刻意偏离 `Qwen-MCP.md` 的地方。

| # | 说明书写法 | 实测事实 | 本实现 |
|---|-----------|---------|--------|
| D1 | `FastMCP(name=, version=, instructions=)` | `mcp` 的 `FastMCP.__init__` 无 `version` 参数 | 只传 `name` / `instructions` / `host` / `port` |
| D2 | 余额查 `api/v1/account/quota` | 该端点非公开文档，无法离线证实 | 多端点探测 + 优雅降级（见上） |
| D3 | `clone_voice(audio_path, voice_name)` 直接命名 | `create_voice(target_model, prefix, url)` 需要 **URL**；`prefix` 仅小写字母数字且 **<10 字符** | 本地文件先上传换 URL；`voice_name` 生成本地别名 + 合规 `prefix` |
| D4 | `voice_name: "用户自定义音色"` | 中文不可作 prefix | `voice_name` 存为本地别名并映射到 `voice_id` |
| D5 | 硬编码 `cosyvoice-v3-plus`、`qwen-audio-3.0-tts-flash` | 无法离线证实这些模型 ID 存在 | 清单配置驱动 + `verify-models` 探测，返回带 `verified` 标记；未证实即标 `null` |
| D6 | 密钥库用 `apikeyvault` / `keyvault-local` | 不在可验证的 PyPI 范围 | 自实现 AES-256-GCM vault + keyring，不引入来源不明的依赖 |
| D7 | 目录仅 `docs/integration-guide.md` | — | 增加设计规格与实现计划 |
| D8 | 9.3 用 `python server.py`，第十章用 `qwen_voice_mcp.gui` | 两者不一致 | 可安装包 `qwen_voice_mcp/` + 根 `server.py` 转发入口，两种写法都可用 |
| D9 | 音频依赖 `pydub` / `soundfile` | 本环境无 ffmpeg；`soundfile` 的 libsndfile 原生支持 MP3/FLAC/OGG | `soundfile` 为首选后端，`pydub` 仅在 ffmpeg 存在时启用，另保留纯标准库回退 |

---

## 开发

```powershell
# 运行测试（219 个用例，全程离线，不消耗任何额度）
python -m pytest -q

# 自检：确认四个工具可被发现
python server.py --selftest
python -m qwen_voice_mcp --selftest

# 探测模型真实可用性（需要有效密钥）
qwen-voice-mcp verify-models
```

| 测试文件 | 用例数 | 覆盖 |
|----------|-------:|------|
| `test_models.py` | 9 | 状态枚举、结果模型 |
| `test_config_store.py` | 10 | 四级配置优先级、范围校验 |
| `test_secret_manager.py` | 13 | 加密往返、篡改检测、并发写 |
| `test_model_registry.py` | 12 | 清单、成本估算、verified 标记 |
| `test_audio_utils.py` | 18 | 时长/格式/大小校验、base64、后端降级 |
| `test_balance_checker.py` | 21 | 端点回退、阈值三档、未知余额 |
| `test_qwen_client.py` | 25 | 异常分类、AudioFormat 映射、prefix 合法性 |
| `test_services.py` | 31 | 计费闸门顺序、错误兜底 |
| `test_mcp_tools.py` | 16 | in-memory MCP 工具发现与调用 |
| `test_cli.py` | 13 | CLI 命令、退出码、脱敏 |
| `test_voice_store.py` | 14 | 音色别名持久化 |
| `test_root_entrypoint.py` | 12 | `server.py` 参数路由 |
| `test_regressions.py` | 25 | SDK 绑定健康、错误消息可读性、GUI 保存前校验 |

### 两个必须保留的环境处理

1. **`tests/conftest.py` 在 Windows 上去掉 `os.mkdir` 的 `mode` 参数。**
   pytest 用 `os.mkdir(path, 0o700)` 建临时目录，而 Windows 上传入 `mode` 会让 CPython
   设置**显式保护性 DACL**（而不是继承父目录权限），导致沙箱令牌无法访问它刚创建的目录，
   于是所有 `tmp_path` 测试在 fixture 阶段报 `PermissionError: [WinError 5]`。
   这不是产品代码问题，请勿删除该处理。

2. **`core/qwen_client.py` 的 SDK 绑定必须分开 import。**
   `SpeechSynthesizer`、`VoiceEnrollmentService`、`AudioFormat`、`Files` 各自独立 import。
   若共用一个 `try` 块，任何一条 import 失败都会让其余绑定保持 `None`，从而**整个 TTS
   与克隆功能静默失效**（历史上确实发生过：`dashscope.audio.voice_enrollment` 不存在）。
   `tests/test_regressions.py` 会断言 `SDK_AVAILABLE is True` 并校验所有 import 目标真实存在。


## 许可

MIT
