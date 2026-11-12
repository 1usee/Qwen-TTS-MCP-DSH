# Qwen Voice

> 本文是 [`SKILL.md`](SKILL.md) 的简体中文译本，供阅读与校对。
> **技能入口仍是 `SKILL.md`**（英文，Agent Skills 的通用做法）；安装时不要把本文件当成第二个技能。

把文字变成语音、用录音克隆音色、查询语音账户余额。

## 何时使用

遇到以下情况时使用本技能：

- 用户要求朗读文字、生成旁白 / 配音 / 口播，或把文本转成音频文件；
- 用户提供一段录音（5–60 秒），要求复制 / 克隆 / 模仿该音色，再用它说话；
- 用户询问余额还有多少，或询问某次合成是否付得起；
- 用户询问有哪些语音模型可用。

**不要**用于语音识别、音乐生成、音效制作或视频剪辑。它只产出语音音频。

## 工具

| 工具 | 用途 | 计费 |
|---|---|---|
| `list_voice_models` | 查看可用模型、能力、价格与当前默认模型 | 免费 |
| `synthesize_speech` | 文本 → 音频文件 | 按字符计费 |
| `clone_voice` | 参考录音 → 可复用音色 | 按次计费 |
| `check_balance` | 账户余额与阈值状态 | 免费 |

两个计费工具在运行前都会自动检查余额。所以用户只是要合成语音时，直接调语音工具即可——事先多调一次
`check_balance` 没有意义。只有当用户确实在问余额时才调用 `check_balance`。

## 工具返回什么

| 工具 | 关键字段 |
|---|---|
| `list_voice_models` | `models[]`（`name`、`type`、`capabilities`、`pricing`、`verified`）、`default_model`、`count` |
| `synthesize_speech` | `audio_path`、`audio_format`、`duration_seconds`、`text_length`、`model_used`、`voice_used`、`estimated_cost` |
| `clone_voice` | `voice_id`、`voice_prefix`、`cloning_status`、`model_used`、`estimated_cost`、`voice_alias_saved` |
| `check_balance` | `balance`、`currency`、`threshold`、`is_sufficient`、`source`、`warnings` |

模型条目上的 `verified: null` 表示它还没被在线探测过——仍然可以调用。

## 怎么用

### 1. 选择模型

用户没有指定模型时，不要猜，也不要打断去问：让工具用默认模型，然后在回复里说明用的是哪个。

用户指定了模型时，直接传进去。若返回 `model_not_supported`，再调 `list_voice_models` 并给出真实可用的选项——不要每次调用前都预校验。

不要凭记忆或旧文档自己编模型名；那些 ID 可能并不存在。

预置音色属于某个模型系列。切换到不同系列时，省略 `voice` 让默认音色生效——不要复用别的系列的音色 ID。没有工具可以列音色，所以音色被拒时如实报告，并回退到默认音色。

### 2. 合成语音

```
synthesize_speech(text, model?, voice?, output_format?)
```

- `text` —— 必填、非空。如果用户的多段文字是一次连续朗读，合并成一次请求；只有用户要多个文件时才拆分。
- `output_format` —— `mp3`（默认）、`wav`、`pcm`、`opus`。
- 成功后把 `audio_path` 作为文件链接返回给用户，并附上时长。不要把整段 JSON 原样倒出来。

### 3. 克隆音色

```
clone_voice(audio_path, voice_name, model?, language_hints?)
```

- `audio_path` —— 本地文件路径、`data:` URI 或 base64。**始终传绝对路径**；相对路径会按服务器的工作目录解析，而不是你的。如果文件只存在于对话里，先把它落成真实文件。
- 参考音频：5–60 秒、≤ 20 MB，`wav` / `mp3` / `m4a` / `flac` / `ogg` / `aac`。
- `voice_name` —— 给人看的本地别名，任何语言都行。它只是标签；后续**不能**把它当作 `voice` 参数。
- 从结果里读 `voice_id`、`cloning_status` 和 `model_used`。
  - `completed` —— 已就绪，可以明确告诉用户。
  - `processing` / `unknown` —— 请求已提交、在上游排队。没有轮询工具：让用户稍后再来问。绝不要宣称已完成。
- 之后要用克隆音色说话，就把返回的 **`voice_id`** 作为 `voice` 传给 `synthesize_speech`，并使用克隆结果里的 **`model_used`**。如果克隆时没传 `model`，这个值就是服务器默认模型。

### 4. 报告余额

`check_balance` 返回 `balance`、`currency`、`threshold`、`is_sufficient`。

- `balance: null` 表示余额接口查不到——**不等于** 0。报告“未知”，让用户自己决定。
- 余额检查是免费的；计费调用前的自动检查不会重复计费。
- 计费调用被 `insufficient_balance` 或 `balance_unavailable` 拒绝时，不要重试。结果里带有 `recharge_url`——把它给用户。

## 结果处理

每个工具都会返回 `status` 字段，按它分支：

| `status` | 该做什么 |
|---|---|
| `success` | 报告具体产物：`audio_path` / `voice_id`、模型、音色、预估费用 |
| `invalid_input` | 修正参数：空文本、格式不支持、音频过短/过长/过大、无法识别的音频 |
| `model_not_supported` | 调 `list_voice_models`，再用列表里的模型重试 |
| `insufficient_balance` | 不要重试。把 `recharge_url` 给用户 |
| `balance_unavailable` | 仅严格模式。余额无法核实所以拒绝；把 `recharge_url` 给用户，或让用户稍后重试 |
| `auth_error` | 让用户自己配置 API Key。绝不在对话里索要密钥 |
| `upstream_error` / `internal_error` | 重试一次。仍失败就如实报告 |

## 规则

- 绝不虚构 `audio_path`、`voice_id` 或成功消息。只报告工具真正返回的内容。
- 绝不索要、回显或写入 API Key。密钥配置是用户自己的事。
- 不要编造模型名：从 `list_voice_models` 或用户那里取。价格取 `pricing` / `estimated_cost`，不要凭记忆。
- 除非 `cloning_status` 为 `completed`，不要宣称克隆音色已就绪。
- 长文本更贵也更慢。输入很长时，先说明再执行。
- 用户要求“换个音色”但没提供录音时，提供可用的预置音色，而不是去克隆。

## 快速示例

用户：“把这段开场白读成 mp3”（文本已提供）

1. 调用 `synthesize_speech(text="...", output_format="mp3")`。
2. 回复：“已生成：[开场白.mp3](<返回的 audio_path>)，时长 <返回的 duration_seconds> 秒，使用 `<model_used>` / `<voice_used>`，预估 ¥<estimated_cost>。”
