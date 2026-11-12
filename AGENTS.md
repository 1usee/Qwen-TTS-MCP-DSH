# AGENTS.md

本仓库是一个 MCP 服务器（DashScope 语音：TTS / 声音克隆 / 余额）。

## 给 agent 的使用指引

处理「文字转语音」「克隆音色」「查语音账户余额」这类请求时，先读：

    skills/qwen-voice/SKILL.md          # 英文（技能入口）
    skills/qwen-voice/SKILL.zh-CN.md    # 中文译本

那份文件只讲「何时用、怎么用」，不依赖本仓库的内部实现。工具通过 MCP 暴露为：

- `list_voice_models`
- `synthesize_speech`
- `clone_voice`
- `check_balance`

如果当前会话看不到这四个工具，说明 MCP 服务器尚未注册，参见 `docs/agent-integration.zh-CN.md`
（英文：`docs/agent-integration.md`）。

## 开发

```powershell
python server.py --selftest
python -m pytest -q
```

本机可用解释器：`D:\Conda_envs\mcp-dev\python.exe`（装齐了 `dashscope` / `keyring` /
`soundfile` / `mcp`）。注意 `mcp` 必须是 **<2**：2.x 把 `mcp.server.fastmcp` 改名成
`mcp.server.mcpserver`，服务会在 import 阶段直接挂掉。

- 测试全程离线，不消耗额度。
- 不要把 API Key 写进仓库、日志或对话；用 `DASHSCOPE_API_KEY` 环境变量，
  或让用户自己执行 `qwen-voice-mcp setup`（隐藏输入 + 加密库）。

### 排查（先跑这个，再猜）

```powershell
qwen-voice-mcp doctor
```

一次给出：mcp 版本是否兼容、依赖是否齐全、密钥来自环境变量还是加密库、
加密库的保护方式（keyring / 本机密钥文件 / 口令）、Claude Code 与 Codex 是否已注册。

### 客户端注册

```powershell
qwen-voice-mcp install --client claude,codex          # 幂等，可 --dry-run
qwen-voice-mcp install --client claude,codex --uninstall
qwen-voice-mcp print-config --client codex            # 只打印，不改文件
```

`install` 只写 `command` / `args`，**绝不写 API Key**——密钥由服务启动时从加密库注入。
向 `~/.codex/config.toml` 合并时用的是"切段替换 + 重新解析校验"，不要改成字符串追加：
同名 TOML 表被声明两次会让 Codex 读不了整个配置文件。
