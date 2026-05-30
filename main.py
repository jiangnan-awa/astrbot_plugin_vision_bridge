from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig

# 当用户只发图片、没有任何文字时，用这句作为给视觉模型的兜底问题。
DEFAULT_IMAGE_ONLY_PROMPT = "请仔细观察并分析这张图片，描述其主要内容与值得注意的细节。"

# 追加给视觉模型的系统级指令，引导它产出"有针对性的回答"而非泛泛的描述。
DEFAULT_VISION_INSTRUCTION = (
    "你是图像理解助手。请结合上面的对话历史与用户的问题，仔细观察图片，"
    "给出准确、详尽且切题的分析与回答。若图片中包含文字，请尽量原样转述。"
    "只需输出对图片的理解内容，不要替用户做最终决策式的客套结尾。"
)


@register(
    "astrbot_plugin_vision_bridge",
    "YourName",
    "为非多模态 LLM 桥接视觉理解：收到图片时把完整请求交给视觉模型，再把其回答注入上下文交给主 LLM 产出最终回复。",
    "v2.0.0",
    "https://github.com/yourname/astrbot_plugin_vision_bridge",
)
class VisionBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.on_llm_request()
    async def bridge_vision(self, event: AstrMessageEvent, req: ProviderRequest):
        """在主 LLM 被调用前介入。

        - 纯文本对话：req.image_urls 为空 -> 直接返回，绝不触碰视觉模型。
        - 含图片对话：把【完整请求】（人设 system_prompt + 历史 contexts + 用户问题
          prompt + 图片）交给视觉模型，让它据此产出有针对性的回答；再清空图片、
          把该回答注入 req，交给非多模态的主 LLM 产出最终回复。
        """
        # 1) 纯文本：不存在图片，直接放行（满足"文本对话不调用视觉模型"）
        if not req.image_urls:
            return

        image_urls = list(req.image_urls)

        # 2) 取出配置好的视觉模型提供商
        provider_id = (self.config.get("vision_provider_id") or "").strip()
        if not provider_id:
            logger.warning(
                "[VisionBridge] 收到图片但未配置 vision_provider_id，跳过图片理解。"
                "请在插件配置中填写视觉模型提供商 ID。"
            )
            req.image_urls = []  # 主 LLM 非多模态，必须清空避免报错
            return

        vision_provider = self.context.get_provider_by_id(provider_id)
        if vision_provider is None:
            logger.error(
                f"[VisionBridge] 找不到 ID 为 '{provider_id}' 的提供商，"
                "请确认它已在「服务提供商」中创建并启用。"
            )
            req.image_urls = []
            return

        # 3) 把完整请求交给视觉模型，得到有针对性的理解/回答
        understanding = await self._understand(vision_provider, req, image_urls)

        # 4) 清空图片（关键：主 LLM 不支持图片），注入理解结果，放行给主 LLM
        req.image_urls = []
        if understanding:
            self._inject(req, understanding)
            logger.info(
                f"[VisionBridge] 已将视觉理解（{len(understanding)} 字）注入上下文，交由主 LLM 回复。"
            )
        else:
            logger.warning("[VisionBridge] 视觉模型未返回有效内容，本轮按无图继续。")
        # 注意：此处不调用 event.stop_event()，主 LLM 会正常运行并产出最终回复，
        # 同时由框架自动把这一轮（纯文本）写入对话历史，支持后续追问。

    async def _understand(self, vision_provider, req: ProviderRequest, image_urls):
        """把完整请求转发给视觉模型，返回它的纯文本回答。

        透传给视觉模型的内容（"全部信息"）：
          - system_prompt: 主对话的人设（可选，forward_persona）
          - contexts:      完整对话历史（可选，forward_history）
          - prompt:        用户本轮的真实问题
          - image_urls:    本轮图片
        text_chat 会把 prompt + image_urls 作为"最新一条用户消息"追加到 contexts 之后。
        """
        user_question = (req.prompt or "").strip() or DEFAULT_IMAGE_ONLY_PROMPT

        instruction = (
            self.config.get("vision_instruction") or ""
        ).strip() or DEFAULT_VISION_INSTRUCTION

        # 组装给视觉模型的 system_prompt
        if self.config.get("forward_persona", True) and (req.system_prompt or "").strip():
            vision_system = f"{req.system_prompt.strip()}\n\n{instruction}"
        else:
            vision_system = instruction

        # 历史上下文（防御性处理：确保是 list）
        contexts = None
        if self.config.get("forward_history", True):
            ctx = req.contexts
            if isinstance(ctx, list) and ctx:
                contexts = ctx

        try:
            resp = await vision_provider.text_chat(
                prompt=user_question,
                contexts=contexts,
                image_urls=image_urls,
                system_prompt=vision_system,
            )
        except Exception as e:  # 视觉模型异常不应让整条会话崩溃
            logger.error(f"[VisionBridge] 调用视觉模型失败：{e}")
            return ""

        return self._extract_text(resp)

    @staticmethod
    def _extract_text(resp) -> str:
        """从 LLMResponse 中稳健地取出纯文本。"""
        if resp is None:
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            chain = getattr(resp, "result_chain", None)
            if chain is not None:
                try:
                    text = (chain.get_plain_text() or "").strip()
                except Exception:
                    text = ""
        return text

    def _inject(self, req: ProviderRequest, understanding: str):
        """把视觉模型的理解结果注入主 LLM 的请求。

        inject_mode:
          - "prompt"        注入用户消息（会随本轮写入历史，原始理解可被后续追问引用）
          - "system_prompt" 注入系统提示（不写入历史，仅本轮生效）
        """
        block = (
            "[图片理解参考]\n"
            "用户在本轮消息中发送了图片，下面是视觉模型结合对话与图片给出的理解，"
            "请把它当作你已经看到图片后的认知，自然地融入回复，"
            "不要向用户提及这段参考或视觉模型的存在：\n"
            f"{understanding}"
        )

        mode = self.config.get("inject_mode", "prompt")
        if mode == "system_prompt":
            base = req.system_prompt or ""
            req.system_prompt = f"{base}\n\n{block}" if base else block
        else:
            base = req.prompt or ""
            req.prompt = f"{base}\n\n{block}" if base else block
