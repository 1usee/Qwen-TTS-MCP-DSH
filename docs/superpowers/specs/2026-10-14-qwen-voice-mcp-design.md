# Qwen Voice MCP — 设计规格 (Design Spec)

- 日期：2026-10-14
- 状态：已批准（设计阶段通过）
- 依据：`Qwen-MCP.md`（DeepseekHarness 语音 MCP 工程说明书）
- 分类：architectural（全新项目，无既有代码）

---

## 1. 目标与成功标准

### 1.1 目标

构建一个 Python MCP Server 包 `qwen_voice_mcp`，作为 DeepseekHarness（DSH）与 Qwen
语音模型之间的桥梁，提供四项能力：

1. 查询 API 可用的 Qwen 语音模型清单
2. 文字转语音（TTS）
3. 声音克隆（Voice Cloning）
4. 账户余额查询

并提供 Tkinter 桌面配置中心，用于 API 密钥录入、验证与加密存储。

### 1.2 成功标准（可验证）

| 编号 | 标准 | 验证方式 |
|------|------|----------|
| S1 | DSH 能发现全部 4 个工具，schema 正确 | MCP in-memory client 断言工具名与必填参数 |
| S2 | `synthesize_speech` 全链路可用 | mock DashScope 下返回成功结构；真实密钥下手动验收 |
| S3 | `clone_voice` 全链路可用 | 同上，含音频校验分支 |
| S4 | 每次计费调用前强制执行余额检查 | 单测断言调用顺序，余额不足时 DashScope 不被调用 |
| S5 | 密钥永不落明文、永不进日志/错误消息 | 单测断言 `redact()` 与错误消息不含密钥 |
| S6 | stdio 模式 stdout 只有协议数据 | 单测断言日志全部走 stderr |
| S7 | 无网络、无密钥时测试全绿 | `pytest -q` 离线通过 |

### 1.3 非目标（YAGNI）

- 不做流式 TTS 回调推送（`streaming_call`）
- 不做 async 并发架构
- 不做插件系统、模型微调
- 不做 Web GUI（NiceGUI 未安装，且违背"轻量级桌面界面"）

---

## 2. 环境事实（已实测，非假设）

- Python `3.11.16` @ `D:\Conda_envs\mcp-dev\python.exe`
- `mcp 1.30.0`，`fastmcp 4.0.3`
- `dashscope 1.27.5`：`audio.tts_v2.{SpeechSynthesizer, VoiceEnrollmentService, AudioFormat}`、`audio.qwen_tts`、`Files.upload`
- `cryptography 50.0.1`、`keyring 25.7.0`、`soundfile 0.14.0`（libsndfile **1.2.2**，原生支持 MP3/FLAC/OGG/AIFF/CAF/W64/WAV）、`numpy 2.4.6`、`filelock 4.1.1`、`python-dotenv`、`httpx 0.28.1`、`requests 2.34.2`、`pydantic 2.13.5`、`pydantic-settings`、`PyYAML`、`pytest 9.1.1`、`typer`、`rich`、`tkinter`
- `pydub` 已安装但**无 ffmpeg** → 仅作软依赖，不在关键路径

---

## 3. 与说明书 `Qwen-MCP.md` 的偏差记录

以下按**实测 SDK 行为**实现，偏差记入 README。

| # | 说明书写法 | 实测事实 | 本设计处理 |
|---|-----------|---------|-----------|
| D1 | `FastMCP(name=, version=, instructions=)` | mcp 1.30 的 `FastMCP.__init__` 无 `version` 参数 | 只传 `name` / `instructions`；版本号放 `__version__` 常量 |
| D2 | 余额查 `api/v1/account/quota` | 非公开文档端点，无法离线证实 | 多端点探测 + 优雅降级（§7） |
| D3 | `clone_voice(audio_path, voice_name)` 直接命名 | `create_voice(target_model, prefix, url)` 需 **URL**；`prefix` 仅小写字母数字且 **<10 字符** | 本地文件先 `Files.upload`，`voice_name` 生成本地别名 + 合规 `prefix` |
| D4 | `voice_name: "用户自定义音色"` | 中文不可作 prefix | `voice_name` 存为本地别名映射到 `voice_id` |
| D5 | 模型 ID 硬编码 `cosyvoice-v3-plus` 等 | 无法离线证实存在 | registry 配置驱动 + `--verify-models` 探测，输出带 `verified` 标记 |
| D6 | 密钥库 `apikeyvault` / `keyvault-local` | 不在环境、不在可验证 PyPI 范围 | 自实现 AES-256-GCM vault + keyring（§6） |
| D7 | `docs/` 仅 integration-guide | — | 增加本 spec 与 `writing-plans` 产出的计划 |
| D8 | 目录无包结构 | 文档 9.3 用 `python server.py`，十章用 `qwen_voice_mcp.gui` | 采用可安装包 `qwen_voice_mcp/` + 根 `server.py` 转发入口（两者都成立） |

---

## 4. 架构与依赖方向

```
server.py ──► tools/*  ──► core/services.VoiceServices ──┬─► core/qwen_client ──► dashscope
(GUI ──► core/secret_manager, core/config_store)          ├─► core/balance_checker
                                                          ├─► core/audio_utils
                                                          └─► core/model_registry
```

**依赖规则（硬约束）**

- `core/models.py`、`config.py` 是叶子模块，不 import 任何内部模块
- `tools/` **禁止** import `dashscope`——只能经 `core/services.py`
- `gui/` 只依赖 `core/secret_manager`、`core/config_store`、`core/model_registry`、`core/balance_checker`
- 计费闸门 `services.ensure_balance()` 只有一处实现，四个工具复用

**分层理由**：说明书 §2.2 的模块职责全部保留；额外引入 `services.py` 作为唯一装配点，
使「余额检查 → 执行 → 错误映射」写一次而非四次，且工具层成为可独立单测的薄适配器。

---

## 5. 文件结构

```
Qwen-TTS-MCP-DSH/
├── server.py                      # 根转发入口（兼容 `python server.py`）
├── config.py                      # 叶子常量 + 默认模型清单 + 端点
├── pyproject.toml
├── README.md
├── .env.example
├── qwen_voice_mcp/
│   ├── __init__.py                # __version__
│   ├── __main__.py                # python -m qwen_voice_mcp
│   ├── server.py                  # FastMCP 实例 + 工具注册 + 启动
│   ├── cli.py                     # typer CLI：run / selftest / verify-models / config
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py              # ToolResult / Status / VoiceModel / 枚举
│   │   ├── errors.py              # SDK 异常 → 统一错误分类
│   │   ├── config_store.py        # 分层配置（CLI>env>file>default）
│   │   ├── secret_manager.py      # AES-256-GCM vault + keyring
│   │   ├── model_registry.py      # 模型清单维护与探测
│   │   ├── audio_utils.py         # 可插拔音频后端：soundfile→pydub→stdlib
│   │   ├── balance_checker.py     # 多端点余额探测 + 阈值三档
│   │   ├── qwen_client.py         # DashScope 封装：TTS / 克隆 / 上传
│   │   └── services.py            # VoiceServices 装配 + 计费闸门
│   ├── tools/
│   │   ├── __init__.py            # register_all(mcp)
│   │   ├── list_models.py
│   │   ├── tts.py
│   │   ├── voice_clone.py
│   │   └── balance.py
│   └── gui/
│       ├── __init__.py
│       └── config_window.py       # Tkinter 配置中心
├── tests/
│   ├── conftest.py
│   ├── test_audio_utils.py
│   ├── test_secret_manager.py
│   ├── test_config_store.py
│   ├── test_model_registry.py
│   ├── test_balance_checker.py
│   ├── test_qwen_client.py
│   ├── test_services.py
│   └── test_mcp_tools.py          # in-memory MCP 端到端
└── docs/
    ├── integration-guide.md
    └── superpowers/
        ├── specs/2026-10-14-qwen-voice-mcp-design.md
        └── plans/2026-10-14-qwen-voice-mcp-plan.md
```

---

## 6. 数据模型与错误分类（`core/models.py`）

### 6.1 `Status` 枚举（唯一 `status` 字段来源）

| status | 含义 | 触发条件 |
|--------|------|----------|
| `success` | 成功 | — |
| `insufficient_balance` | 余额不足 | 余额 ≤ 阈值 |
| `balance_unavailable` | 余额不可得 | 全部端点失败且严格模式 |
| `auth_error` | 密钥无效/无权限 | HTTP 401/403 或 SDK 认证码 |
| `invalid_input` | 参数/音频不合格 | 校验失败、空文本 |
| `model_not_supported` | 模型不在 registry | 未知模型名 |
| `rate_limited` | 限流 | 429 |
| `network_error` | 网络/超时 | 超时、连接错误 |
| `upstream_error` | 服务端错误 | 5xx / 未知 code |
| `internal_error` | 本程序缺陷 | 兜底，不含堆栈 |

### 6.2 `ToolResult`

```python
@dataclass(frozen=True)
class ToolResult:
    status: Status
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]: ...
    @property
    def ok(self) -> bool: ...
```

契约：`to_dict()` 始终产出 `{"status": ..., "message": ..., **data}`，
`warnings` 非空时附 `warnings` 键。工具返回该字典，MCP 层再 `json.dumps`。

### 6.3 具体返回结构

`list_voice_models`：

```json
{
  "status": "success",
  "message": "共 4 个语音模型",
  "models": [
    {"name": "cosyvoice-v2", "type": "voice_cloning_and_tts",
     "description": "...", "capabilities": ["voice_clone","text_to_speech"],
     "pricing": "...", "verified": null, "source": "builtin"}
  ],
  "default_model": "cosyvoice-v2",
  "count": 4
}
```

`synthesize_speech` 成功：

```json
{
  "status": "success",
  "message": "语音合成完成",
  "audio_path": "C:\\...\\qwen_tts_20261014_120000_ab12.mp3",
  "audio_format": "mp3",
  "duration_seconds": 3.52,
  "text_length": 4,
  "model_used": "cosyvoice-v2",
  "voice_used": "longanyang",
  "estimated_cost": 0.0001
}
```

余额不足：

```json
{
  "status": "insufficient_balance",
  "message": "账户余额不足，当前余额 ¥0.50，预估本次合成消耗约 ¥0.11，余额阈值 ¥1.00。请为 Qwen 账户充值后重试。",
  "current_balance": 0.50,
  "threshold": 1.00,
  "estimated_cost": 0.11,
  "recharge_url": "https://bailian.console.aliyun.com/"
}
```

`clone_voice` 成功：

```json
{
  "status": "success",
  "message": "声音克隆完成",
  "voice_id": "qwenvoiceclone01",
  "voice_name": "用户自定义音色",
  "voice_prefix": "qwenvoice",
  "model_used": "cosyvoice-v2",
  "cloning_status": "completed",
  "request_id": "..."
}
```

`check_balance`：

```json
{
  "status": "success",
  "message": "账户余额 ¥12.50，状态正常。",
  "balance": 12.50,
  "currency": "CNY",
  "threshold": 1.00,
  "is_sufficient": true,
  "source": "https://dashscope.aliyuncs.com/api/v1/account/quota"
}
```

余额端点全部失败时：

```json
{
  "status": "success",
  "message": "余额查询接口不可用，已按非严格模式放行。",
  "balance": null, "currency": null, "threshold": 1.00,
  "is_sufficient": null, "source": null,
  "warnings": ["余额状态未知：所有探测端点均失败"]
}
```

（`check_balance` 自身是只读工具，端点不可用不算工具失败；由 `strict_balance` 决定
计费工具是否放行——见 §7。）

---

## 7. 余额检查（`core/balance_checker.py`）

### 7.1 端点探测

按序尝试，任一成功即返回；全部失败返回 `BalanceStatus.unknown`：

1. `GET {base}/api/v1/account/quota`（说明书指定）
2. `GET {base}/api/v1/billing/query`（兼容尝试）
3. `GET {base}/api/v1/account/balance`（兼容尝试）

识别字段名：`balance`、`total_balance`、`available_balance`、`remaining`、
`amount`、`data.balance`；货币取 `currency`，默认 `CNY`。
每个端点超时 `balance_timeout_seconds`（默认 8s），失败不抛异常、记录 `last_error`。

### 7.2 阈值三档（硬性约束：计费调用前必须检查）

| 条件 | 行为 |
|------|------|
| `余额 > 阈值 + 预估消耗` | 正常执行 |
| `阈值 < 余额 ≤ 阈值 + 预估消耗` | 执行，附 `warnings` |
| `余额 ≤ 阈值` | 拒绝，`insufficient_balance` |
| 余额未知 + `strict_balance=True` | 拒绝，`balance_unavailable` |
| 余额未知 + `strict_balance=False`（默认） | 执行，附 `warnings` |

`estimated_cost` 由 registry 的 `pricing_per_1k_chars` 与文本长度算得；
克隆按 `clone_unit_price` 固定单价。缺价格信息时记 0 并附 warning。

---

## 8. 密钥存储（`core/secret_manager.py`）

### 8.1 双层设计

1. **主密钥（KEK）**
   - 首选：`keyring` 存入 Windows Credential Manager，服务名 `qwen-voice-mcp`，用户名 `vault-master-key`
   - 回退：用户口令 → `PBKDF2HMAC(SHA256, length=32, iterations=600_000)` + 随机 16 字节盐
2. **密文库**：`%APPDATA%\qwen-voice-mcp\vault.enc`（可用 `QWEN_VOICE_VAULT` 覆盖）

### 8.2 存储格式

```json
{"v": 1, "nonce": "<b64 12B>", "ct": "<b64 密文+GCM tag>", "salt": "<b64 16B>",
 "mode": "keyring|passphrase"}
```

- AES-256-GCM，nonce 每次写入重新随机
- 写盘：同目录临时文件 + `os.replace` 原子替换
- 并发：`filelock` 独占锁
- 权限：文件 ACL 收紧至当前用户（Windows `icacls` 尽力而为，失败仅 warning）

### 8.3 接口

```python
class SecretManager:
    def __init__(self, path: Path | None = None, *, prefer_keyring: bool = True): ...
    def store(self, name: str, value: str) -> None: ...
    def retrieve(self, name: str) -> str | None: ...
    def delete(self, name: str) -> bool: ...
    def list_names(self) -> list[str]: ...
    def load_into_env(self, name: str = "DASHSCOPE_API_KEY") -> bool: ...
    @staticmethod
    def redact(value: str | None) -> str: ...   # sk-ab…1234
```

`load_into_env` 仅在环境变量缺失时注入，保证「环境变量优先」。

### 8.4 安全不变量

- 明文密钥只存在于进程内存与函数局部变量
- `redact()` 用于一切日志与错误消息；`errors.py` 出口统一过一遍
- stdio 模式日志只写 `sys.stderr`（`logging` handler 显式指向 stderr）

---

## 9. 配置（`config.py` + `core/config_store.py`）

优先级：**CLI 参数 > 环境变量（`QWEN_VOICE_*`） > `%APPDATA%\qwen-voice-mcp\config.json` > 默认值**

字段：`transport`、`host`（默认 `127.0.0.1`）、`port`（默认 8765）、`default_model`、
`default_voice`（默认 `longanyang`）、`balance_threshold`（默认 1.00）、`strict_balance`
（默认 `false`）、`balance_timeout_seconds`、`http_auth_token`、`output_dir`、`temp_dir`、
`audio_max_seconds`（60）、`audio_min_seconds`（5）、`audio_max_bytes`（20MB）、`log_level`。

`pydantic` 模型校验类型与范围；`host` 非环回时拒绝启动（除非 `allow_non_loopback=True`）。

---

## 10. 音频处理（`core/audio_utils.py`）

### 10.1 后端可插拔

```
soundfile (libsndfile 1.2.2)  →  首选，原生支持 WAV/MP3/FLAC/OGG/AIFF/CAF
    ↓ 不可用
pydub                          →  需 ffmpeg，仅当 ffmpeg 存在时启用
    ↓ 不可用
stdlib                         →  wave 模块（WAV）+ MP3 帧头扫描
```

`AudioProbe` 返回 `{path, format, duration_seconds, sample_rate, channels, size_bytes, backend}`。

### 10.2 校验规则

| 项 | 规则 | 失败 status |
|----|------|------------|
| 存在性 | 文件存在且可读 | `invalid_input` |
| 大小 | ≤ `audio_max_bytes`（20MB） | `invalid_input` |
| 格式 | 后缀 + 魔数双重校验，白名单 wav/mp3/m4a/flac/ogg/aac | `invalid_input` |
| 时长 | `audio_min_seconds` ≤ d ≤ `audio_max_seconds`（默认 5–60s） | `invalid_input` |
| 采样率 | 8k–48k，越界仅 warning（不阻断） | warning |

m4a/aac 若后端解析不出时长 → 仅报 format 并附 warning，不误判失败。

### 10.3 临时文件

`output_dir` 下生成 `qwen_tts_<UTC时间戳>_<4位随机>.<fmt>`；
`cleanup_temp_files(max_age_hours)` 清理超过 24h 的产物，`--selftest` 与启动时各调一次。

---

## 11. Qwen 客户端（`core/qwen_client.py`）

```python
class QwenClient:
    def __init__(self, api_key: str | None, *, timeout: float, registry: ModelRegistry): ...
    def synthesize(self, text, model, voice, output_format, output_path) -> SynthOutcome
    def upload_reference(self, audio_path: Path) -> str          # Files.upload → url/file_id
    def clone_voice(self, url, target_model, prefix, language_hints) -> CloneOutcome
    def query_voice(self, voice_id) -> dict | None
    def verify_model(self, model: str) -> bool
```

要点：

- 密钥解析顺序：显式参数 → `DASHSCOPE_API_KEY` → vault
- `SpeechSynthesizer(model=..., voice=..., format=AudioFormat.MP3_24000HZ_MONO_256KBPS).call(text, timeout_millis=...)`
- `output_format` → `AudioFormat` 映射表；不支持时 `invalid_input`
- `VoiceEnrollmentService(api_key=...).create_voice(target_model, prefix, url, language_hints)`
- 克隆后 `query_voice` 轮询（最多 3 次、间隔 1s），失败不谎报成功
- 所有异常经 `errors.classify()` 映射为 `Status`

### 11.1 `prefix` 生成规则

`voice_name` → 小写化 → 仅保留 `[a-z0-9]` → 取前 6 位 → 补足随机 2 位，确保
**长度 <10 且唯一**；`voice_name` 作为本地别名存入 `%APPDATA%\qwen-voice-mcp\voices.json`，
映射到返回的 `voice_id`，供后续 TTS 直接用 `voice_name` 引用。

---

## 12. MCP 工具（`tools/`）与 server

| 工具 | 参数 | 返回 |
|------|------|------|
| `list_voice_models` | 无 | §6.3 |
| `synthesize_speech` | `text`(必), `model`, `voice`, `output_format` | §6.3 |
| `clone_voice` | `audio_path`(必), `voice_name`(必), `model`, `language_hints` | §6.3 |
| `check_balance` | 无 | §6.3 |

- `audio_path` 支持本地路径**或** `data:` / 裸 base64（自动落盘到 temp 再处理）
- `tools/__init__.py::register_all(mcp, services)` 统一注册，`server.py` 调用
- 工具内**不** try/except 兜底——`services` 已保证不抛异常；若仍抛出，由
  `server.py` 的外层包装转成 `internal_error`（不泄露堆栈）

### 12.1 启动

```python
mcp = FastMCP(name="qwen-voice-mcp", instructions="...", host=..., port=...)
register_all(mcp, services)
mcp.run(transport=cfg.transport)   # 'stdio' | 'streamable-http' | 'sse'
```

- stdio：日志 → stderr
- streamable-http：绑定 `127.0.0.1`，`Authorization: Bearer <http_auth_token>` 校验中间件

---

## 13. GUI（`gui/config_window.py`，Tkinter 纯标准库）

布局对齐说明书 §3.1：

```
Qwen Voice MCP — 配置中心
[API 配置]  API Key: [••••••]  [验证]   状态: ● 已验证 — 余额 ¥12.50
[服务配置]  传输方式 (o) stdio ( ) streamable-http   端口 [8765]
            默认音色 [longanyang v]   余额阈值 [1.00]   严格模式 [ ]
[保存配置] [测试连接] [清除密钥] [刷新模型]
```

- API Key 默认 `show="•"`，可切换明文
- 保存前先 `validate_key()`（轻量余额/模型探测），通过才写入 vault
- 余额校验走 `balance_checker`，不额外计费
- 所有耗时操作放线程 + `queue` 回主线程更新 UI，避免界面卡死
- 无显示环境时 `import tkinter` 失败 → CLI 提示改用 `qwen-voice-mcp config set-key`

---

## 14. CLI（`cli.py`，typer）

```
qwen-voice-mcp run [--transport stdio|streamable-http] [--port N]
qwen-voice-mcp gui
qwen-voice-mcp selftest
qwen-voice-mcp verify-models
qwen-voice-mcp config show|set-key|clear-key|set KEY VALUE
qwen-voice-mcp balance
```

根 `server.py` 等价于 `qwen-voice-mcp run`，兼容说明书 §9.3 的 `python server.py --transport stdio`。

---

## 15. 测试策略

| 层 | 文件 | 覆盖 |
|----|------|------|
| 纯单元 | `test_audio_utils.py` | 现场生成 WAV/FLAC；边界 4.9/5.0/60.0/60.1s；魔数不符；超大文件；后端降级 |
| 纯单元 | `test_secret_manager.py` | 往返、错口令、GCM 篡改检测、并发写锁、`redact()` |
| 纯单元 | `test_config_store.py` | 四级优先级、范围校验、非环回拒绝 |
| 纯单元 | `test_model_registry.py` | 查找、默认模型、`verified` 标记、未知模型 |
| 纯单元 | `test_balance_checker.py` | 端点回退链、字段名识别、阈值三档、未知余额两模式 |
| mock | `test_qwen_client.py` | 异常分类、AudioFormat 映射、prefix 生成合法性（<10、小写字母数字） |
| mock | `test_services.py` | **计费闸门调用顺序**：余额不足时合成函数零调用 |
| e2e | `test_mcp_tools.py` | `mcp.shared.memory` in-memory client：工具清单、schema、4 工具调用结果 |

约束：**测试全程离线**，`dashscope` 与 `httpx` 全部 monkeypatch；不消耗任何额度。

---

## 16. 验收命令

```powershell
& "D:\Conda_envs\mcp-dev\python.exe" -m pytest -q
& "D:\Conda_envs\mcp-dev\python.exe" server.py --selftest
& "D:\Conda_envs\mcp-dev\python.exe" -m qwen_voice_mcp selftest
```

三项全绿方视为完成。

---

## 17. 安全清单（对齐说明书 §8）

| 维度 | 措施 |
|------|------|
| 密钥存储 | AES-256-GCM；KEK 由 Windows 凭据管理器或 PBKDF2 派生 |
| 密钥传输 | 环境变量 / 内存 / 加密通道；不写明文文件 |
| stdio 日志 | 严禁 stdout；logging 显式 stderr |
| HTTP 模式 | Bearer 校验；强制绑定 127.0.0.1 |
| 文件上传 | 后缀 + 魔数 + 大小 + 时长白名单；隔离 temp；用后清理 |
| 错误消息 | 不含路径、密钥、堆栈；统一 `redact()` |
| 依赖 | 不引用无法验证的第三方密钥库 |
