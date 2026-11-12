# 在其他 Agent 中接入 qwen-voice

> 语言：[English](agent-integration.md) | **简体中文**

本仓库产出的是一份**可移植的 Agent Skill**：[`skills/qwen-voice/SKILL.md`](../skills/qwen-voice/SKILL.md)
（中文译本：[`SKILL.zh-CN.md`](../skills/qwen-voice/SKILL.zh-CN.md)）。
它只描述「何时用、怎么用」，不包含任何 MCP 服务器实现细节，因此 Claude Code、Codex、DeepSeek Harness
以及支持 [Agent Skills](https://agentskills.io) 标准的客户端都能复用。

要让技能真正生效，需要两步：**注册 MCP 服务器**（让客户端看到 4 个工具）+ **安装 skill**（让 agent 知道怎么用）。

---

## 1. 先确认服务器本身能跑

在仓库根目录执行（用装好依赖的解释器）：

```powershell
D:\Conda_envs\mcp-dev\python.exe server.py --selftest
```

预期输出 `SELFTEST OK` 和四个工具名。依赖清单见 [`pyproject.toml`](../pyproject.toml)。

再跑一次真实 stdio 握手（模拟其他 agent 的客户端）：

```powershell
D:\Conda_envs\mcp-dev\python.exe scripts\stdio_smoke.py
```

预期最后一行 `STDIO CONTRACT OK`，并列出 `check_balance`、`clone_voice`、`list_voice_models`、`synthesize_speech`。

> 本机现状：`D:\Conda_envs\mcp-dev` 是唯一装齐 `dashscope` / `keyring` / `soundfile` / `mcp` 的环境。
> `mcp-dev-2` 缺 `keyring`、`dashscope`、`soundfile`；PATH 上的 Python 3.15 没有项目依赖。

---

## 2. 注册 MCP 服务器

### Claude Code

项目级（推荐，写入仓库根目录 `.mcp.json`）：

```json
{
  "mcpServers": {
    "qwen-voice": {
      "command": "D:\\Conda_envs\\mcp-dev\\python.exe",
      "args": [
        "F:\\MCP_Server\\Qwen-TTS-MCP-DSH\\server.py",
        "--transport",
        "stdio"
      ],
      "env": {
        "PYTHONPATH": "F:\\MCP_Server\\Qwen-TTS-MCP-DSH"
      }
    }
  }
}
```

或命令行注册：

```powershell
claude mcp add qwen-voice `
  -e PYTHONPATH="F:\MCP_Server\Qwen-TTS-MCP-DSH" `
  -- D:\Conda_envs\mcp-dev\python.exe `
     F:\MCP_Server\Qwen-TTS-MCP-DSH\server.py --transport stdio
```

用 `claude mcp list` 确认状态为 connected，会话里用 `/mcp` 查看工具。

### Codex

编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.qwen-voice]
command = "D:\\Conda_envs\\mcp-dev\\python.exe"
args = ["F:\\MCP_Server\\Qwen-TTS-MCP-DSH\\server.py", "--transport", "stdio"]

[mcp_servers.qwen-voice.env]
PYTHONPATH = "F:\\MCP_Server\\Qwen-TTS-MCP-DSH"
```

启动 `codex` 后用 `/mcp` 查看已连接的服务器与工具。

### DeepSeek Harness

`~/.dsh/profiles/<profile>/cordis.yml` 中已有一份等价配置（当前指向 `F:\dsh-MCP\Qwen-TTS-MCP-DSH`，与本仓库内容一致）。
若要改为本仓库路径，改 `command` / `args` / `PYTHONPATH` 三处即可。

---

## 3. 安装 skill

`skills/qwen-voice/` 是唯一源文件，不要在多处手改副本。

### Claude Code

```powershell
# 个人级：所有项目可用
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.claude\skills\qwen-voice"

# 或项目级：只在本仓库生效
New-Item -ItemType Directory -Force ".claude\skills" | Out-Null
Copy-Item -Recurse -Force "skills\qwen-voice" ".claude\skills\qwen-voice"
```

Claude Code 会自动加载 `<skills>/qwen-voice/SKILL.md`，也可用 `/qwen-voice` 直接调用。

### Codex

Codex 的各版本对 skills 目录支持不一致。两种做法都可用：

```powershell
# A. 支持 skills 目录的版本
New-Item -ItemType Directory -Force "$HOME\.codex\skills" | Out-Null
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.codex\skills\qwen-voice"
```

```markdown
<!-- B. 兜底：在项目/全局 AGENTS.md 里加一行引用 -->
- 语音合成/克隆/余额：先读 `skills/qwen-voice/SKILL.md`，按其中的工具用法执行。
```

### DeepSeek Harness

```powershell
# 用户级（所有会话）
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.dsh\skills\qwen-voice"

# 或项目级
Copy-Item -Recurse -Force "skills\qwen-voice" ".dsh\skills\qwen-voice"
```

---

## 4. 验收

在目标 agent 里依次试三句话：

| 说法 | 期望行为 |
|---|---|
| 「列出可用的语音模型」 | 调用 `list_voice_models`，列出 4 个模型与 `default_model` |
| 「把“你好世界”读成 mp3」 | 调用 `synthesize_speech`，返回真实 `audio_path` 并说明模型/音色 |
| 「账户余额还有多少」 | 调用 `check_balance`；`balance` 为 null 时明确说“未知”，不说 0 |

再用一句反例确认边界：

| 说法 | 期望行为 |
|---|---|
| 「把这段录音的声音克隆出来，然后用我的名字念下一段」 | 先 `clone_voice`，拿到 `voice_id`；后续 `synthesize_speech` 传 **`voice_id`**（不是别名），且模型与克隆时一致 |

---

## 5. 密钥

三种方式任选其一，优先级从高到低：

1. 环境变量 `DASHSCOPE_API_KEY`（不落盘，推荐 CI/临时使用）
2. GUI：`qwen-voice-mcp gui` → 保存配置（写入加密 vault）
3. CLI：`qwen-voice-mcp config set-key sk-xxxx`

密钥不会、也不应该通过对话传给 agent。当前机器上尚未保存任何密钥（`check_balance` 会提示未配置）。
