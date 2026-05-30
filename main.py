from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig
import asyncio
import re
import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, List, Any

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
    "为非多模态 LLM 桥接视觉理解：收到图片时把完整请求交给视觉模型，再把其回答注入上下文交给主 LLM 产出最终回复。增强版支持OCR、网络搜索、知识库。",
    "v3.0.0",
    "https://github.com/jiangnan-awa/astrbot_plugin_vision_bridge",
)
class VisionBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path("data/plugin_data/astrbot_plugin_vision_bridge")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 延迟初始化重量级组件
        self._ocr_engine = None
        self._knowledge_base = None
        self._clip_model = None
        self._search_engine = None
        self._game_api = None

        # 用户纠错记录（会话级别）
        self.correction_log = {}

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
            # 检测用户纠错
            await self._detect_correction(event, req)
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

        # 3) P1: 先查询知识库（如果启用）
        if self.config.get("enable_knowledge_base", True):
            kb_result = await self._query_knowledge_base(image_urls)
            if kb_result and kb_result.get("confidence", 0) >= self.config.get("kb_similarity_threshold", 0.85):
                logger.info(f"[VisionBridge] 知识库命中（相似度 {kb_result['confidence']:.2f}），跳过视觉模型调用")
                req.image_urls = []
                self._inject(req, kb_result["content"])
                return

        # 4) P0: OCR 文本预提取
        ocr_text = ""
        if self.config.get("enable_ocr", True):
            ocr_text = await self._extract_ocr_text(image_urls)
            if ocr_text:
                logger.info(f"[VisionBridge] OCR 提取到文字：{ocr_text[:100]}...")

        # 5) 提取上下文线索
        context_clues = self._extract_context_clues(req.prompt, req.contexts)

        # 6) 把完整请求交给视觉模型，得到有针对性的理解/回答
        understanding = await self._understand(vision_provider, req, image_urls, ocr_text, context_clues)

        # 7) 评估置信度
        confidence_info = self._evaluate_confidence(understanding, ocr_text, context_clues)

        # 8) P1: 如果置信度低，触发网络搜索验证
        enhanced_understanding = understanding
        if self.config.get("enable_web_search", True) and confidence_info["confidence"] < self.config.get("confidence_threshold", 0.7):
            search_results = await self._search_and_verify(understanding, ocr_text, context_clues)
            if search_results:
                enhanced_understanding = self._merge_search_results(understanding, search_results, confidence_info)
                logger.info(f"[VisionBridge] 网络搜索增强完成，找到 {len(search_results)} 条相关信息")

        # 9) P1: 存入知识库（如果启用）
        if self.config.get("enable_knowledge_base", True):
            await self._save_to_knowledge_base(image_urls, enhanced_understanding, confidence_info, ocr_text)

        # 10) 清空图片（关键：主 LLM 不支持图片），注入理解结果，放行给主 LLM
        req.image_urls = []
        if enhanced_understanding:
            self._inject(req, enhanced_understanding)
            logger.info(
                f"[VisionBridge] 已将视觉理解（{len(enhanced_understanding)} 字，置信度 {confidence_info['confidence']:.2f}）注入上下文"
            )
        else:
            logger.warning("[VisionBridge] 视觉模型未返回有效内容，本轮按无图继续。")

    async def _extract_ocr_text(self, image_urls: List[str]) -> str:
        """P0: OCR 文本提取"""
        try:
            if self._ocr_engine is None:
                from paddleocr import PaddleOCR
                self._ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

            all_text = []
            for url in image_urls[:3]:  # 最多处理前3张图片
                try:
                    # 下载图片
                    import requests
                    from PIL import Image
                    from io import BytesIO

                    response = requests.get(url, timeout=10)
                    img = Image.open(BytesIO(response.content))

                    # OCR 识别
                    result = self._ocr_engine.ocr(img, cls=True)

                    if result and result[0]:
                        texts = [line[1][0] for line in result[0] if line[1][1] > 0.7]  # 置信度 > 0.7
                        all_text.extend(texts)
                except Exception as e:
                    logger.warning(f"[VisionBridge] OCR 处理图片失败 {url}: {e}")
                    continue

            return " ".join(all_text) if all_text else ""
        except ImportError:
            logger.warning("[VisionBridge] PaddleOCR 未安装，跳过 OCR 提取。请运行: pip install paddleocr paddlepaddle")
            return ""
        except Exception as e:
            logger.error(f"[VisionBridge] OCR 提取失败: {e}")
            return ""

    def _extract_context_clues(self, prompt: str, contexts: List) -> Dict[str, Any]:
        """提取上下文线索"""
        clues = {
            "temporal_keywords": [],
            "category_keywords": [],
            "popularity_keywords": [],
            "has_new_content_signal": False
        }

        if not prompt:
            return clues

        # 时间线索
        temporal_patterns = [
            r"(新出|最近|刚发布|今年|2024|2025|2026)",
            r"(new|recent|latest|just released)"
        ]
        for pattern in temporal_patterns:
            matches = re.findall(pattern, prompt, re.IGNORECASE)
            if matches:
                clues["temporal_keywords"].extend(matches)
                clues["has_new_content_signal"] = True

        # 类别线索
        category_patterns = [
            r"(游戏|角色|APP|应用|软件|品牌)",
            r"(game|character|app|application|brand)"
        ]
        for pattern in category_patterns:
            matches = re.findall(pattern, prompt, re.IGNORECASE)
            if matches:
                clues["category_keywords"].extend(matches)

        # 热度线索
        popularity_patterns = [
            r"(很火|热门|流行|爆款)",
            r"(popular|trending|viral|hot)"
        ]
        for pattern in popularity_patterns:
            matches = re.findall(pattern, prompt, re.IGNORECASE)
            if matches:
                clues["popularity_keywords"].extend(matches)

        return clues

    def _evaluate_confidence(self, understanding: str, ocr_text: str, context_clues: Dict) -> Dict[str, Any]:
        """P0: 评估置信度"""
        confidence = 0.8  # 默认中等置信度
        reasons = []

        # 检测不确定性表述
        uncertainty_patterns = [
            r"(可能|疑似|看起来像|不确定|无法确定)",
            r"(might|possibly|seems like|uncertain|not sure)"
        ]
        for pattern in uncertainty_patterns:
            if re.search(pattern, understanding, re.IGNORECASE):
                confidence -= 0.2
                reasons.append("包含不确定性表述")
                break

        # OCR 文字匹配度
        if ocr_text:
            # 如果理解结果中包含 OCR 提取的文字，置信度提升
            ocr_words = set(ocr_text.split())
            understanding_words = set(understanding.split())
            overlap = len(ocr_words & understanding_words) / max(len(ocr_words), 1)
            if overlap > 0.5:
                confidence += 0.1
                reasons.append("OCR文字匹配度高")

        # 时间线索（新内容信号）
        if context_clues.get("has_new_content_signal"):
            confidence -= 0.15
            reasons.append("用户提到新内容，可能超出模型知识范围")

        # 限制范围
        confidence = max(0.1, min(1.0, confidence))

        return {
            "confidence": confidence,
            "reasons": reasons,
            "needs_verification": confidence < 0.7
        }

    async def _search_and_verify(self, understanding: str, ocr_text: str, context_clues: Dict) -> List[Dict]:
        """P1: 网络搜索验证"""
        try:
            if self._search_engine is None:
                from duckduckgo_search import DDGS
                self._search_engine = DDGS()

            # 提取搜索关键词
            keywords = self._extract_search_keywords(understanding, ocr_text, context_clues)
            if not keywords:
                return []

            # 构造搜索查询
            query = " ".join(keywords[:3])  # 最多使用前3个关键词
            if context_clues.get("category_keywords"):
                query += f" {context_clues['category_keywords'][0]}"

            # 添加时间限制（如果有新内容信号）
            if context_clues.get("has_new_content_signal"):
                query += " 2024 2025 2026"

            logger.info(f"[VisionBridge] 搜索查询: {query}")

            # 执行搜索
            max_results = self.config.get("max_search_results", 3)
            results = []

            search_results = self._search_engine.text(query, max_results=max_results)
            for r in search_results:
                results.append({
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url": r.get("href", "")
                })

            # P1: 如果启用游戏API，尝试专业数据源
            if self.config.get("enable_game_api", True) and any(k in query.lower() for k in ["游戏", "game"]):
                game_info = await self._query_game_api(keywords)
                if game_info:
                    results.insert(0, game_info)

            return results
        except ImportError:
            logger.warning("[VisionBridge] duckduckgo-search 未安装，跳过网络搜索。请运行: pip install duckduckgo-search")
            return []
        except Exception as e:
            logger.error(f"[VisionBridge] 网络搜索失败: {e}")
            return []

    def _extract_search_keywords(self, understanding: str, ocr_text: str, context_clues: Dict) -> List[str]:
        """提取搜索关键词"""
        keywords = []

        # 优先使用 OCR 文字
        if ocr_text:
            # 提取可能的名称（大写开头、引号包裹等）
            ocr_keywords = re.findall(r'[《「『]([^》」』]+)[》」』]', ocr_text)
            keywords.extend(ocr_keywords)

            # 提取英文名称（连续大写字母）
            english_names = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', ocr_text)
            keywords.extend(english_names[:2])

        # 从理解结果中提取
        understanding_keywords = re.findall(r'[《「『]([^》」』]+)[》」』]', understanding)
        keywords.extend(understanding_keywords)

        # 去重并限制数量
        return list(dict.fromkeys(keywords))[:5]

    async def _query_game_api(self, keywords: List[str]) -> Optional[Dict]:
        """P1: 查询游戏数据库 API"""
        try:
            import aiohttp

            if not keywords:
                return None

            # 使用 RAWG API（免费，无需密钥）
            query = keywords[0]
            url = f"https://api.rawg.io/api/games?search={query}&page_size=1"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("results"):
                            game = data["results"][0]
                            return {
                                "title": f"游戏数据库: {game.get('name', '')}",
                                "snippet": f"发布日期: {game.get('released', '未知')}，评分: {game.get('rating', 'N/A')}/5，平台: {', '.join([p['platform']['name'] for p in game.get('platforms', [])[:3]])}",
                                "url": f"https://rawg.io/games/{game.get('slug', '')}"
                            }
            return None
        except Exception as e:
            logger.debug(f"[VisionBridge] 游戏API查询失败: {e}")
            return None

    def _merge_search_results(self, understanding: str, search_results: List[Dict], confidence_info: Dict) -> str:
        """合并搜索结果到理解内容"""
        if not search_results:
            return understanding

        # 构建外部验证块
        verification_block = "\n\n[外部验证信息]\n"
        verification_block += f"由于识别置信度较低（{confidence_info['confidence']:.0%}），已通过网络搜索验证：\n\n"

        for i, result in enumerate(search_results[:3], 1):
            verification_block += f"{i}. {result['title']}\n"
            verification_block += f"   {result['snippet'][:150]}...\n"
            if result.get('url'):
                verification_block += f"   来源: {result['url']}\n"
            verification_block += "\n"

        verification_block += "请综合以上信息回答用户问题。"

        return understanding + verification_block

    async def _query_knowledge_base(self, image_urls: List[str]) -> Optional[Dict]:
        """P1: 查询本地知识库"""
        try:
            if self._knowledge_base is None:
                await self._init_knowledge_base()

            if self._knowledge_base is None:
                return None

            # 计算图片特征
            image_hash = self._compute_image_hash(image_urls[0])

            # 查询知识库
            results = self._knowledge_base.query(
                query_texts=[image_hash],
                n_results=1
            )

            if results and results["ids"] and results["ids"][0]:
                metadata = results["metadatas"][0][0]
                distance = results["distances"][0][0] if results.get("distances") else 1.0
                similarity = 1.0 - distance

                if similarity >= self.config.get("kb_similarity_threshold", 0.85):
                    return {
                        "content": metadata.get("content", ""),
                        "confidence": similarity,
                        "entity_name": metadata.get("entity_name", ""),
                        "verified": metadata.get("user_confirmed", False)
                    }

            return None
        except Exception as e:
            logger.debug(f"[VisionBridge] 知识库查询失败: {e}")
            return None

    async def _save_to_knowledge_base(self, image_urls: List[str], content: str, confidence_info: Dict, ocr_text: str):
        """P1: 保存到知识库"""
        try:
            if self._knowledge_base is None:
                await self._init_knowledge_base()

            if self._knowledge_base is None:
                return

            image_hash = self._compute_image_hash(image_urls[0])

            # 保存到知识库
            self._knowledge_base.add(
                ids=[image_hash],
                documents=[content],
                metadatas=[{
                    "content": content,
                    "ocr_text": ocr_text,
                    "confidence": confidence_info["confidence"],
                    "user_confirmed": False,
                    "timestamp": str(asyncio.get_event_loop().time())
                }]
            )

            logger.debug(f"[VisionBridge] 已保存到知识库: {image_hash}")
        except Exception as e:
            logger.debug(f"[VisionBridge] 保存到知识库失败: {e}")

    async def _init_knowledge_base(self):
        """初始化知识库"""
        try:
            import chromadb

            kb_path = self.data_dir / "knowledge_base"
            kb_path.mkdir(exist_ok=True)

            client = chromadb.PersistentClient(path=str(kb_path))
            self._knowledge_base = client.get_or_create_collection(
                name="vision_knowledge",
                metadata={"hnsw:space": "cosine"}
            )

            logger.info("[VisionBridge] 知识库初始化成功")
        except ImportError:
            logger.warning("[VisionBridge] ChromaDB 未安装，知识库功能不可用。请运行: pip install chromadb")
            self._knowledge_base = None
        except Exception as e:
            logger.error(f"[VisionBridge] 知识库初始化失败: {e}")
            self._knowledge_base = None

    def _compute_image_hash(self, image_url: str) -> str:
        """计算图片哈希"""
        return hashlib.md5(image_url.encode()).hexdigest()

    async def _detect_correction(self, event: AstrMessageEvent, req: ProviderRequest):
        """检测用户纠错"""
        if not req.prompt:
            return

        # 检测纠错模式
        correction_patterns = [
            r"不是.*是(.+)",
            r"错了.*应该是(.+)",
            r"这是(.+)，不是",
            r"/vision_correct\s+(.+)"
        ]

        for pattern in correction_patterns:
            match = re.search(pattern, req.prompt, re.IGNORECASE)
            if match:
                corrected_name = match.group(1).strip()
                logger.info(f"[VisionBridge] 检测到用户纠错: {corrected_name}")

                # 记录纠错（简化版，实际应该关联到具体图片）
                self.correction_log[corrected_name] = {
                    "timestamp": asyncio.get_event_loop().time(),
                    "user_id": getattr(event, "user_id", "unknown")
                }

                # TODO: 更新知识库中的对应条目
                break

    async def _understand(self, vision_provider, req: ProviderRequest, image_urls, ocr_text: str, context_clues: Dict):
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

        # P0: 增强提示词 - 注入 OCR 结果和上下文线索
        enhanced_instruction = instruction

        if ocr_text:
            enhanced_instruction += f"\n\n[OCR文本提取]\n图片中检测到以下文字内容：\n{ocr_text}\n\n请优先使用OCR提取的准确文本而非视觉推测。"

        if context_clues.get("has_new_content_signal"):
            enhanced_instruction += "\n\n[重要提示]\n用户提到了时间相关的关键词（如'新'、'最近'等），这可能是2023年10月后发布的新内容。如果你无法确定具体名称，请明确说明'这可能是我训练数据外的新内容'，并描述视觉特征而非臆测名称。"

        enhanced_instruction += "\n\n请在回答中包含你的置信度评估（高/中/低），并说明判断依据。"

        # 组装给视觉模型的 system_prompt
        if self.config.get("forward_persona", True) and (req.system_prompt or "").strip():
            vision_system = f"{req.system_prompt.strip()}\n\n{enhanced_instruction}"
        else:
            vision_system = enhanced_instruction

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
