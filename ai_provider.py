"""
AI Provider Abstraction

WHAT: Unified interface để gọi nhiều AI provider khác nhau
WHY:  app.py không cần biết đang dùng Anthropic hay Gemini hay OpenAI hay GreenNode
HOW:  Mỗi provider implement cùng interface: call(messages, system) → (text, input_tok, output_tok, cost)
"""

import os
from typing import List, Dict, Tuple
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Provider config — đọc từ .env
# ---------------------------------------------------------------------------

PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower().strip()

# Model mặc định theo provider (override bằng AI_MODEL trong .env)
_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "gemini":    "gemini-2.5-flash",
    "openai":    "gpt-4o",
    "qwen":      "qwen/qwen3-5-27b",       # GreenNode MAAS path
    "gemma":     "google/gemma-4-31b-it",  # GreenNode MAAS path
    "minimax":   "minimax/minimax-m2.5",   # GreenNode MAAS path
    "cursor":    "claude-sonnet-4-5",
}

MODEL      = os.getenv("AI_MODEL", _DEFAULT_MODELS.get(PROVIDER, ""))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))

# Pricing per 1M tokens (input, output)
# GreenNode: 1 credit = 1 VND — giá lấy từ aiplatform.console.vngcloud.vn
_PRICING = {
    "anthropic": (3.0,    15.0),   # USD/1M — claude-sonnet-4-6
    "gemini":    (1.25,    5.0),   # USD/1M — gemini-2.5-flash
    "openai":    (5.0,    15.0),   # USD/1M — gpt-4o
    "qwen":      (11521,  92165),  # VND/1M — Qwen 3.5 27B trên GreenNode MAAS
    "gemma":     (0.0,     0.0),   # VND/1M — Gemma 4 31B-IT (TBA)
    "minimax":   (0.0,     0.0),   # VND/1M — MiniMax M2.5 (TBA)
    "cursor":    (0.0,     0.0),   # via Cursor API
}


@dataclass
class AIResponse:
    text:         str
    input_tokens: int
    output_tokens: int
    cost:         float
    provider:     str
    model:        str


def calc_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = _PRICING.get(provider, (0, 0))
    return (input_tokens / 1e6) * price_in + (output_tokens / 1e6) * price_out


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _call_anthropic(messages: List[Dict], system: str) -> AIResponse:
    import anthropic as _anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY chưa được set trong .env")

    client   = _anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL or "claude-sonnet-4-6",
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
    )
    text    = response.content[0].text
    inp_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    return AIResponse(
        text=text, input_tokens=inp_tok, output_tokens=out_tok,
        cost=calc_cost("anthropic", inp_tok, out_tok),
        provider="anthropic", model=MODEL or "claude-sonnet-4-6",
    )


def _call_gemini(messages: List[Dict], system: str) -> AIResponse:
    try:
        from google import genai
        from google.genai import types
        from google.genai.errors import ServerError
    except ImportError:
        raise ImportError("Cần cài: pip install google-genai")

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY chưa được set trong .env")

    client = genai.Client(api_key=api_key)

    # Fallback chain khi model bị overloaded (503)
    _FALLBACK_MODELS = [
        MODEL or "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash-lite",
    ]

    # Convert messages: "assistant" → "model" cho Gemini
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=MAX_TOKENS,
    )

    last_error = None
    model_name = _FALLBACK_MODELS[0]
    for model_name in _FALLBACK_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            break
        except ServerError as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                last_error = e
                continue
            raise
    else:
        raise last_error

    text    = response.text or ""
    inp_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
    out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
    return AIResponse(
        text=text, input_tokens=inp_tok, output_tokens=out_tok,
        cost=calc_cost("gemini", inp_tok, out_tok),
        provider="gemini", model=model_name,
    )


def _call_openai(messages: List[Dict], system: str) -> AIResponse:
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Cần cài: pip install openai")

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY chưa được set trong .env")

    client     = OpenAI(api_key=api_key)
    model_name = MODEL or "gpt-4o"

    # OpenAI nhận system message riêng trong messages list
    full_messages = [{"role": "system", "content": system}] + messages
    response = client.chat.completions.create(
        model=model_name,
        max_tokens=MAX_TOKENS,
        messages=full_messages,
    )
    text    = response.choices[0].message.content
    inp_tok = response.usage.prompt_tokens
    out_tok = response.usage.completion_tokens
    return AIResponse(
        text=text, input_tokens=inp_tok, output_tokens=out_tok,
        cost=calc_cost("openai", inp_tok, out_tok),
        provider="openai", model=model_name,
    )


def _call_greennode(messages: List[Dict], system: str) -> AIResponse:
    """
    GreenNode AgentBase — OpenAI-compatible API.
    Dùng chung cho Qwen, Gemma, MiniMax và bất kỳ model nào GreenNode host.
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Cần cài: pip install openai")

    api_key  = os.getenv("GREENODE_API_KEY", "")
    base_url = os.getenv("GREENNODE_BASE_URL", "https://api.greennode.ai/v1")
    if not api_key:
        raise ValueError("GREENODE_API_KEY chưa được set trong .env")

    client     = OpenAI(api_key=api_key, base_url=base_url)
    model_name = MODEL or _DEFAULT_MODELS.get(PROVIDER, "")
    if not model_name:
        raise ValueError(f"AI_MODEL chưa được set cho provider '{PROVIDER}'")

    full_messages = [{"role": "system", "content": system}] + messages
    response = client.chat.completions.create(
        model=model_name,
        max_tokens=MAX_TOKENS,
        messages=full_messages,
    )
    text    = response.choices[0].message.content
    inp_tok = response.usage.prompt_tokens if response.usage else 0
    out_tok = response.usage.completion_tokens if response.usage else 0
    return AIResponse(
        text=text, input_tokens=inp_tok, output_tokens=out_tok,
        cost=0.0,   # self-hosted — no token cost
        provider=PROVIDER, model=model_name,
    )


# ---------------------------------------------------------------------------
# Public interface — app.py chỉ cần gọi hàm này
# ---------------------------------------------------------------------------

def _call_cursor(messages: List[Dict], system: str) -> AIResponse:
    """Cursor API — OpenAI-compatible."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Cần cài: pip install openai")

    api_key    = os.getenv("CURSOR_API_KEY", "")
    base_url   = os.getenv("CURSOR_BASE_URL", "https://api.cursor.sh/v1")
    if not api_key:
        raise ValueError("CURSOR_API_KEY chưa được set trong .env")

    client     = OpenAI(api_key=api_key, base_url=base_url)
    model_name = MODEL or _DEFAULT_MODELS.get("cursor", "claude-sonnet-4-5")

    full_messages = [{"role": "system", "content": system}] + messages
    response = client.chat.completions.create(
        model=model_name,
        max_tokens=MAX_TOKENS,
        messages=full_messages,
    )
    text    = response.choices[0].message.content or ""
    inp_tok = response.usage.prompt_tokens if response.usage else 0
    out_tok = response.usage.completion_tokens if response.usage else 0
    return AIResponse(
        text=text, input_tokens=inp_tok, output_tokens=out_tok,
        cost=0.0,
        provider="cursor", model=model_name,
    )


_PROVIDERS = {
    "anthropic": _call_anthropic,
    "gemini":    _call_gemini,
    "openai":    _call_openai,
    "qwen":      _call_greennode,
    "gemma":     _call_greennode,
    "minimax":   _call_greennode,
    "cursor":    _call_cursor,
}

# ---------------------------------------------------------------------------
# Smart Routing — chọn model tier theo độ phức tạp CAP + input length
# Chỉ áp dụng với Anthropic (haiku / sonnet). Các provider khác dùng model mặc định.
# Tắt bằng SMART_ROUTING=false trong .env
# ---------------------------------------------------------------------------

SMART_ROUTING = os.getenv("SMART_ROUTING", "true").lower() == "true"

# Tier models cho Anthropic
_ANTHROPIC_TIERS = {
    "simple":  "claude-haiku-4-5-20251001",   # ~12× rẻ hơn sonnet
    "medium":  "claude-sonnet-4-6",            # default
}

# CAP base complexity: negative = simple, positive = complex
_CAP_COMPLEXITY = {
    "CAP-1": 0,   # Read Req — phụ thuộc text length
    "CAP-2": 1,   # Risk — cần reasoning
    "CAP-3": 2,   # Test Plan — output dài, cần structure
    "CAP-4": 1,   # Test Cases — medium
    "CAP-5": 0,   # API Test — local, không qua đây
    "CAP-6": 2,   # Synthesis — phân tích diff phức tạp
    "CAP-7": -1,  # Bug Report — template-like, short
    "CAP-8": 0,   # Coverage — medium
    "CAP-9": 0,   # Export — local, không qua đây
    "CHAT":  0,   # General chat
}


def select_model_tier(cap: str = "CHAT", text_len: int = 0) -> str:
    """
    Tính complexity score → chọn tier (simple/medium).
    Returns model name nếu provider là anthropic, None nếu provider khác.
    """
    if not SMART_ROUTING or PROVIDER != "anthropic":
        return None  # dùng model mặc định

    # Nếu user đã set AI_MODEL explicit → không override
    if os.getenv("AI_MODEL", ""):
        return None

    score = _CAP_COMPLEXITY.get(cap, 0)
    if text_len < 300:
        score -= 1   # input ngắn → đơn giản hơn
    elif text_len > 1500:
        score += 1   # input dài → phức tạp hơn

    tier = "simple" if score <= -1 else "medium"
    return _ANTHROPIC_TIERS[tier]


def call_ai(messages: List[Dict], system: str, cap: str = "CHAT") -> AIResponse:
    """
    Gọi AI provider hiện tại. Smart Routing tự chọn model tier nếu provider = anthropic.
    cap: CAP-1..CAP-9 hoặc CHAT — dùng để tính complexity score.
    """
    fn = _PROVIDERS.get(PROVIDER)
    if not fn:
        supported = ", ".join(_PROVIDERS.keys())
        raise ValueError(f"AI_PROVIDER='{PROVIDER}' không hợp lệ. Hỗ trợ: {supported}")

    # Smart Routing: tạm override global MODEL nếu chọn được tier
    text_len = sum(len(m.get("content", "")) for m in messages)
    routed_model = select_model_tier(cap, text_len)
    if routed_model:
        global MODEL
        _original_model = MODEL
        MODEL = routed_model
        try:
            resp = fn(messages, system)
        finally:
            MODEL = _original_model  # khôi phục
        resp.model = routed_model  # đảm bảo log đúng model
        return resp

    return fn(messages, system)


def get_provider_info() -> Dict:
    """Trả về thông tin provider hiện tại để hiện trên UI."""
    return {
        "provider": PROVIDER,
        "model":    MODEL or _DEFAULT_MODELS.get(PROVIDER, "unknown"),
        "supported": list(_PROVIDERS.keys()),
    }
