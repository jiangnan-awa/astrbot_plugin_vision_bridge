# astrbot_plugin_vision_bridge

为**非多模态 LLM** 桥接视觉理解能力的 AstrBot 插件。

## 它解决什么问题

你的主对话模型（例如 DeepSeek、本地部署的文本模型等）不支持图片输入。本插件让它“看得见图”：

- **纯文本对话**：插件不做任何事，**绝不调用视觉模型**，零额外开销。
- **收到图片时**：把发往主 LLM 的**完整请求**（人设 + 对话历史 + 用户问题 + 图片）转发给多模态视觉模型，让它据此产出**有针对性的理解/回答**（而非泛泛的图片描述）→ 把结果注入上下文 → 交给主 LLM 产出最终回复。
- 整个过程对用户**完全透明**，用户只会看到一条自然、已融合图片信息的回复。

## 工作原理

插件挂在 AstrBot 的 `on_llm_request` 钩子上，在主 LLM 被调用前介入：

```
用户消息 ──▶ on_llm_request 钩子
                  │
        req.image_urls 为空？
          ├─ 是 ─▶ 直接放行（不调用视觉模型）──▶ 主 LLM 正常回复
          └─ 否 ─▶ 把完整 req（system_prompt + contexts + prompt + image_urls）
                    转发给视觉模型 ──▶ 得到有针对性的理解
                       └─▶ 清空 req.image_urls（主 LLM 非多模态，必须清空）
                       └─▶ 把理解注入 req.prompt / system_prompt
                       └─▶ 主 LLM 结合理解生成最终回复
```

两个关键点：
1. 转发**完整请求**而非只发图片，视觉模型能结合人设和上下文给出更切题的回答，避免“先描述再二次理解”的信息损耗。
2. 调用主 LLM 前**清空 `req.image_urls`**，否则非多模态的主 LLM 会因为收到图片而报错。

后续追问：本插件不调用 `event.stop_event()`，主 LLM 这一轮正常运行，框架会**自动把本轮（纯文本）写入对话历史**。所以下一轮用户纯文字追问图片细节时，主 LLM 仍能看到上轮结论。

## 安装

把整个 `astrbot_plugin_vision_bridge` 目录放入 AstrBot 的 `data/plugins/` 下，在面板「插件管理」中重载即可。

> 用面板「上传 zip」安装时，请把 5 个文件先放进一个名为 `astrbot_plugin_vision_bridge` 的文件夹，再压缩**该文件夹**（zip 内应能看到 `astrbot_plugin_vision_bridge/main.py` 这样的路径）。直接选中文件压缩会触发 AstrBot 解压逻辑的限制而报 `WinError 267`。

## 配置

在插件配置页填写：

| 配置项 | 说明 |
| --- | --- |
| `vision_provider_id` | **必填**。配置页下拉直接选择一个多模态视觉模型（GPT-4o / qwen-vl / gemini 等）。需先在「服务提供商」里创建好。 |
| `vision_instruction` | 追加给视觉模型的系统指令，引导它产出切题回答而非泛泛描述。 |
| `forward_persona` | 是否把主对话人设一并转发给视觉模型，使回答口吻一致。默认开。 |
| `forward_history` | 是否把对话历史转发给视觉模型，使理解贴合上下文。默认开。 |
| `inject_mode` | `prompt`（默认，注入用户消息，随本轮写入历史，支持后续追问）或 `system_prompt`（仅本轮生效、不写入历史）。 |

> 注意：主 LLM 与视觉模型应是**两个不同的提供商**——主 LLM 处理文本，`vision_provider_id` 指向多模态模型。

## 文件结构

```
astrbot_plugin_vision_bridge/
├── main.py            # 插件主逻辑
├── metadata.yaml      # 插件元信息
├── _conf_schema.json  # 配置项 schema（生成可视化配置页，含模型下拉选择）
├── requirements.txt   # 依赖（本插件无额外依赖）
└── README.md
```
