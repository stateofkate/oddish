from __future__ import annotations

from oddish.workers.agents.mini_swe_agent import openrouter_billed_cost


def _raw(*costs):
    msgs = [{"role": "user", "content": "task"}]
    for cost in costs:
        usage = {"prompt_tokens": 10, "completion_tokens": 1}
        if cost is not None:
            usage["cost"] = cost
        msgs.append({"role": "assistant", "extra": {"response": {"usage": usage}}})
    return {"info": {"model_stats": {"instance_cost": 9.99}}, "messages": msgs}


def test_sums_openrouter_reported_cost():
    assert openrouter_billed_cost("openrouter/z-ai/glm-5.3", _raw(0.25, 0.5)) == 0.75


def test_keeps_instance_cost_when_any_response_lacks_cost():
    assert openrouter_billed_cost("openrouter/z-ai/glm-5.3", _raw(0.25, None)) is None


def test_ignores_non_openrouter_models():
    assert openrouter_billed_cost("zai/glm-5.3", _raw(0.25)) is None
