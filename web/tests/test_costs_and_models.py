"""Costs per model id and per analysis, the prices of every kind of token, the auto-or-fixed routing of ChatGPT and
the live model check, with the APIs replaced by fakes."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

from app import claude, costs, db, gpt, main, model_check
from tests.test_app import H, _chatgpt_stylist, add, client  # noqa: F401  (client is a fixture)

REQUEST = httpx.Request("POST", "https://api.example/v1")


def _user(client):
    return client.get("/api/state").json()["user"]


def _luna(cost):
    return costs.Call("openai", "gpt-6-luna", input_tokens=1000, output_tokens=100, cost_usd=cost)


# MARK: - Costs per model id and per analysis

def test_costs_add_up_per_model_id_without_counting_twice(client):
    user = _user(client)
    luna, astra = _luna(0.001), costs.Call("openai", "gpt-6-astra", input_tokens=5000, output_tokens=900, cost_usd=0.2)
    first = costs.store(user, "stylist", [luna, astra])
    costs.store(user, "herkennen", [_luna(0.0005)])
    # The analysis itself: its total and each model's share.
    assert first["cost_usd"] == pytest.approx(0.201)
    assert [(m["model"], m["cost_usd"]) for m in first["per_model"]] == [("gpt-6-astra", 0.2), ("gpt-6-luna", 0.001)]

    u = client.get("/api/state").json()["ai_usage"]
    by = {m["model"]: m for m in u["per_model"]}
    assert set(by) == {"gpt-6-luna", "gpt-6-astra"}  # per id, never "gpt-6-luna, gpt-6-astra"
    assert by["gpt-6-luna"]["cost_usd"] == pytest.approx(0.0015) and by["gpt-6-luna"]["analyses"] == 2
    assert by["gpt-6-astra"]["cost_usd"] == pytest.approx(0.2) and by["gpt-6-astra"]["calls"] == 1
    # The models together are the total, which is the sum of the analyses.
    assert u["cost_usd"] == pytest.approx(sum(m["cost_usd"] for m in u["per_model"]))
    assert u["cost_eur"] == pytest.approx(sum(a["cost_eur"] for a in u["latest"]), abs=1e-5)
    assert u["status"] == "estimated" and not u["partial"]
    stylist = next(a for a in u["latest"] if a["task"] == "stylist")
    assert [m["model"] for m in stylist["per_model"]] == ["gpt-6-astra", "gpt-6-luna"]


def test_two_calls_of_one_model_in_one_analysis_count_once_each(client):
    costs.store(_user(client), "stylist", [_luna(0.001), _luna(0.002)])
    u = client.get("/api/state").json()["ai_usage"]
    assert u["per_model"] == [{**u["per_model"][0], "model": "gpt-6-luna", "analyses": 1, "calls": 2}]
    assert u["per_model"][0]["cost_usd"] == pytest.approx(0.003)


def test_partial_and_unknown_costs_are_marked(client):
    user = _user(client)
    lost = costs.Call("openai", "gpt-6-astra", status="unknown", missing=[costs.TIMED_OUT])
    part = costs.store(user, "stylist", [_luna(0.001), lost])
    assert part["status"] == "partial"
    assert {m["model"]: m["status"] for m in part["per_model"]} == {"gpt-6-luna": "estimated", "gpt-6-astra": "unknown"}
    none = costs.store(user, "herkennen", [costs.from_openai("gpt-onbekend-9", {"input_tokens": 10, "output_tokens": 5})])
    assert none["status"] == "unknown" and none["cost_usd"] == 0
    u = client.get("/api/state").json()["ai_usage"]
    assert u["status"] == "partial" and u["partial"]
    assert {m["model"]: m["status"] for m in u["per_model"]}["gpt-onbekend-9"] == "unknown"


def test_an_old_row_without_its_calls_still_counts(client):
    user = _user(client)
    with db.connect() as conn:
        conn.execute("INSERT INTO ai_usage (id, user_id, at, task, status, models, cost_usd, cost_eur, calls) "
                     "VALUES ('oud', ?, ?, 'stylist', 'estimated', 'gpt-6-luna', 0.01, 0.0087, 'kapot')", (user["id"], db.now_iso()))
    u = client.get("/api/state").json()["ai_usage"]
    assert u["per_model"][0]["model"] == "gpt-6-luna" and u["per_model"][0]["cost_usd"] == pytest.approx(0.01)


def test_an_answer_shows_its_cost_per_model(client, monkeypatch):
    add(client, name="Wit hemd")
    _chatgpt_stylist(client, monkeypatch, ['{"advies": "Ok.", "outfits": []}'])
    r = client.post("/api/stylist", json={"query": "kantoor", "use_weather": False}, headers=H).json()
    # The fake reports no usage: the call is kept, its cost unknown, and the answer says so.
    assert r["analysis"]["status"] == "unknown"
    assert r["analysis"]["per_model"][0]["model"] == gpt.DEFAULT_MODEL


# MARK: - Prices of each kind of token

def test_openai_reads_writes_and_the_rest_of_the_prompt_are_priced_once():
    usage = {"input_tokens": 10_000, "output_tokens": 500,
             "input_tokens_details": {"cached_tokens": 6000, "cache_write_tokens": 3000},
             "output_tokens_details": {"reasoning_tokens": 400}}
    call = costs.from_openai("gpt-6-luna", usage, "default")
    # 1000 plain, 6000 read, 3000 written (30 minutes); the 400 reasoning tokens are part of the 500 output.
    assert call.status == "estimated"
    assert call.cost_usd == pytest.approx((1000 * 0.10 + 6000 * 0.01 + 3000 * 0.125 + 500 * 0.50) / 1e6)


def test_openai_12_hour_cache_writes_have_their_own_price():
    usage = {"input_tokens": 1000, "output_tokens": 0, "input_tokens_details": {
        "cached_tokens": 0, "cache_write_tokens": 1000, "cache_write_tokens_details": {"ttl_30m_tokens": 600, "ttl_12h_tokens": 400}}}
    call = costs.from_openai("gpt-6-astra", usage)
    assert call.cache_write_12h_tokens == 400
    assert call.cost_usd == pytest.approx((600 * 12.5 + 400 * 25.0) / 1e6)
    # A model without a 12-hour price: those writes are left out and the cost is partial.
    costs.PRICES["openai"]["proef-model"] = {"input": 1.0, "cached": 0.1, "cache_write": 1.25, "output": 2.0}
    try:
        assert costs.from_openai("proef-model", usage).status == "partial"
    finally:
        del costs.PRICES["openai"]["proef-model"]


def test_long_prompts_cost_more():
    usage = {"input_tokens": 300_000, "output_tokens": 1000, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
    call = costs.from_openai("gpt-6-luna", usage)
    assert call.cost_usd == pytest.approx((300_000 * 0.10 * 2 + 1000 * 0.50 * 1.5) / 1e6)
    haiku = costs.from_anthropic("claude-haiku-5-5", {"input_tokens": 50_000, "output_tokens": 100, "cache_read_input_tokens": 60_000})
    assert haiku.cost_usd == pytest.approx((50_000 * 0.10 * 5 + 60_000 * 0.01 * 5 + 100 * 0.50 * 5) / 1e6)


def test_missing_usage_and_other_tiers_are_never_guessed():
    assert costs.from_openai("gpt-6-luna", None).status == "unknown"
    assert costs.from_anthropic("claude-sonnet-5-5", {"output_tokens": 3}).status == "unknown"
    flex = costs.from_openai("gpt-6-luna", {"input_tokens": 10, "output_tokens": 1,
                                            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}, "flex")
    assert flex.status == "partial" and "flex" in flex.missing[0]
    picture = costs.from_openai_image("gpt-image-2.5-flare", None)
    assert picture.status == "unknown"


def test_a_refusal_fallback_counts_each_attempt_once():
    usage = {"input_tokens": 0, "output_tokens": 0, "iterations": [
        {"type": "message", "model": "claude-sonnet-5-5", "input_tokens": 100, "output_tokens": 10},
        {"type": "fallback_message", "model": "claude-opus-5-5", "input_tokens": 100, "output_tokens": 50}]}
    calls = costs.anthropic_calls("claude-sonnet-5-5", "claude-opus-5-5", usage)
    assert [c.model for c in calls] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert sum(c.cost_usd for c in calls) == pytest.approx((100 * 2 + 10 * 10 + 100 * 4 + 50 * 20) / 1e6)


def test_a_timed_out_call_is_kept_as_possibly_billed(monkeypatch):
    user = {"id": "u", "is_admin": False}

    class SlowResponses:
        async def create(self, **kwargs):
            raise openai.APITimeoutError(request=REQUEST)
    monkeypatch.setattr(gpt, "client", lambda user: SimpleNamespace(responses=SlowResponses()))

    class SlowMessages:
        async def create(self, **kwargs):
            raise anthropic.APITimeoutError(request=REQUEST)
    monkeypatch.setattr(claude, "client", lambda user: SimpleNamespace(beta=SimpleNamespace(messages=SlowMessages())))

    async def run():
        calls = []
        token = costs.METER.set(calls)
        try:
            with pytest.raises(claude.ClaudeError):
                await gpt.run_until_tool_call(user, {"name": "x", "input_schema": {"type": "object"}}, system="s",
                                              content=[{"type": "text", "text": "q"}], effort="low", helpers={}, name="gpt-6-luna")
            with pytest.raises(claude.ClaudeError):
                await claude._ask_claude(user, {"name": "x", "input_schema": {"type": "object"}}, system="s",
                                         content=[{"type": "text", "text": "q"}], effort="low", helpers={}, name="claude-sonnet-5-5")
        finally:
            costs.METER.reset(token)
        return calls
    calls = asyncio.run(run())
    assert [(c.provider, c.model, c.status) for c in calls] == [("openai", "gpt-6-luna", "unknown"),
                                                                ("anthropic", "claude-sonnet-5-5", "unknown")]
    assert costs.summary(calls)["status"] == "unknown"


# MARK: - Automatic routing or one fixed model

def test_luna_chosen_on_purpose_can_keep_sol_and_astra_out(client, monkeypatch):
    add(client, name="Wit hemd")
    models = _chatgpt_stylist(client, monkeypatch, ['{"advies": "Ok.", "outfits": []}'])
    client.put("/api/settings/model", json={"model": "gpt-6-luna", "provider": "openai"}, headers=H)
    ai = client.get("/api/state").json()["ai"]
    assert ai["routing"] == "auto" and ai["routes"]  # as before: Luna may call in the stronger models
    client.post("/api/stylist", json={"query": "Sollicitatiegesprek", "use_weather": False}, headers=H)

    ai = client.put("/api/settings/routing", json={"routing": "fixed"}, headers=H).json()
    assert ai["routing"] == "fixed" and not ai["routes"]
    client.post("/api/stylist", json={"query": "Sollicitatiegesprek", "use_weather": False}, headers=H)
    assert models == [gpt.COMPLEX_MODEL, "gpt-6-luna"]


def test_a_fixed_luna_also_keeps_the_second_try_on_luna(client, monkeypatch):
    shirt = add(client, name="Wit hemd")
    bad = '{"advies": "Ok.", "outfits": [{"titel": "A", "stukken": ["bestaat-niet"], "waarom": "x"}]}'
    good = '{"advies": "Ok.", "outfits": [{"titel": "A", "stukken": ["%s"], "waarom": "x"}]}' % shirt["id"]
    models = _chatgpt_stylist(client, monkeypatch, [bad, good])
    client.put("/api/settings/routing", json={"routing": "fixed"}, headers=H)
    r = client.post("/api/stylist", json={"query": "Een gewone werkdag", "use_weather": False}, headers=H)
    assert models == [gpt.DEFAULT_MODEL, gpt.DEFAULT_MODEL]  # the check still runs, on Luna
    assert r.json()["outfits"][0]["item_ids"] == [shirt["id"]]


def test_routing_only_applies_to_luna_and_never_with_zuinig(client):
    user = _user(client)
    client.put("/api/settings/provider", json={"provider": "openai"}, headers=H)
    client.put("/api/settings/routing", json={"routing": "auto"}, headers=H)
    assert gpt.model_for(user, "complex") == gpt.COMPLEX_MODEL and gpt.model_for(user, "review") == gpt.REVIEW_MODEL
    assert gpt.model_for(user, "") == gpt.DEFAULT_MODEL
    client.put("/api/settings/quality", json={"quality": "zuinig"}, headers=H)
    assert gpt.model_for(user, "complex") == gpt.DEFAULT_MODEL and not client.get("/api/state").json()["ai"]["routes"]
    client.put("/api/settings/quality", json={"quality": "standaard"}, headers=H)
    client.put("/api/settings/model", json={"model": "gpt-6-astra", "provider": "openai"}, headers=H)
    assert gpt.model_for(user, "complex") == "gpt-6-astra"  # never down from a stronger model to Sol
    assert client.put("/api/settings/routing", json={"routing": "soms"}, headers=H).status_code == 400


# MARK: - Recommended models: only what passed the live check

ALL_PASS = {"checked": "2026-10-25", "image": True, "function": True, "reasoning": True, "cache": True}


def test_only_checked_models_are_recommended(client, monkeypatch):
    monkeypatch.setattr(model_check, "FILE", db.DATA_DIR / "openai_models_test.json")
    model_check.FILE.write_text(json.dumps({"gpt-6-luna": ALL_PASS, "gpt-6-sol": {**ALL_PASS, "reasoning": False}}))

    class FakeModels:
        async def list(self):
            for i, name in enumerate(["gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol", "gpt-image-2.5-flare", "gpt-6-luna-2026-09-01"]):
                yield SimpleNamespace(id=name, created=100 + i)
    monkeypatch.setattr(gpt, "client", lambda user: SimpleNamespace(models=FakeModels()))
    listed = client.get("/api/settings/models?provider=openai").json()
    assert [m["id"] for m in listed] == ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-sol"]
    assert [m["recommended"] for m in listed] == [True, False, False]
    assert listed[2]["failed"] == ["reasoning-instelling"] and listed[1]["checked"] is None
    assert model_check.recommended() == {"gpt-6-luna"}


def _fake_openai(*, reject=(), color="marineblauw", cache_fields=True, cached=1500):
    """A Responses API that answers as a model would, and rejects what is in `reject` like OpenAI does (400)."""
    sent = []

    class Responses:
        async def create(self, **kwargs):
            sent.append(kwargs)
            for part, word in (("reasoning", "reasoning"), ("prompt_cache_options", "prompt_cache_options")):
                if part in kwargs and part in reject:
                    raise openai.BadRequestError(f"Unsupported parameter: '{word}'.", response=httpx.Response(400, request=REQUEST), body=None)
            images = any(p.get("type") == "input_image" for m in kwargs["input"] for p in m["content"])
            if images and "image" in reject:
                raise openai.BadRequestError("This model does not support image inputs.", response=httpx.Response(400, request=REQUEST), body=None)
            first = len(sent) == 1 or images
            details = {"cached_tokens": 0 if first else cached, "cache_write_tokens": 2600 if first else 0} if cache_fields else {}
            usage = {"input_tokens": 2700, "output_tokens": 30, "input_tokens_details": details}
            call = SimpleNamespace(type="function_call", name="geef_kleur", arguments=json.dumps({"kleur": color}))
            return SimpleNamespace(output=[call], usage=SimpleNamespace(**{**usage, "input_tokens_details": SimpleNamespace(**details)}),
                                   model=kwargs["model"], service_tier="default")
    return SimpleNamespace(responses=Responses()), sent


def test_the_live_check_passes_a_model_that_does_it_all():
    client_, sent = _fake_openai()
    result = asyncio.run(model_check.check(client_, "gpt-6-luna"))
    assert model_check.passed(result), result
    assert sent[0]["reasoning"] == {"effort": "low"} and sent[1]["reasoning"] == {"effort": "medium"}
    assert sent[0]["input"][0]["content"][0]["prompt_cache_breakpoint"] and sent[0]["prompt_cache_options"]
    assert any(p["type"] == "input_image" for p in sent[0]["input"][1]["content"])
    assert result["cost_usd"] > 0 and result["cost_status"] == "estimated"


@pytest.mark.parametrize("reject, failed", [(("reasoning",), "reasoning"), (("prompt_cache_options",), "cache"), (("image",), "image")])
def test_the_live_check_fails_what_a_model_rejects(reject, failed):
    client_, _ = _fake_openai(reject=reject)
    result = asyncio.run(model_check.check(client_, "gpt-oud"))
    assert result[failed] is False and not model_check.passed(result)
    assert all(result[f] for f in model_check.FEATURES if f != failed)


def test_the_live_check_needs_the_photo_seen_and_the_cache_reported():
    client_, _ = _fake_openai(color="rood")
    blind = asyncio.run(model_check.check(client_, "gpt-blind"))
    assert blind["image"] is False and "rood" in blind["notes"][0]
    client_, _ = _fake_openai(cache_fields=False)
    assert asyncio.run(model_check.check(client_, "gpt-zonder-cache"))["cache"] is False
    client_, _ = _fake_openai(cached=0)  # accepted and reported, but no hit this time: noted, not failed
    missed = asyncio.run(model_check.check(client_, "gpt-mis"))
    assert missed["cache"] is True and "cache-treffer" in missed["notes"][0]
