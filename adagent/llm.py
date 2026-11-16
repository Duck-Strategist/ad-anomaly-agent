"""LLM 调用（DeepSeek / 千问都走 OpenAI 兼容接口）。

只做最薄的一层封装：发消息、要 JSON、记录 token 用量和耗时。不用 LangChain 的链，
因为这里需要的只是"一次调用 + 结构化输出"，自己写更透明，也方便把原始输入输出存进 trace。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from openai import OpenAI

from . import db  # noqa: F401  确保 .env 已加载
from . import tracing

PROVIDERS = {
    "deepseek": ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"),
    "qwen": ("DASHSCOPE_BASE_URL", "DASHSCOPE_API_KEY"),
}

# 逻辑名 → (厂商, 环境变量里的模型名)
MODELS = {
    "deepseek": ("deepseek", "DEEPSEEK_MODEL"),
    "qwen": ("qwen", "QWEN_MODEL"),
    "qwen-small": ("qwen", "QWEN_MODEL_SMALL"),
}


@dataclass
class LLMCall:
    model: str
    messages: list[dict]
    output: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    parsed: dict | None = None
    error: str | None = None


@dataclass
class LLM:
    name: str                      # 逻辑名：deepseek / qwen / qwen-small
    calls: list[LLMCall] = field(default_factory=list)

    def __post_init__(self):
        provider, model_env = MODELS[self.name]
        base_env, key_env = PROVIDERS[provider]
        self.model = os.environ[model_env]
        self.client = OpenAI(base_url=os.environ[base_env], api_key=os.environ[key_env], timeout=60, max_retries=2)

    def chat_json(self, messages: list[dict]) -> LLMCall:
        """要求模型输出 JSON 对象；解析失败不抛异常，记录在 call.error 里交给调用方处理。"""
        with tracing.observation("llm", as_type="generation", model=self.model, input=messages,
                                 model_parameters={"temperature": 0, "response_format": "json_object"}) as gen:
            t0 = time.perf_counter()
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
            call = LLMCall(
                model=self.model, messages=messages, output=text,
                prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
                latency_s=round(time.perf_counter() - t0, 2),
            )
            try:
                call.parsed = json.loads(text)
                if not isinstance(call.parsed, dict):
                    raise ValueError("top-level JSON must be an object")
            except ValueError as e:
                call.error = f"invalid JSON output: {e}"
            tracing.safe_update(gen, output=text, usage_details={"input": call.prompt_tokens, "output": call.completion_tokens},
                                **({"level": "WARNING", "status_message": call.error} if call.error else {}))
        self.calls.append(call)
        return call
