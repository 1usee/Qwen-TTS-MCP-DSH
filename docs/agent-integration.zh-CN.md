# 在其他 Agent 中接入 qwen-voice

> 语言：[English](agent-integration.md) | **简体中文**

本仓库产出的是一份**可移植的 Agent Skill**：[`skills/qwen-voice/SKILL.md`](../skills/qwen-voice/SKILL.md)
（中文译本：[`SKILL.zh-CN.md`](../skills/qwen-voice/SKILL.zh-CN.md)）。
它只描述「何时用、怎么用」，不包含任何 MCP 服务器实现细节，因此 Claude Code、Codex、DeepSeek Harness
以及支持 [Agent Skills](https://agentskills.io) 标准的客户端都能复用。

要让技能真正生效，需要两步：**注册 MCP 服务器**（让客户端看到 4 个工具）+ **安装 skill**（让 agent 知道怎么用）。

---

## 1. 先装好包并跑通自检

```powershell
pipx install .          # 或：uv tool install .
qwen-voice-mcp selftest
```

预期输出 `SELFTEST OK` 和四个工具名。依赖清单见 [`pyproject.toml`](../pyproject.toml)。

再跑一次真实 stdio 握手（模拟其他 agent 的客户端）：

```powershell
qwen-voice-mcp doctor
python scripts\stdio_smoke.py
```

`doctor` 的每一行都应为 `OK` / `已配置` / `已注册`；`stdio_smoke.py` 预期最后一行
`STDIO CONTRACT OK`，并列出 `check_balance`、`clone_voice`、`list_voice_models`、`synthesize_speech`。

> 从源码开发时才需要注意解释器：本项目需要同时装有 `dashscope` / `keyring` /
> `soundfile` / `mcp`（**<2**）的环境。装成命令行工具后，客户端配置里不再出现
> 解释器路径，也就不会因为换机器而失效。

---

## 2. 注册 MCP 服务器

一条命令，两个客户端都注册（幂等、可撤销）：

```powershell
qwen-voice-mcp install --client claude,codex
qwen-voice-mcp install --client claude,codex --uninstall   # 撤销
```

各客户端的默认写入位置、`--dry-run`、以及 `install` 不可用时的手动配置，见
[Claude Code / Codex 接入](claude-codex.md)。摘要：

### Claude Code

```powershell
claude mcp add qwen-voice --scope user -- qwen-voice-mcp run
```

或项目根目录 `.mcp.json`（仓库里已有一份可直接用）：

```json
{
  "mcpServers": {
    "qwen-voice": {
      "command": "qwen-voice-mcp",
      "args": ["run", "--transport", "stdio"]
    }
  }
}
```

用 `claude mcp list` 确认状态为 connected，会话里用 `/mcp` 查看工具。

### Codex

```powershell
codex mcp add qwen-voice -- qwen-voice-mcp run
```

或编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.qwen-voice]
command = "qwen-voice-mcp"
args = ["run", "--transport", "stdio"]
```

启动 `codex` 后用 `/mcp` 查看已连接的服务器与工具。

> ⚠️ 不要手工追加第二份同名表——TOML 里重复声明同一张表是语法错误。
> 需要修改请用 `qwen-voice-mcp install --client codex`，它会替换而不是追加。

### DeepSeek Harness

```powershell
qwen-voice-mcp print-config --client dsh
```

把输出粘进 `~/.dsh/profiles/<profile>/cordis.yml`。DSH 没有可安全自动合并的配置格式，
所以 `install --client dsh` 只打印片段、不改文件。

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

推荐用一条命令完成（隐藏输入、默认先验证、写入加密库）：

```powershell
qwen-voice-mcp setup
```

其他方式，优先级从高到低：

1. 环境变量 `DASHSCOPE_API_KEY`（不落盘，推荐 CI/临时使用；会覆盖加密库）
2. 加密库，由 `setup` / `config set-key` / GUI 写入
3. `.env` 文件（当前目录或配置目录，`QWEN_VOICE_NO_DOTENV=1` 可关闭）

`config set-key` 也可以管道输入，适合脚本：

```powershell
$env:MY_KEY | qwen-voice-mcp config set-key --stdin
```

密钥存于 `%APPDATA%\qwen-voice-mcp\vault.enc`，主密钥由系统凭据管理器保管；
无凭据管理器的环境（WSL/Docker/CI）自动回退到同目录密钥文件，`doctor` 会标明强度。

密钥不会、也不应该通过对话传给 agent——客户端配置里也不需要出现它。
