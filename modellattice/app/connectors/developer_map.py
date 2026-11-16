"""Id-prefix -> developer display name.

Governed data, not a fixed enum (v0.1 Section 3, "metadata-driven
design"): a connector seeing an unmapped prefix falls back to a
title-cased guess rather than failing, and this dict is the place to add
the correct name once noticed. Keys are lowercase, with any OpenRouter
"stealth" `~` prefix already stripped by the caller.
"""

KNOWN_DEVELOPERS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "qwen": "Alibaba (Qwen)",
    "mistralai": "Mistral AI",
    "z-ai": "Zhipu AI",
    "deepseek": "DeepSeek",
    "nvidia": "NVIDIA",
    "meta": "Meta",
    "meta-llama": "Meta",
    "moonshotai": "Moonshot AI",
    "x-ai": "xAI",
    "tencent": "Tencent",
    "openrouter": "OpenRouter",
    "aion-labs": "AionLabs",
    "cohere": "Cohere",
    "inclusionai": "InclusionAI",
    "thinkingmachines": "Thinking Machines",
    "minimax": "MiniMax",
    "xiaomi": "Xiaomi",
    "amazon": "Amazon",
    "perplexity": "Perplexity",
    "sakana": "Sakana AI",
    "morph": "Morph",
    "liquid": "Liquid AI",
    "bytedance-seed": "ByteDance",
    "baidu": "Baidu",
    "upstage": "Upstage",
    "rekaai": "Reka AI",
    "fireworks": "Fireworks AI",
    "ibm-granite": "IBM",
    "meituan": "Meituan",
    "writer": "Writer",
}


def resolve_developer_name(raw_prefix: str) -> str:
    key = raw_prefix.lstrip("~").lower()
    if key in KNOWN_DEVELOPERS:
        return KNOWN_DEVELOPERS[key]
    # Fallback: "some-vendor" -> "Some Vendor". Good enough to display and
    # to key a Provider row on; correct it in KNOWN_DEVELOPERS once seen.
    return " ".join(part.capitalize() for part in key.replace("_", "-").split("-"))
