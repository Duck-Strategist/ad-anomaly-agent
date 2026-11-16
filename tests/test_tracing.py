"""观测（SPEC Q24）：埋点出错不影响业务；interrupt 不记成错误；业务异常照常抛出。不连 Langfuse。"""

from contextlib import contextmanager

import pytest
from langgraph.errors import GraphInterrupt

from adagent import tracing
from adagent.workflow import _traced


class Recorder:
    """假的 Langfuse 客户端：记录每个观测的 update 和退出时的异常。"""

    def __init__(self, fail_on_start=False, fail_on_update=False):
        self.events, self.fail_on_start, self.fail_on_update = [], fail_on_start, fail_on_update

    def start_as_current_observation(self, *, name, **kw):
        if self.fail_on_start:
            raise RuntimeError("langfuse down")
        rec = self

        class Obs:
            def update(self, **u):
                if rec.fail_on_update:
                    raise RuntimeError("update failed")
                rec.events.append((name, "update", u))

        @contextmanager
        def cm():
            try:
                yield Obs()
            except BaseException as e:
                rec.events.append((name, "error", type(e).__name__))
                raise
            rec.events.append((name, "end", None))
        return cm()


@pytest.fixture
def fake(monkeypatch):
    def use(**kw):
        r = Recorder(**kw)
        monkeypatch.setattr(tracing, "enabled", lambda: True)
        monkeypatch.setattr(tracing, "_client", r)
        return r
    return use


def test_node_output_is_recorded(fake):
    r = fake()
    out = _traced("route", lambda s: {"plan": {"intent": "query"}})({"question": "q"})
    assert out == {"plan": {"intent": "query"}}
    assert ("route", "update", {"output": {"plan": {"intent": "query"}}}) in r.events
    assert ("route", "end", None) in r.events


def test_interrupt_is_not_an_error(fake):
    r = fake()

    def approve(_s):
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):          # 仍然交给 LangGraph 处理
        _traced("approve", approve)({"proposal": {"ad_group_id": "US-AG07"}})
    assert ("approve", "end", None) in r.events  # 观测正常结束，没有记成错误
    assert not [e for e in r.events if e[1] == "error"]


def test_business_error_still_raises_and_is_recorded(fake):
    r = fake()

    def boom(_s):
        raise ValueError("bad")

    with pytest.raises(ValueError):
        _traced("query", boom)({})
    assert ("query", "error", "ValueError") in r.events


@pytest.mark.parametrize("kw", [{"fail_on_start": True}, {"fail_on_update": True}])
def test_tracing_failure_does_not_break_the_node(fake, kw):
    fake(**kw)
    assert _traced("route", lambda s: {"plan": {}})({"question": "q"}) == {"plan": {}}


def test_small_truncates_rows():
    v = tracing.small({"rows": list(range(25))})
    assert v["rows"][:20] == list(range(20)) and v["rows"][20] == "... 5 more"


def test_trace_id_for_thread_ids(monkeypatch):
    assert tracing.trace_id_for("0" * 32) == "0" * 32       # uuid4().hex 直接用
    monkeypatch.setattr(tracing, "_client", type("C", (), {"create_trace_id": lambda self, seed: "x" + seed})())
    assert tracing.trace_id_for("eval-AT01-1") == "xeval-AT01-1"
