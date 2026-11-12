---
name: "qwen-voice"
description: "Dynamic access to qwen-voice MCP server (4 tools)"
version: "0.3.0"
---

# qwen-voice Skill

This skill provides dynamic access to the qwen-voice MCP server without loading all tool definitions into context.

## Context Efficiency

Traditional MCP approach:
- All 4 tools loaded at startup
- Estimated context: 2000 tokens

This skill approach:
- Metadata only: ~100 tokens
- Full instructions (when used): ~5k tokens
- Tool execution: 0 tokens (runs externally)

## How This Works

Instead of loading all MCP tool definitions upfront, this skill:
1. Tells you what tools are available (just names and brief descriptions)
2. You decide which tool to call based on the user's request
3. Generate a JSON command to invoke the tool
4. The executor handles the actual MCP communication

## Available Tools

- `list_voice_models`: 查询可用语音模型。用户询问“有哪些语音模型可用”时调用；返回模型清单、能力与默认模型。只读，不计费。
- `synthesize_speech(text*:str, model?, voice?, output_format?)`: 文字转语音。用户提交文字并要求语音输出时调用；执行前会自动检查账户余额，余额不足时返回 insufficient_balance 并提示充值。
- `clone_voice(audio_path*:str, voice_name*:str, model?, language_hints?)`: 声音克隆。用户上传语音文件并要求复制音色时调用；audio_path 支持本地路径、data: URI 或 base64，执行前自动检查余额。
- `check_balance`: 查询账户余额。用户询问余额，或执行计费调用前自动检查；端点不可用时返回 balance 为 null 并附警告，不算失败。

## Usage Pattern

When the user's request matches this skill's capabilities:

**Step 1: Identify the right tool** from the list above

**Step 2: Write the call as JSON** to a temporary file using a file-writing tool, without shell interpolation:

```json
{"tool": "tool_name", "arguments": {"param1": "value1"}}
```

**Step 3: Execute the call:**

```bash
npx -y mcp-to-skill@0.3.0 exec --config "$SKILL_DIR/mcp-config.json" --call-file "/tmp/mcp-call.json"
```

IMPORTANT: Replace `$SKILL_DIR` with the actual path to this skill directory. Do not place user-controlled argument values directly in a shell command.

## Commands

**Call a tool from a JSON file:**
```bash
npx -y mcp-to-skill@0.3.0 exec --config "$SKILL_DIR/mcp-config.json" --call-file "/path/to/call.json"
```

**List all tools:**
```bash
npx -y mcp-to-skill@0.3.0 exec --config "$SKILL_DIR/mcp-config.json" --list
```

**Get tool schema (if needed):**
```bash
npx -y mcp-to-skill@0.3.0 exec --config "$SKILL_DIR/mcp-config.json" --describe tool_name
```

## Example

User: "Use qwen-voice to do X"

```bash
npx -y mcp-to-skill@0.3.0 exec --config "$SKILL_DIR/mcp-config.json" --call-file "/tmp/mcp-call.json"
```

## Custom Instructions

<!-- mcp-to-skill:custom:start -->
Add project-specific guidance here. This section is preserved by `mcp-to-skill refresh`.
<!-- mcp-to-skill:custom:end -->

## Error Handling

If the executor returns an error:
- Check the tool name is correct
- Verify required arguments are provided (marked with `*` in signatures above)
- Ensure the MCP server is accessible

---

*This skill was auto-generated from an MCP server configuration.*
*Generator: [mcp-to-skill](https://github.com/larkinwc/ts-mcp-to-skill)*
