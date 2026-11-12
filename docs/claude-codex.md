# 接入 Claude Code 与 Codex

> 语言：**简体中文** | [English](claude-codex.md)（见文末术语对照）

本文只讲一件事：**怎么让 Claude Code 和 Codex 用上 qwen-voice 的四个工具**。
密钥怎么存、存在哪里、为什么不需要写进客户端配置，集中在
[README 的「配置 API Key」](../README.md#2-配置-api-key只需一次) 一节。

---

## 0. 前置：装好包

```powershell
pipx install .
# 或
uv tool install .
```

装完先确认命令存在（新开的终端里）：

```powershell
qwen-voice-mcp version
```

> 用 `pip install -e .` 也可以，但 `pipx` / `uv tool` 会把 `qwen-voice-mcp`
> 放进 PATH，客户端配置里才能只写命令名而不写解释器绝对路径——这是配置能在
> 不同机器上通用的前提。

---

## 1. 一次性配置密钥

```powershell
qwen-voice-mcp setup
```

输入时不回显 → 联网验证一次 → 写入加密库。**这一步 Claude Code 和 Codex 共用**，
不需要在两个客户端里各配一次。

验证：

```powershell
qwen-voice-mcp doctor
```

看到 `API Key │ 已配置` 即可。

---

## 2. 自动注册

```powershell
qwen-voice-mcp install --client claude,codex
```

- 幂等：重复执行只会提示「无需改动」
- 安全：先解析目标文件再合并，不会覆盖你已经有的其他 MCP 服务器、注释和配置项；
  解析失败就中止，一个字节都不写
- 可撤销：`qwen-voice-mcp install --client claude,codex --uninstall`

只想看会改什么，不落盘：

```powershell
qwen-voice-mcp install --client claude,codex --dry-run
```

### 各客户端写到哪

| 客户端 | 默认目标 | 覆盖方式 |
|---|---|---|
| Claude Code | 当前目录的 `.mcp.json`（项目级） | `--project-dir <目录>`；或 `--use-cli` 走 `claude mcp add`（用户级） |
| Codex | `$CODEX_HOME/config.toml`，未设置则 `~/.codex/config.toml` | `--codex-home <目录>` |
| DeepseekHarness | 不写文件，只打印 YAML 片段 | 手动粘进 `cordis.patch.yml` |

---

## 3. 手动注册（`install` 不可用时）

### 3.1 只打印配置

```powershell
qwen-voice-mcp print-config --client claude
qwen-voice-mcp print-config --client codex
qwen-voice-mcp print-config --client dsh
```

输出里**没有 API Key**——密钥在加密库里，客户端不需要知道它。

### 3.2 Claude Code

命令行（用户级，所有项目可用）：

```powershell
claude mcp add qwen-voice --scope user -- qwen-voice-mcp run
```

或写进项目根目录 `.mcp.json`：

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

确认：

```powershell
claude mcp list
```

状态应为 `connected`；会话里 `/mcp` 能看到四个工具。

### 3.3 Codex

```powershell
codex mcp add qwen-voice -- qwen-voice-mcp run
```

或编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.qwen-voice]
command = "qwen-voice-mcp"
args = ["run", "--transport", "stdio"]
```

启动 `codex` 后用 `/mcp` 查看连接状态。

> ⚠️ 不要手写第二份 `[mcp_servers.qwen-voice]`。TOML 里同名表出现两次是语法错误，
> Codex 会直接读不了整个配置。需要改就用 `install`，它负责替换而不是追加。

### 3.4 可选：装上 Skill

MCP 让客户端"看得到工具"，Skill 让 agent"知道什么时候用"。两份可以都装：

```powershell
# Claude Code（个人级）
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.claude\skills\qwen-voice"

# Codex（支持 skills 目录的版本）
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.codex\skills\qwen-voice"
```

Codex 若没有 skills 目录机制，在项目的 `AGENTS.md` 里加一行即可：

```markdown
- 语音合成/克隆/余额：先读 `skills/qwen-voice/SKILL.md`，按其中的工具用法执行。
```

---

## 4. 密钥为什么不用写进客户端配置

服务启动时（`server.py` → `_default_secret_manager`）会读加密库，把密钥注入
**本进程**的环境变量。所以：

- 客户端配置里不出现 `DASHSCOPE_API_KEY`，配置文件可以放心提交/分享；
- 换客户端不用重新配置密钥；
- 优先级是 **环境变量 > 加密库**：如果某次想临时换密钥，`$env:DASHSCOPE_API_KEY=...`
  即可，不必动加密库。

加密库位置：

```
%APPDATA%\qwen-voice-mcp\vault.enc
```

主密钥由系统凭据管理器保管（Windows 凭据管理器 / macOS Keychain /
Linux Secret Service）。**在没有凭据管理器的机器上**（WSL、Docker、CI），
自动回退为同目录的 `vault.enc.key` 文件——这时的保护强度只相当于
"文件权限"，`doctor` 会如实标出 `keyfile`。

需要在 CI 里完全无状态运行，就别用加密库：

```powershell
$env:DASHSCOPE_API_KEY = "sk-..."     # 只在当前进程有效
qwen-voice-mcp run
```

---

## 5. 验收与排障

### 5.1 三句话验收

| 在客户端里说 | 期望 |
|---|---|
| 列出可用的语音模型 | 调用 `list_voice_models`，返回 4 个模型与 `default_model` |
| 把"你好世界"读成 mp3 | 调用 `synthesize_speech`，返回真实 `audio_path` 与时长 |
| 账户余额还有多少 | 调用 `check_balance`；`balance` 为 `null` 时明确说"未知" |

### 5.2 工具不出现

按顺序排查：

```powershell
qwen-voice-mcp doctor
```

| doctor 里看到 | 含义 | 处理 |
|---|---|---|
| `mcp 包 │ 不兼容` | 装到了 mcp 2.x（`FastMCP` 已改名） | `pip install "mcp>=1.2,<2"` |
| `API Key │ 未配置` | 没存过密钥 | `qwen-voice-mcp setup` |
| `Claude Code │ 未注册` | 当前目录没有 `.mcp.json` | `qwen-voice-mcp install --client claude` |
| `Codex │ 未注册` | `config.toml` 里没有该表 | `qwen-voice-mcp install --client codex` |
| `密钥库 │ 不存在` | vault 文件缺失 | 重新 `setup` |

其他常见原因：

- **`qwen-voice-mcp` 不在 PATH**：客户端拉起子进程时用的是它自己的环境。
  用 `where.exe qwen-voice-mcp` 确认；或在配置里写绝对路径。
  这也是建议用 `pipx` 而不是 `pip install -e .` 的原因。
- **改了配置没重启**：Claude Code / Codex 都需要重启会话才会重新读 MCP 配置。
- **路径写成了 `python server.py`**：只有在项目目录里、且用对了解释器时才行；
  换成 `qwen-voice-mcp run` 更省事。

### 5.3 提示 `auth_error`

密钥没被读到，或无效。先 `qwen-voice-mcp doctor` 看 `API Key` 一行的来源，
再 `qwen-voice-mcp balance` 实际探测一次（免费）。仍失败就重新
`qwen-voice-mcp setup`。

**不要把密钥贴进对话让 agent 帮你配**——agent 不需要、也不应该看到它。
