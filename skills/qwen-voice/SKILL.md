---
name: qwen-voice
description: Use when the user wants text read aloud or turned into an audio file, wants a voice cloned from a reference recording, or asks about the speech account balance before/after a synthesis job. Covers DashScope/Qwen TTS, CosyVoice, and Sambert models.
---

# Qwen Voice

> 中文译本: [SKILL.zh-CN.md](SKILL.zh-CN.md)

Turn text into speech, clone a voice from a recording, and check speech-account balance.

## When to use

Use this skill when the user:

- asks to read text aloud, generate narration, dubbing, 口播, or an audio file from text;
- supplies a recording (5–60 s) and asks to copy / clone / replicate that voice, then speak in it;
- asks how much balance is left, or whether a synthesis job can be paid for;
- asks which speech models are available.

Do **not** use it for speech-to-text, music generation, sound effects, or video editing. It produces speech audio only.

## Tools

| Tool | Use it for | Billing |
|---|---|---|
| `list_voice_models` | See available models, capabilities, prices, and the current default model | free |
| `synthesize_speech` | Text → audio file | billed per character |
| `clone_voice` | Reference recording → reusable voice | billed per clone |
| `check_balance` | Account balance and threshold status | free |

Both billed tools check the balance automatically before running. So when the user only wants speech, call the speech tool directly — an extra `check_balance` beforehand adds nothing. Call `check_balance` when the user actually asks about the balance.

## What the tools return

| Tool | Key fields |
|---|---|
| `list_voice_models` | `models[]` (`name`, `type`, `capabilities`, `pricing`, `verified`), `default_model`, `count` |
| `synthesize_speech` | `audio_path`, `audio_format`, `duration_seconds`, `text_length`, `model_used`, `voice_used`, `estimated_cost` |
| `clone_voice` | `voice_id`, `voice_prefix`, `cloning_status`, `model_used`, `estimated_cost`, `voice_alias_saved` |
| `check_balance` | `balance`, `currency`, `threshold`, `is_sufficient`, `source`, `warnings` |

`verified: null` on a listed model means it has not been live-probed — it is still callable.

## How to use

### 1. Pick the model

If the user did not name a model, do not guess one and do not interrupt to ask: let the tool use its default, then state the model in your reply.

If the user names a model, pass it through. If it comes back `model_not_supported`, call `list_voice_models` and offer the real options — don't pre-validate every call.

Never invent a model name yourself from memory or old docs; those IDs may not exist.

Preset voices belong to a model family. When switching to a different family, omit `voice` and let the default apply — do not reuse a voice id from another family. No tool lists voices, so if a voice is rejected, report it and fall back to the default.

### 2. Synthesize speech

```
synthesize_speech(text, model?, voice?, output_format?)
```

- `text` — required, non-empty. Merge the user's paragraphs into one request when they are one continuous read; split only when the user wants separate files.
- `output_format` — `mp3` (default), `wav`, `pcm`, `opus`.
- On success, return `audio_path` to the user as a file link, plus duration. Do not dump the whole JSON result.

### 3. Clone a voice

```
clone_voice(audio_path, voice_name, model?, language_hints?)
```

- `audio_path` — local file path, `data:` URI, or base64. **Always pass an absolute path**; a relative path is resolved against the server's working directory, not yours. If the file only exists in the chat, materialize it first.
- Reference audio: 5–60 s, ≤ 20 MB, `wav` / `mp3` / `m4a` / `flac` / `ogg` / `aac`.
- `voice_name` — a human-friendly local alias, any language. It is only a label; it is **not** accepted as the `voice` argument later.
- Read `voice_id`, `cloning_status`, and `model_used` from the result.
  - `completed` — ready. Say so.
  - `processing` / `unknown` — the request was submitted and is queued upstream. There is no polling tool: tell the user to ask you again later. Never claim it finished.
- To speak in the cloned voice afterwards, pass the returned **`voice_id`** as `voice` to `synthesize_speech`, with the **`model_used`** from the clone result. If the clone request omitted `model`, that value is the server default.

### 4. Report balance

`check_balance` returns `balance`, `currency`, `threshold`, `is_sufficient`.

- `balance: null` means the balance endpoint could not be queried — it does **not** mean zero. Report "unknown" and let the user decide.
- Balance checks are free; the automatic one before a billed call is not charged twice.
- When a billed call is rejected for `insufficient_balance` or `balance_unavailable`, do not retry. The result carries `recharge_url` — give it to the user.

## Result handling

Every tool returns a `status` field. Act on it:

| `status` | What to do |
|---|---|
| `success` | Report the concrete artifact: `audio_path` / `voice_id`, model, voice, cost estimate |
| `invalid_input` | Fix the argument: empty text, unsupported format, clip too short/long/oversized, unrecognized audio |
| `model_not_supported` | Call `list_voice_models`, then retry with a listed model |
| `insufficient_balance` | Do not retry. Give the user `recharge_url` |
| `balance_unavailable` | Only in strict mode. Balance could not be verified, so the call was refused; give `recharge_url` or ask the user to retry later |
| `auth_error` | Tell the user to configure their API key themselves. Never ask for the key in chat |
| `upstream_error` / `internal_error` | Retry once. If it still fails, report the failure honestly |

## Rules

- Never invent an `audio_path`, a `voice_id`, or a success message. Only report what the tool returned.
- Never ask for, echo, or write an API key. Key setup is the user's job.
- Do not invent model names: take them from `list_voice_models` or from the user. Take prices from `pricing` / `estimated_cost`, never from memory.
- Do not claim a cloned voice is ready unless `cloning_status` is `completed`.
- Long text costs more and takes longer. For very long input, say so before running the job.
- If the user asks for "a different voice" without providing a recording, offer the available preset voices instead of cloning.

## Quick example

User: "把这段开场白读成 mp3" (text supplied)

1. Call `synthesize_speech(text="...", output_format="mp3")`.
2. Reply: "已生成：[开场白.mp3](<返回的 audio_path>)，时长 <返回的 duration_seconds> 秒，使用 `<model_used>` / `<voice_used>`，预估 ¥<estimated_cost>。"
