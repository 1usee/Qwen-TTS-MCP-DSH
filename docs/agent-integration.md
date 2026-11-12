# Using qwen-voice from other agents

> Language: **English** | [简体中文](agent-integration.zh-CN.md)

This repository ships a **portable Agent Skill**: [`skills/qwen-voice/SKILL.md`](../skills/qwen-voice/SKILL.md)
(Chinese translation: [`SKILL.zh-CN.md`](../skills/qwen-voice/SKILL.zh-CN.md)).
It describes only *when* and *how* to use the tools — no MCP server internals — so Claude Code, Codex,
DeepSeek Harness, and any client that follows the [Agent Skills](https://agentskills.io) standard can reuse it.

Two steps make it work: **register the MCP server** (so the client sees the four tools) and **install the skill**
(so the agent knows how to use them).

---

## 1. Confirm the server runs

From the repository root, using an interpreter with the dependencies installed:

```powershell
D:\Conda_envs\mcp-dev\python.exe server.py --selftest
```

Expected: `SELFTEST OK` followed by the four tool names. Dependencies are listed in [`pyproject.toml`](../pyproject.toml).

Then exercise a real stdio handshake (the way another agent's client would):

```powershell
D:\Conda_envs\mcp-dev\python.exe scripts\stdio_smoke.py
```

Expected last line: `STDIO CONTRACT OK`, listing `check_balance`, `clone_voice`, `list_voice_models`,
and `synthesize_speech`.

> On this machine, `D:\Conda_envs\mcp-dev` is the only environment with `dashscope` / `keyring` /
> `soundfile` / `mcp` installed. `mcp-dev-2` lacks `keyring`, `dashscope`, and `soundfile`;
> the Python 3.15 on `PATH` has none of the project dependencies.

---

## 2. Register the MCP server

### Claude Code

Project scope (recommended — write `.mcp.json` at the repository root):

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

Or register from the command line:

```powershell
claude mcp add qwen-voice `
  -e PYTHONPATH="F:\MCP_Server\Qwen-TTS-MCP-DSH" `
  -- D:\Conda_envs\mcp-dev\python.exe `
     F:\MCP_Server\Qwen-TTS-MCP-DSH\server.py --transport stdio
```

Confirm with `claude mcp list` (status should be connected), and inspect the tools with `/mcp` in a session.

### Codex

Edit `~/.codex/config.toml`:

```toml
[mcp_servers.qwen-voice]
command = "D:\\Conda_envs\\mcp-dev\\python.exe"
args = ["F:\\MCP_Server\\Qwen-TTS-MCP-DSH\\server.py", "--transport", "stdio"]

[mcp_servers.qwen-voice.env]
PYTHONPATH = "F:\\MCP_Server\\Qwen-TTS-MCP-DSH"
```

Start `codex` and use `/mcp` to see connected servers and their tools.

### DeepSeek Harness

`~/.dsh/profiles/<profile>/cordis.yml` already contains an equivalent entry (currently pointing at
`F:\dsh-MCP\Qwen-TTS-MCP-DSH`, which is identical to this repository). To switch it to this checkout,
change `command`, `args`, and `PYTHONPATH`.

---

## 3. Install the skill

`skills/qwen-voice/` is the single source of truth. Do not hand-edit copies elsewhere.

### Claude Code

```powershell
# Personal scope: available in every project
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.claude\skills\qwen-voice"

# Or project scope: only this repository
New-Item -ItemType Directory -Force ".claude\skills" | Out-Null
Copy-Item -Recurse -Force "skills\qwen-voice" ".claude\skills\qwen-voice"
```

Claude Code loads `<skills>/qwen-voice/SKILL.md` automatically; you can also invoke it directly with
`/qwen-voice`.

### Codex

Skills-directory support varies across Codex versions. Either approach works:

```powershell
# A. For versions that read a skills directory
New-Item -ItemType Directory -Force "$HOME\.codex\skills" | Out-Null
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.codex\skills\qwen-voice"
```

```markdown
<!-- B. Fallback: point at the skill from the project/global AGENTS.md -->
- Speech synthesis / cloning / balance: read `skills/qwen-voice/SKILL.md` first and follow its tool usage.
```

### DeepSeek Harness

```powershell
# User scope (all sessions)
Copy-Item -Recurse -Force "skills\qwen-voice" "$HOME\.dsh\skills\qwen-voice"

# Or project scope
Copy-Item -Recurse -Force "skills\qwen-voice" ".dsh\skills\qwen-voice"
```

---

## 4. Acceptance

Ask the target agent these three things:

| Prompt | Expected behavior |
|---|---|
| "List the available speech models" | Calls `list_voice_models`, lists the four models and `default_model` |
| "Read 'hello world' as an mp3" | Calls `synthesize_speech`, returns a real `audio_path`, and states the model and voice used |
| "How much balance is left?" | Calls `check_balance`; if `balance` is null, says "unknown" rather than 0 |

Then check the boundary case:

| Prompt | Expected behavior |
|---|---|
| "Clone the voice in this recording, then read the next paragraph in my name" | Calls `clone_voice`, gets a `voice_id`; the follow-up `synthesize_speech` passes **`voice_id`** (not the alias) and reuses the clone's model |

---

## 5. API key

Pick one, highest priority first:

1. Environment variable `DASHSCOPE_API_KEY` (nothing on disk; best for CI or temporary use)
2. GUI: `qwen-voice-mcp gui` → save (writes to the encrypted vault)
3. CLI: `qwen-voice-mcp config set-key sk-xxxx`

The key is never — and should never be — passed to an agent through chat. No key is stored on this
machine yet (`check_balance` reports it as unconfigured).
