"""Langfuse 观测（SPEC Q24）。没有配置 key 或 ADAGENT_TRACE=0 时完全不记录。

原则：观测不能拖垮主流程。埋点本身出错只记日志；业务代码的异常照常抛出。
trace id 由 thread_id 决定：同一个会话的提问、回答反问、审批落在同一条 trace 里，和审计日志的 trace_id 对得上。
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

from . import db  # noqa: F401  确保 .env 已加载

log = logging.getLogger(__name__)
_client = None
MAX_ROWS = 20   # 节点输出里的查询结果只保留前 20 行


def enabled() -> bool:
    return (os.getenv("ADAGENT_TRACE", "1") != "0"
            and bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(os.getenv("LANGFUSE_SECRET_KEY")))


def client():
    global _client
    if _client is None:
        from langfuse import Langfuse
        _client = Langfuse()   # 读 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL
    return _client


class _Noop:
    def update(self, **_kw):
        return self


NOOP = _Noop()


def safe_update(obs, **kw) -> None:
    try:
        obs.update(**kw)
    except Exception:
        log.warning("tracing: update failed", exc_info=True)


def trace_id_for(thread_id: str) -> str:
    """Langfuse 的 trace id 必须是 32 位小写十六进制。uuid4().hex 直接用；其他（如 eval- 开头）按种子换算。"""
    if re.fullmatch(r"[0-9a-f]{32}", thread_id):
        return thread_id
    return client().create_trace_id(seed=thread_id)


def trace_url(thread_id: str) -> str | None:
    if not enabled():
        return None
    try:
        return client().get_trace_url(trace_id=trace_id_for(thread_id))
    except Exception:
        log.warning("tracing: trace url failed", exc_info=True)
        return None


@contextmanager
def _guarded(cm) -> Iterator[Any]:
    """进入 / 退出观测时出错只记日志，包住的业务代码的异常照常抛出。"""
    try:
        obs = cm.__enter__()
    except Exception:
        log.warning("tracing: start failed", exc_info=True)
        yield NOOP
        return
    try:
        yield obs
    except BaseException as e:
        try:
            cm.__exit__(type(e), e, e.__traceback__)
        except Exception:
            log.warning("tracing: end failed", exc_info=True)
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            log.warning("tracing: end failed", exc_info=True)


@contextmanager
def observation(name: str, as_type: str = "span", **kw) -> Iterator[Any]:
    """当前 trace 下的一个子观测（节点 span 或模型调用 generation）。"""
    if not enabled():
        yield NOOP
        return
    try:
        cm = client().start_as_current_observation(name=name, as_type=as_type, **kw)
    except Exception:
        log.warning("tracing: start failed", exc_info=True)
        yield NOOP
        return
    with _guarded(cm) as obs:
        yield obs


@contextmanager
def request(name: str, thread_id: str, user_id: str, input: Any = None) -> Iterator[Any]:
    """一次对外请求（提问 / 回答反问 / 审批）的根 span，挂在 thread_id 对应的 trace 下。"""
    if not enabled():
        yield NOOP
        return
    try:
        from langfuse import propagate_attributes
        tags = ["eval"] if thread_id.startswith("eval-") else []
        prop = propagate_attributes(user_id=user_id, session_id=thread_id, tags=tags,
                                    metadata={"thread_id": thread_id}, trace_name="agent")
        root = client().start_as_current_observation(
            name=name, as_type="span", input=input, trace_context={"trace_id": trace_id_for(thread_id)})
    except Exception:
        log.warning("tracing: request start failed", exc_info=True)
        yield NOOP
        return
    with _guarded(prop), _guarded(root) as obs:
        yield obs


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            log.warning("tracing: flush failed", exc_info=True)


def shutdown() -> None:
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            log.warning("tracing: shutdown failed", exc_info=True)


def small(v: Any) -> Any:
    """节点输出里的大列表截断，避免把整张查询结果发到 Langfuse。"""
    if isinstance(v, dict):
        return {k: small(x) for k, x in v.items()}
    if isinstance(v, list) and len(v) > MAX_ROWS:
        return [small(x) for x in v[:MAX_ROWS]] + [f"... {len(v) - MAX_ROWS} more"]
    if isinstance(v, list):
        return [small(x) for x in v]
    return v
