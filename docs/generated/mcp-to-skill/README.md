# mcp-to-skill 生成物（存档）

这里保留了自动生成的原始草稿，用于对照与重新生成。**最终交付的 skill 不在这个目录**，
而是 [`skills/qwen-voice/SKILL.md`](../../../skills/qwen-voice/SKILL.md)（英文）与
[`SKILL.zh-CN.md`](../../../skills/qwen-voice/SKILL.zh-CN.md)（中文译本）。

```
qwen-voice/
├── SKILL.md          # mcp-to-skill 0.3.0 原样输出
├── mcp-config.json   # 生成时使用的服务器配置（含本机绝对路径）
└── tools.json        # 工具 schema 快照，可用于离线 refresh
input/
└── qwen-voice.json   # 传给 --mcp-config 的输入
```

## 重新生成

```powershell
npx -y mcp-to-skill generate `
  --mcp-config docs/generated/mcp-to-skill/input/qwen-voice.json `
  --server qwen-voice `
  --output-dir docs/generated/mcp-to-skill/qwen-voice
```

## 草稿 → 终稿改了什么

生成器产出的是一个「执行器说明书」：它教 agent 用 `npx mcp-to-skill exec` 调服务器，
因此整篇都在描述服务器本体（配置路径、`$SKILL_DIR`、exec 命令、工具快照）。终稿删掉了这些：

| 草稿里的内容 | 终稿的处理 |
|---|---|
| `This skill provides dynamic access to the qwen-voice MCP server…` | 删除。改为直接描述任务与四个工具 |
| Context Efficiency / How This Works（省 token 的原理） | 删除。与使用者无关 |
| `npx -y mcp-to-skill@0.3.0 exec --config "$SKILL_DIR/mcp-config.json"` | 删除。改为原生工具调用 |
| `--call-file`、`--list`、`--describe`、`$SKILL_DIR` | 删除 |
| `mcp-config.json` / `tools.json` / 服务器 command、args、env | 删除 |
| 自动生成的空 `Custom Instructions` 与 `Error Handling` | 删除，替换为基于 `status` 的真实分支 |
| 生成器署名与版本号 | 删除，仅保留 `name` + `description` |

同时补上了草稿没有、但 agent 必须知道的使用契约：何时不该用、必填/可选参数、
返回字段、余额自动检查、克隆后用 `voice_id` 而非别名、`cloning_status` 不能谎报、
`balance: null` 不等于 0、`recharge_url` 的用法。

## 为什么不用 refresh

`refresh` 会按生成模板重写 `SKILL.md`，只保留 custom-content 标记内的文字。终稿已经
脱离生成模板，所以这里把生成物作为快照存档；工具清单变化时重新 generate 到本目录，
再人工对照更新 `skills/qwen-voice/SKILL.md`。
