# DeepseekHarness 语音 MCP 工程说明书

## 一、项目概述

### 1.1 项目目标

构建一个基于 Model Context Protocol 的 Python MCP Server，作为 DeepseekHarness 与 Qwen 语音模型之间的桥梁。该 MCP 提供以下核心能力：

- 通过 GUI 界面配置和加密存储 Qwen API 密钥
- 向 DeepseekHarness 动态返回可用的 Qwen 语音模型列表及功能描述
- 接收 DeepseekHarness 提交的文字或语音文件，分别执行文字转语音或声音克隆
- 每次执行前查询 API 账户余额，余额不足时返回结构化提示

### 1.2 技术选型

| 组件 | 技术方案 | 说明 |
|------|----------|------|
| MCP 协议 | Python MCP SDK 1.2.0+，FastMCP | 使用 FastMCP 类自动生成工具定义 |
| GUI 框架 | Tkinter 或 NiceGUI | 轻量级桌面配置界面 |
| API 加密存储 | apikeyvault / keyvault-local | AES-256-GCM 加密存储密钥 |
| Qwen 语音 API | DashScope SDK | 支持 CosyVoice 语音克隆与 TTS |
| 音频处理 | pydub / soundfile | 音频格式校验与转换 |
| 传输方式 | stdio（本地）或 streamable-http（远程） | stdio 适合本地集成 |

### 1.3 前置条件

- Python 3.10 或更高版本
- 有效的 Qwen（DashScope）API 密钥
- DeepseekHarness 已安装并可正常打开
- 已创建用于 MCP 接入的 Harness 工作区


## 二、系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                    DeepseekHarness                       │
│  ┌──────────────┐    ┌────────────────────────────────┐  │
│  │  MCP Client  │◄──►│  MCP Server (本项目)            │  │
│  │  (stdio/HTTP)│    │  ┌──────────┐ ┌─────────────┐  │  │
│  └──────────────┘    │  │ Tool:    │ │ Tool:       │  │  │
                      │  │ list_    │ │ synthesize_ │  │  │
                      │  │ models   │ │ speech      │  │  │
                      │  └──────────┘ └─────────────┘  │  │
                      │  ┌──────────┐ ┌─────────────┐  │  │
                      │  │ Tool:    │ │ Tool:       │  │  │
                      │  │ clone_   │ │ check_      │  │  │
                      │  │ voice    │ │ balance     │  │  │
                      │  └──────────┘ └─────────────┘  │  │
                      └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────┐
│                   Qwen (DashScope) API                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │ CosyVoice   │  │ Qwen-Audio  │  │ 账户余额接口     │  │
│  │ 语音克隆    │  │ TTS 合成    │  │ (quota/billing) │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 2.2 模块划分

| 模块 | 文件 | 职责 |
|------|------|------|
| GUI 配置模块 | `gui/config_window.py` | API 密钥录入、验证、加密存储 |
| 密钥管理模块 | `core/secret_manager.py` | 加密读写密钥，自动加载到环境变量 |
| MCP Server 入口 | `server.py` | FastMCP 实例创建、工具注册、stdio/HTTP 启动 |
| 工具实现层 | `tools/` 目录 | 四个 MCP Tool 的具体实现 |
| Qwen API 客户端 | `core/qwen_client.py` | 封装 DashScope SDK 调用，统一错误处理 |
| 余额检查模块 | `core/balance_checker.py` | 调用 DashScope 账户接口，返回余额状态 |
| 模型注册模块 | `core/model_registry.py` | 维护支持的模型清单及功能描述 |
| 音频处理模块 | `core/audio_utils.py` | 音频格式校验、编码转换、临时文件管理 |
| 配置常量 | `config.py` | 模型清单、API 端点、超时阈值等 |


## 三、GUI 配置模块设计

### 3.1 界面布局

```
┌──────────────────────────────────────────────┐
│  Qwen Voice MCP — 配置中心                    │
├──────────────────────────────────────────────┤
│  API 配置                                     │
│  ┌──────────────────────────────────────────┐│
│  │ API Key:  [••••••••••••••••••••]  [验证] ││
│  │ 状态:     ● 已验证 — 账户余额 ¥12.50     ││
│  └──────────────────────────────────────────┘│
│                                               │
│  服务配置                                     │
│  ┌──────────────────────────────────────────┐│
│  │ 传输方式:  ○ stdio   ○ streamable-http   ││
│  │ 监听端口:  [8765          ]  (HTTP 模式)  ││
│  │ 默认音色:  [longanyang  ▼]                ││
│  │ 余额阈值:  [¥1.00        ]  (低于则告警)  ││
│  └──────────────────────────────────────────┘│
│                                               │
│  [保存配置]  [测试连接]  [清除密钥]           │
└──────────────────────────────────────────────┘
```

### 3.2 密钥存储方案

推荐采用 **apikeyvault** 作为密钥管理方案。该库提供加密 vault（密钥以密文存储在 `~/.config/apivault/keys.enc`），由操作系统钥匙串解锁（macOS Keychain / Windows Credential Manager / Linux Secret Service），并附带桌面 GUI 和 CLI。

若需要完全自包含的方案，可采用 **keyvault-local**，使用 AES-256-GCM 加密，密钥仅以密文存于本地单文件 `secrets.db` 中，支持桌面 GUI。

存储流程：
1. 用户在 GUI 中输入 API Key
2. 调用 `validate_key()` 函数向 DashScope 发送轻量级请求验证密钥有效性
3. 验证通过后，通过加密库写入本地加密存储
4. 服务启动时自动解密并加载到环境变量 `DASHSCOPE_API_KEY`


## 四、MCP Server 核心设计

### 4.1 FastMCP 实例初始化

使用 Python MCP SDK 的 `FastMCP` 类，该类利用 Python 类型提示和 docstring 自动生成工具定义。服务实例需声明名称和版本号：

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="qwen-voice-mcp",
    version="1.0.0",
    instructions="Qwen 语音模型 MCP 服务，提供模型列表查询、文字转语音、声音克隆和账户余额检查能力。"
)
```

### 4.2 工具定义

#### Tool 1: `list_voice_models`

**功能**：返回当前 API 可用的 Qwen 语音模型名称及功能描述。

**参数**：无

**返回结构**：

```json
{
  "models": [
    {
      "name": "cosyvoice-v3-plus",
      "type": "voice_cloning_and_tts",
      "description": "最新大规模语音克隆模型，5-20秒参考音频即可生成高度相似的自然音色。支持流式文本转语音合成，适用于专业场景。",
      "capabilities": ["voice_clone", "text_to_speech", "streaming_tts"],
      "pricing": "¥0.26 / 万字符"
    },
    {
      "name": "qwen-audio-3.0-tts-flash",
      "type": "text_to_speech",
      "description": "Qwen-Audio 系列快速语音合成模型，低延迟，适合实时场景。",
      "capabilities": ["text_to_speech"],
      "pricing": "按量计费"
    }
  ],
  "default_model": "cosyvoice-v3-plus"
}
```

模型清单在 `core/model_registry.py` 中维护，支持运行时动态更新。CosyVoice-v3-plus 的核心能力包括语音克隆（5-20 秒参考音频）和实时流式 TTS 合成。

#### Tool 2: `synthesize_speech`

**功能**：接收文本，调用文字转语音模型生成音频，返回音频文件。

**参数**：

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| text | string | 是 | 要合成的文本内容 |
| model | string | 否 | 模型名称，默认使用配置中的 default_model |
| voice | string | 否 | 音色名称，默认使用配置中的 default_voice |
| output_format | string | 否 | 输出格式，默认 mp3 |

**执行流程**：
1. 调用 `check_balance()` 检查账户余额
2. 若余额不足，直接返回结构化错误提示
3. 使用 DashScope SDK 的 `SpeechSynthesizer` 进行合成
4. 将生成的音频写入临时文件
5. 返回音频文件路径或 base64 编码

**返回结构（成功）**：

```json
{
  "status": "success",
  "audio_path": "/tmp/qwen_tts_20260101_120000.mp3",
  "audio_format": "mp3",
  "duration_seconds": 3.5,
  "text_length": 42,
  "model_used": "cosyvoice-v3-plus",
  "voice_used": "longanyang"
}
```

**返回结构（余额不足）**：

```json
{
  "status": "insufficient_balance",
  "message": "账户余额不足，当前余额 ¥0.50，预估本次合成消耗约 ¥0.11，余额阈值 ¥1.00。请提醒用户为 Qwen 账户充值后重试。",
  "current_balance": 0.50,
  "threshold": 1.00,
  "estimated_cost": 0.11
}
```

#### Tool 3: `clone_voice`

**功能**：接收参考音频文件，执行声音克隆，返回克隆后的 voice_id。

**参数**：

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| audio_path | string | 是 | 参考音频文件路径（本地路径或 base64） |
| voice_name | string | 是 | 自定义音色名称 |
| model | string | 否 | 克隆模型，默认 cosyvoice-v3-plus |

**执行流程**：
1. 调用 `check_balance()` 检查余额
2. 校验音频文件：格式（wav/mp3/m4a）、时长（5-60 秒）、采样率
3. 通过 DashScope Voice Enrollment API 上传参考音频
4. 轮询或等待克隆任务完成，获取 voice_id
5. 返回 voice_id 和克隆状态

**返回结构**：

```json
{
  "status": "success",
  "voice_id": "qwen-voice-clone-20260101-abc123",
  "voice_name": "用户自定义音色",
  "model_used": "cosyvoice-v3-plus",
  "cloning_status": "completed"
}
```

Voice Cloning 创建的自定义音色可通过在 TTS 调用中传入对应的 `voice_id` 来使用。

#### Tool 4: `check_balance`（内部辅助工具）

**功能**：查询当前 API 账户余额。此工具也注册为 MCP Tool，供 DeepseekHarness 显式调用。

**参数**：无

**返回结构**：

```json
{
  "status": "ok",
  "balance": 12.50,
  "currency": "CNY",
  "threshold": 1.00,
  "is_sufficient": true,
  "message": "账户余额 ¥12.50，状态正常。"
}
```

DashScope 的余额查询可调用 `https://dashscope.aliyuncs.com/api/v1/account/quota` 接口。该工具同时支持作为独立 MCP Server 使用的 `check-balance` 方案的理念。


## 五、Qwen API 集成层设计

### 5.1 客户端封装

`core/qwen_client.py` 封装所有对 DashScope 的调用，提供统一的重试、超时和错误处理：

- **初始化**：从环境变量 `DASHSCOPE_API_KEY` 读取密钥，若不存在则从加密存储中加载
- **TTS 合成**：封装 `SpeechSynthesizer(model, voice).call(text)` 的调用，支持流式与非流式
- **语音克隆**：封装 Voice Enrollment 接口的上传、任务创建、状态轮询
- **余额查询**：调用 quota 接口，解析返回的余额字段
- **错误处理**：区分网络错误、认证错误、配额错误、参数错误，分别返回不同的错误码和用户友好提示

### 5.2 音频处理

`core/audio_utils.py` 负责：

- **格式校验**：检查输入音频是否为 wav / mp3 / m4a / flac 等 DashScope 支持的格式
- **时长检测**：使用 `pydub` 或 `soundfile` 读取音频时长，确保在 5-60 秒范围内
- **编码转换**：必要时将音频转换为 16kHz 单声道 wav（DashScope Voice Enrollment 推荐格式）
- **临时文件管理**：为 TTS 输出生成带时间戳的唯一文件名，定期清理过期临时文件


## 六、DeepseekHarness 集成配置

### 6.1 配置方式

DeepseekHarness 通过 MCP Client 连接本服务。配置写入 Harness Profile 的 `cordis.patch.yml` 文件中。用户可在 Harness 的“设置 → MCP”中可视化管理服务。

**stdio 模式配置示例**：

```yaml
mcp_servers:
  qwen-voice:
    command: python
    args: ["/path/to/qwen-voice-mcp/server.py", "--transport", "stdio"]
    env:
      DASHSCOPE_API_KEY: "${QWEN_API_KEY}"
```

**streamable-http 模式配置示例**：

```yaml
mcp_servers:
  qwen-voice:
    url: "http://127.0.0.1:8765/mcp"
    transport: "streamable-http"
    headers:
      Authorization: "Bearer ${MCP_AUTH_TOKEN}"
```

### 6.2 工具映射

DeepseekHarness 会自动将 MCP Server 暴露的工具注册为模型可调用的原生工具。工具清单映射为 DSH 可见能力，在执行前由 DSH 提供权限确认。

映射关系：

| MCP Tool | DSH 显示名称 | 触发场景 |
|----------|-------------|----------|
| `list_voice_models` | 查询可用语音模型 | 用户询问“有哪些语音模型可用” |
| `synthesize_speech` | 文字转语音 | 用户提交文字并要求语音输出 |
| `clone_voice` | 声音克隆 | 用户上传语音文件要求复制音色 |
| `check_balance` | 查询账户余额 | 用户询问余额或执行前自动检查 |


## 七、余额检查与错误处理

### 7.1 检查时机

每次执行 `synthesize_speech` 或 `clone_voice` 之前，**必须**先调用 `check_balance()`。这是硬性约束，不可跳过。

### 7.2 阈值策略

在配置中定义 `balance_threshold`（默认 ¥1.00）。逻辑如下：

- 余额 > 阈值 + 预估消耗：正常执行
- 余额 > 阈值 但 ≤ 阈值 + 预估消耗：执行但附带警告信息
- 余额 ≤ 阈值：拒绝执行，返回 `insufficient_balance` 错误

### 7.3 DeepseekHarness 侧的用户提示

MCP Tool 返回的结构化错误会被 DSH 解析并呈现给用户。错误消息应包含：

- 当前余额和阈值
- 预估消耗（若可计算）
- 明确的行动建议（“请为 Qwen 账户充值后重试”）
- 充值入口链接（可选）


## 八、安全设计

| 安全维度 | 措施 |
|----------|------|
| API 密钥存储 | AES-256-GCM 加密，密钥由 OS 钥匙串保护 |
| 密钥传输 | 环境变量或加密通道，永不写入日志或明文文件 |
| stdio 日志 | 禁止写入 stdout，使用 `sys.stderr` 或 logging 库 |
| HTTP 模式 | 支持 Bearer Token 认证，限制监听地址为 127.0.0.1 |
| 文件上传 | 校验文件类型和大小，隔离临时目录，执行后清理 |
| 错误消息 | 不泄露内部路径、密钥或堆栈信息 |


## 九、部署与运行

### 9.1 环境准备

```bash
# 创建项目
uv init qwen-voice-mcp
cd qwen-voice-mcp

# 创建虚拟环境
uv venv
source .venv/bin/activate

# 安装依赖
uv add "mcp[cli]" dashscope httpx pydub apikeyvault
```

### 9.2 首次配置

```bash
# 启动 GUI 配置界面
python -m qwen_voice_mcp.gui

# 或使用 CLI 快速配置
apivault set DASHSCOPE_API_KEY sk-xxxxxxxxxxxx
```

### 9.3 启动 MCP Server

```bash
# stdio 模式（本地集成）
python server.py --transport stdio

# HTTP 模式（远程集成）
python server.py --transport http --port 8765
```

### 9.4 在 DeepseekHarness 中验证

1. 打开 Harness 设置 → MCP → 添加服务器
2. 填入上述配置
3. 在会话中输入“列出可用的语音模型”，验证 `list_voice_models` 正常返回
4. 输入“请把‘你好世界’转成语音”，验证 `synthesize_speech` 正常执行


## 十、项目文件结构

```
qwen-voice-mcp/
├── server.py                     # MCP Server 入口，FastMCP 实例与工具注册
├── config.py                     # 常量定义：模型清单、API 端点、阈值
├── pyproject.toml                # 项目依赖声明
├── README.md
│
├── gui/
│   ├── __init__.py
│   └── config_window.py          # GUI 配置窗口
│
├── core/
│   ├── __init__.py
│   ├── secret_manager.py         # 加密密钥读写
│   ├── qwen_client.py            # DashScope API 封装
│   ├── balance_checker.py        # 余额查询与阈值判断
│   ├── model_registry.py         # 模型清单维护
│   └── audio_utils.py            # 音频格式校验与转换
│
├── tools/
│   ├── __init__.py
│   ├── list_models.py            # list_voice_models 实现
│   ├── tts.py                    # synthesize_speech 实现
│   ├── voice_clone.py            # clone_voice 实现
│   └── balance.py                # check_balance 实现
│
├── tests/
│   ├── test_balance_checker.py
│   ├── test_qwen_client.py
│   └── test_audio_utils.py
│
└── docs/
    └── integration-guide.md      # DeepseekHarness 集成指南
```


## 十一、开发路线图

| 阶段 | 内容 | 产出 |
|------|------|------|
| Phase 1 | MCP Server 骨架 + `list_voice_models` + `check_balance` | 可被 DSH 发现并调用两个只读工具 |
| Phase 2 | `synthesize_speech` 实现 + GUI 密钥配置 | 文字转语音全链路可用 |
| Phase 3 | `clone_voice` 实现 + 音频校验 | 声音克隆全链路可用 |
| Phase 4 | 余额阈值策略 + 错误提示优化 | 余额不足时 DSH 能正确提醒用户 |
| Phase 5 | 测试、文档、打包 | 可发布版本 |