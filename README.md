# astrbot_plugin_vision_bridge

为**非多模态 LLM** 桥接视觉理解能力的 AstrBot 插件。

## 它解决什么问题

你的主对话模型（例如 DeepSeek、本地部署的文本模型等）不支持图片输入。本插件让它“看得见图”：

- **纯文本对话**：插件不做任何事，**绝不调用视觉模型**，零额外开销。
- **收到图片时**：自动用另一个多模态视觉模型理解图片 → 把理解结果注入上下文 → 交给主 LLM 生成最终回复。
- 整个过程对用户**完全透明**，用户只会看到一条自然、已融合图片信息的回复。

## 工作原理

插件挂在 AstrBot 的 `on_llm_request` 钩子上，在主 LLM 被调用前介入：

```
用户消息 ──▶ on_llm_request 钩子
                  │
        req.image_urls 为空？
          ├─ 是 ─▶ 直接放行（不调用视觉模型）──▶ 主 LLM 正常回复
          └─ 否 ─▶ 调用视觉模型理解图片
                       └─▶ 把描述注入 req.prompt / system_prompt
                       └─▶ 清空 req.image_urls（主 LLM 非多模态）
                       └─▶ 主 LLM 结合描述生成最终回复
```

关键点：清空 `req.image_urls` 是必须的，否则非多模态的主 LLM 会因为收到图片而报错。

## 安装

把整个 `astrbot_plugin_vision_bridge` 目录放入 AstrBot 的 `data/plugins/` 下，然后在面板「插件管理」中重载即可。

## 配置

在插件配置页填写：

| 配置项 | 说明 |
| --- | --- |
| `vision_provider_id` | **必填**。先在「服务提供商」里创建一个多模态视觉模型（GPT-4o / qwen-vl / gemini 等），把它的 ID 填这里。 |
| `vision_prompt` | 下发给视觉模型的描述指令，可自定义详细程度与语言。 |
| `inject_mode` | `prompt`（默认，描述进入对话历史，支持追问图片细节）或 `system_prompt`（仅本轮生效）。 |
| `pass_user_text_to_vision` | 是否把用户文字一起发给视觉模型，让描述更有针对性。默认开。 |

> 注意：主 LLM 与视觉模型应是**两个不同的提供商**——主 LLM 处理文本，`vision_provider_id` 指向多模态模型。

## 文件结构

```
astrbot_plugin_vision_bridge/
├── main.py            # 插件主逻辑
├── metadata.yaml      # 插件元信息
├── _conf_schema.json  # 配置项 schema（生成可视化配置页）
├── requirements.txt   # 依赖（本插件无额外依赖）
└── README.md
```
