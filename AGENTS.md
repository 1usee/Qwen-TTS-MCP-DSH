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
D:\Conda_envs\mcp-dev\python.exe server.py --selftest
D:\Conda_envs\mcp-dev\python.exe -m pytest -q
```

- 需要含 `dashscope` / `keyring` / `soundfile` / `mcp` 的解释器（本机为 `D:\Conda_envs\mcp-dev`）。
- 测试全程离线，不消耗额度。
- 不要把 API Key 写进仓库、日志或对话；用 `DASHSCOPE_API_KEY` 环境变量或 `qwen-voice-mcp config set-key`。
