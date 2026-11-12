# Using qwen-voice from other agents

> Language: **English** | [简体中文](agent-integration.zh-CN.md)

This repository ships a **portable Agent Skill**: [`skills/qwen-voice/SKILL.md`](../skills/qwen-voice/SKILL.md)
(Chinese translation: [`SKILL.zh-CN.md`](../skills/qwen-voice/SKILL.zh-CN.md)).
It describes only *when* and *how* to use the tools — no MCP server internals — so Claude Code, Codex,
DeepSeek Harness, and any client that follows the [Agent Skills](https://agentskills.io) standard can reuse it.

Two steps make it work: **register the MCP server** (so the client sees the four tools) and **install the skill**
(so the agent knows how to use them).

---

## 1. Install the package and confirm it runs

```powershell
pipx install .          # or: uv tool install .
qwen-voice-mcp selftest
qwen-voice-mcp doctor
```

Expected: `SELFTEST OK` followed by the four tool names, then a `doctor` table where every row reads
`OK` / `已配置` / `已注册`. Dependencies are listed in [`pyproject.toml`](../pyproject.toml).

Then exercise a real stdio handshake (the way another agent's client would):

```powershell
python scripts\stdio_smoke.py
```

Expected last line: `STDIO CONTRACT OK`, listing `check_balance`, `clone_voice`, `list_voice_models`,
and `synthesize_speech`.

> Only when developing from source do you need to care about the interpreter: the project requires
> one environment that has `dashscope` / `keyring` / `soundfile` / `mcp` (**<2**) installed. Once
> installed as a command, client configs no longer contain an interpreter path — which is exactly
> what makes them portable across machines.

---

## 2. Register the MCP server

One command registers both clients (idempotent, reversible):

```powershell
qwen-voice-mcp install --client claude,codex
qwen-voice-mcp install --client claude,codex --uninstall   # undo
```

Per-client target paths, `--dry-run`, and the manual route are in
[Claude Code / Codex integration](claude-codex.md). In short:

### Claude Code

```powershell
claude mcp add qwen-voice --scope user -- qwen-voice-mcp run
```

Or `.mcp.json` at the repository root (this repository already ships one):

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

Confirm with `claude mcp list` (status should be connected), and inspect the tools with `/mcp`.

### Codex

```powershell
codex mcp add qwen-voice -- qwen-voice-mcp run
```

Or edit `~/.codex/config.toml`:

```toml
[mcp_servers.qwen-voice]
command = "qwen-voice-mcp"
args = ["run", "--transport", "stdio"]
```

Start `codex` and use `/mcp` to see connected servers and their tools.

> ⚠️ Never append a second `[mcp_servers.qwen-voice]` table: duplicate tables are a TOML syntax
> error and Codex will refuse the whole file. Use `qwen-voice-mcp install --client codex`, which
> replaces rather than appends.

### DeepSeek Harness

```powershell
qwen-voice-mcp print-config --client dsh
```

Paste the output into `~/.dsh/profiles/<profile>/cordis.yml`. DSH has no config format we can merge
safely, so `install --client dsh` only prints the fragment and never writes a file.

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

One command does it all (hidden input, validated, persisted):

```powershell
qwen-voice-mcp setup
```

Other routes, highest priority first:

1. Environment variable `DASHSCOPE_API_KEY` (nothing on disk; best for CI or temporary use — it
   overrides the vault)
2. The encrypted vault, written by `setup` / `config set-key` / the GUI
3. A `.env` file in the current directory or the config home (disable with
   `QWEN_VOICE_NO_DOTENV=1`)

`config set-key` also accepts piped input, for scripts:

```powershell
$env:MY_KEY | qwen-voice-mcp config set-key --stdin
```

The key lives in `%APPDATA%\qwen-voice-mcp\vault.enc`; its master key is held by the OS credential
store. Hosts without one (WSL, Docker, CI) fall back to a key file next to the vault, and `doctor`
reports which tier is active.

The key is never — and should never be — passed to an agent through chat, and it never appears in a
client config file.
