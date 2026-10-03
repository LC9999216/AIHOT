// 免费 API 行业的分类体系：类别、标签词表、厂商名录。
// 模型按这里的词表打标签，主题页（topics.json）按标签归类，筛选栏按类别分组。
// 换行业时：类别的 key 会出现在网址里（/all?category=…），上线后就不要再改；标签和名录可以随时增减。

/**
 * 网页上的类别（筛选栏、卡片角标、RSS 分类订阅）。key 是网址和接口里的身份，上线后不要改。
 * section 是日报里的分节标题（几个类别可以共用一节，按这里的顺序排）；guide 告诉模型怎么归类。
 */
export const CATEGORIES = [
  { key: "perpetual", label: "永久免费", section: "永久免费", guide: "长期有效的免费额度，不会过期，随时可薅" },
  { key: "renewing", label: "按量续期", section: "按量续期", guide: "每天/每月自动重置的免费额度，用完等下个周期" },
  { key: "recurring", label: "周期赠送", section: "周期赠送", guide: "定期发放的 credits 或代金券，领完即用" },
  { key: "trial", label: "体验额度", section: "体验额度", guide: "新用户一次性试用额度，用完不再续" },
] as const;

/**
 * 内容理解一步给每篇资料判的“内容类型”（写在 prompts/content-understanding.md 里，改了类型要同步改那份提示词）。
 * 评分提示词（prompts/selection-score.md）按类型给五个维度不同的权重。
 */
export const ITEM_TYPES = ["free_api_listing"] as const;

// ── 标签词表 ────────────────────────────────────────────────────────────────────────────

/** 每篇资料的第一个标签必须是这些“分类标签”之一。 */
export const CATEGORY_TAGS = [
  "永久免费", "按量续期", "周期赠送", "体验额度", "其他",
] as const;

/** 可选的主题标签：免费特征。 */
export const TOPIC_TAGS = [
  "OpenAI兼容", "免绑卡", "商用允许", "新人专享", "无需手机号",
  "文本生成", "图像生成", "音频", "向量/检索", "多模态", "代码",
] as const;

/** 可选的实体标签（厂商、平台）。 */
export const ENTITY_TAGS = ["Google", "DeepSeek", "阿里", "智谱", "月之暗面", "MiniMax", "OpenAI", "GitHub", "Hugging Face", "OpenRouter"] as const;

/** 模型常写的近义词，统一成词表里的写法。 */
export const TAG_SYNONYMS: Readonly<Record<string, string>> = {
  "永久": "永久免费", "长期免费": "永久免费", "不限时": "永久免费",
  "每月重置": "按量续期", "每日重置": "按量续期", "按月续期": "按量续期",
  "赠送额度": "周期赠送", "代金券": "周期赠送", "credits": "周期赠送",
  "试用": "体验额度", "新用户": "体验额度", "trial": "体验额度",
  "openai兼容": "OpenAI兼容", "兼容openai": "OpenAI兼容",
  "不用绑卡": "免绑卡", "无需信用卡": "免绑卡",
  "可商用": "商用允许", "商业可用": "商用允许",
  "文本": "文本生成", "对话": "文本生成", "llm": "文本生成",
  "图片": "图像生成", "生图": "图像生成", "image": "图像生成",
  "语音": "音频", "tts": "音频", "stt": "音频",
  "embedding": "向量/检索", "embeddings": "向量/检索", "rerank": "向量/检索", "重排": "向量/检索",
  "ocr": "多模态", "vision": "多模态", "视觉": "多模态",
};

/** 模型漏了分类标签时，按内容类型补一个。 */
export const CATEGORY_BY_ITEM_TYPE: Readonly<Record<string, string>> = {
  free_api_listing: "其他",
};

// ── 身份识别：把原文里提到的厂商归到名录 ─────────────────────────────────────────────

/** 原文里的这些写法也算提到了对应厂商（身份核验、实体归类用）。 */
export const IDENTITY_LEXICON: ReadonlyArray<{ id: string; name: string; patterns: RegExp[] }> = [
  { id: "google", name: "Google", patterns: [/google|gemini|AI Studio|谷歌/i] },
  { id: "deepseek", name: "DeepSeek", patterns: [/deepseek|深度求索/i] },
  { id: "alibaba", name: "阿里", patterns: [/\bqwen|通义|千问|百炼|阿里/i] },
  { id: "zhipu", name: "智谱", patterns: [/智谱|\bglm|Z\.ai/i] },
  { id: "moonshot", name: "月之暗面", patterns: [/\bkimi\b|月之暗面|moonshot/i] },
  { id: "minimax", name: "MiniMax", patterns: [/minimax|海螺/i] },
  { id: "openai", name: "OpenAI", patterns: [/openai|chatgpt/i] },
  { id: "github", name: "GitHub", patterns: [/github/i] },
  { id: "hugging-face", name: "Hugging Face", patterns: [/hugging\s?face/i] },
  { id: "openrouter", name: "OpenRouter", patterns: [/openrouter/i] },
];

/** 厂商官方域名（来源归属用）。 */
export const PUBLISHER_DOMAINS: ReadonlyArray<{ entityId: string; domains: readonly string[] }> = [
  { entityId: "google", domains: ["ai.google.dev", "deepmind.google", "blog.google"] },
  { entityId: "deepseek", domains: ["deepseek.com"] },
  { entityId: "alibaba", domains: ["aliyun.com", "qwen.ai"] },
  { entityId: "zhipu", domains: ["zhipuai.cn", "z.ai"] },
  { entityId: "moonshot", domains: ["moonshot.cn"] },
  { entityId: "minimax", domains: ["minimax.com"] },
  { entityId: "openai", domains: ["openai.com"] },
  { entityId: "github", domains: ["github.com"] },
  { entityId: "hugging-face", domains: ["huggingface.co"] },
  { entityId: "openrouter", domains: ["openrouter.ai"] },
];

/** 原文里的这些写法也算提到了对应厂商。 */
export const IDENTITY_CONTEXT_ALIASES: ReadonlyArray<{ entityId: string; pattern: RegExp }> = [
  { entityId: "zhipu", pattern: /\bZhipu(?:\s+AI\b|['’]s\b)/i },
  { entityId: "alibaba", pattern: /通义千问|阿里百炼/i },
];

/** 厂商主题：id → 显示名、卡片上显示的标签（null 表示只用 entity:<id> 归类）、别名。 */
export const ENTITIES: Record<string, { name: string; displayTag: string | null; aliases: string[] }> = {
  google: { name: "Google", displayTag: "Google", aliases: ["Google", "Gemini", "AI Studio", "谷歌"] },
  deepseek: { name: "DeepSeek", displayTag: "DeepSeek", aliases: ["DeepSeek", "深度求索"] },
  alibaba: { name: "阿里", displayTag: "阿里", aliases: ["阿里", "千问", "Qwen", "通义", "百炼"] },
  zhipu: { name: "智谱", displayTag: "智谱", aliases: ["智谱", "GLM", "Z.ai", "智谱AI"] },
  moonshot: { name: "月之暗面", displayTag: null, aliases: ["月之暗面", "Kimi", "Moonshot"] },
  minimax: { name: "MiniMax", displayTag: null, aliases: ["MiniMax", "海螺"] },
  openai: { name: "OpenAI", displayTag: "OpenAI", aliases: ["OpenAI", "ChatGPT"] },
  github: { name: "GitHub", displayTag: "GitHub", aliases: ["GitHub", "Models"] },
  "hugging-face": { name: "Hugging Face", displayTag: "Hugging Face", aliases: ["Hugging Face", "HF"] },
  openrouter: { name: "OpenRouter", displayTag: null, aliases: ["OpenRouter"] },
};
