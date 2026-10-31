"""The stylist's targeted trend search (trends_zoeken), end to end with the AI services replaced by fakes: the search
itself with Claude's and ChatGPT's web search, the week's cache, its cost, and the fixes to the trend headlines."""

import time
from types import SimpleNamespace

import pytest

from app import claude, costs, db, gpt, trends
from tests.test_app import H, add, client  # noqa: F401  (client is a fixture)

TOPIC = "herfst 2026 trends etentje neutrale tinten bruin"


@pytest.fixture(autouse=True)
def empty_search_cache():
    trends._searches.clear()
    (db.DATA_DIR / "trend-searches.json").unlink(missing_ok=True)
    yield
    trends._searches.clear()


def _claude_fakes(monkeypatch, shirt_id: str, *, searches_per_question: int = 1):
    """The stylist (account model) asks trends_zoeken once; the search (helper model) answers with web search."""
    sent, searched = [], []

    class FakeMessages:
        async def create(self, **kwargs):
            if any(t.get("name") == "web_search" for t in kwargs["tools"]):
                searched.append(kwargs)
                text = SimpleNamespace(type="text", text="- Bruin als nieuw zwart: chocolade met crème.", citations=[
                    SimpleNamespace(type="web_search_result_location", url="https://www.vogue.co.uk/article/brown",
                                    title="Brown is the colour of autumn")])
                result = SimpleNamespace(type="web_search_tool_result", content=[
                    SimpleNamespace(type="web_search_result", url="https://www.whowhatwear.com/trends", title="Trends")])
                usage = SimpleNamespace(input_tokens=3000, output_tokens=200, server_tool_use=SimpleNamespace(web_search_requests=1),
                                        cache_creation_input_tokens=0, cache_read_input_tokens=0, service_tier="standard")
                return SimpleNamespace(content=[SimpleNamespace(type="server_tool_use", id="s1", name="web_search"), result, text],
                                       stop_reason="end_turn", usage=usage, model=kwargs["model"])
            sent.append(kwargs)
            last = kwargs["messages"][-1]["content"]
            if isinstance(last, list) and last and isinstance(last[0], dict) and last[0].get("type") == "tool_result":
                done = SimpleNamespace(type="tool_use", id="t9", name="geef_outfits", input={
                    "advies": "Ik pas de bruintrend toe met je hemd.", "outfits": [{"titel": "Bruin", "stukken": [shirt_id]}]})
                return SimpleNamespace(content=[done], stop_reason="tool_use")
            calls = [SimpleNamespace(type="tool_use", id=f"t{n}", name="trends_zoeken", input={"onderwerp": TOPIC})
                     for n in range(searches_per_question)]
            return SimpleNamespace(content=calls, stop_reason="tool_use")
    monkeypatch.setattr(claude, "client", lambda user: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    return sent, searched


def test_claude_searches_trends_for_the_question_and_shows_the_sources(client, monkeypatch):
    shirt = add(client, name="Bruin hemd")
    sent, searched = _claude_fakes(monkeypatch, shirt["id"])
    r = client.post("/api/stylist", json={"query": "etentje op restaurant", "use_weather": False}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    # The stylist was offered the tool and told when to use it.
    assert "trends_zoeken" in [t["name"] for t in sent[0]["tools"]]
    assert "trends_zoeken" in sent[0]["system"][0]["text"]
    # The search ran on the helper model with Anthropic's web search, and only the topic went along.
    assert len(searched) == 1 and searched[0]["model"] == claude.HELPER_MODEL
    tool = searched[0]["tools"][0]
    assert tool["type"] == ("web_search_20250305" if claude.HELPER_MODEL.startswith("claude-haiku") else "web_search_20260209")
    assert TOPIC in searched[0]["messages"][0]["content"] and "geboortedatum" not in searched[0]["messages"][0]["content"]
    # What the stylist read back: the summary and its sources.
    answer = sent[1]["messages"][-1]["content"][0]["content"]
    assert "Bruin als nieuw zwart" in answer and "https://www.vogue.co.uk/article/brown" in answer
    assert "Onbetrouwbare brondata" in answer
    # The app shows what was searched, with the cited source (cited beats merely consulted).
    assert body["trend_search"]["topic"] == TOPIC and body["trend_search"]["cached"] is False
    assert [s["url"] for s in body["trend_search"]["sources"]] == ["https://www.vogue.co.uk/article/brown"]
    # The search is in the answer's cost: $10 per 1000 searches on top of the tokens.
    helper = next(m for m in body["analysis"]["per_model"] if m["model"] == claude.HELPER_MODEL)
    tokens = (3000 * costs.price("anthropic", claude.HELPER_MODEL)["input"] + 200 * costs.price("anthropic", claude.HELPER_MODEL)["output"]) / 1e6
    assert helper["cost_usd"] == pytest.approx(tokens + 0.01)


def test_the_same_topic_is_searched_once_a_week_and_once_per_question(client, monkeypatch):
    shirt = add(client, name="Bruin hemd")
    sent, searched = _claude_fakes(monkeypatch, shirt["id"], searches_per_question=2)
    r = client.post("/api/stylist", json={"query": "etentje", "use_weather": False}, headers=H).json()
    # Two calls in one question: the second is told to use the first result.
    results = sent[1]["messages"][-1]["content"]
    assert len(searched) == 1 and "Je zocht al trends" in results[1]["content"]
    assert r["trend_search"]["cached"] is False
    # Another question on the same topic (phrased in another order) within the week comes from the cache.
    trends._searches.clear()  # also from the file, as after a restart
    again = client.post("/api/stylist", json={"query": "etentje", "use_weather": False}, headers=H).json()
    assert len(searched) == 1 and again["trend_search"]["cached"] is True
    assert trends.cached_search(db.DATA_DIR / "trend-searches.json", "Bruin neutrale TINTEN etentje herfst 2026 trends") is not None
    # After a week it searches again.
    later = time.time() + trends.SEARCH_MAX_AGE_SECONDS + 1
    assert trends.cached_search(db.DATA_DIR / "trend-searches.json", TOPIC, now=later) is None


def test_a_follow_up_does_not_search_for_trends(client, monkeypatch):
    shirt = add(client, name="Bruin hemd")
    sent, _ = _claude_fakes(monkeypatch, shirt["id"])

    async def fake_ask(user, tool, *, system, content, helpers=None, **kwargs):
        sent.append(helpers or {})
        return {"advies": "Ok.", "outfits": [{"titel": "Bruin", "stukken": [shirt["id"]]}]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    previous = {"queries": ["etentje"], "outfits": [{"title": "Bruin", "item_ids": [shirt["id"]]}]}
    r = client.post("/api/stylist", json={"query": "andere schoenen", "use_weather": False, "previous": previous}, headers=H)
    assert r.status_code == 200, r.text
    assert "trends_zoeken" not in sent[-1] and r.json()["trend_search"] is None


def test_chatgpt_searches_with_openai_web_search_and_counts_the_search(client, monkeypatch):
    shirt = add(client, name="Bruin hemd")
    asked = []

    class FakeResponses:
        async def create(self, **kwargs):
            asked.append(kwargs)
            if kwargs.get("tools", [{}])[0].get("type") == "web_search":
                note = SimpleNamespace(type="url_citation", url="https://www.whowhatwear.com/fashion/trends/brown", title="Brown")
                message = SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="- Bruin.", annotations=[note])])
                search = SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="search", sources=[
                    SimpleNamespace(type="url", url="https://example.org/consulted")]))
                opened = SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="open_page"))
                usage = SimpleNamespace(input_tokens=5000, output_tokens=300,
                                        input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0))
                return SimpleNamespace(id="s", output=[search, opened, message], output_text="- Bruin.", status="completed",
                                       usage=usage, model=kwargs["model"], service_tier="default")
            if "previous_response_id" not in kwargs:
                call = SimpleNamespace(type="function_call", name="trends_zoeken", call_id="c1", arguments='{"onderwerp": "%s"}' % TOPIC)
                return SimpleNamespace(id="r1", output=[call], status="completed")
            assert "- Bruin." in kwargs["input"][0]["output"]
            call = SimpleNamespace(type="function_call", name="geef_outfits",
                                   arguments='{"advies": "Bruintrend.", "outfits": [{"titel": "Bruin", "stukken": ["%s"]}]}' % shirt["id"])
            return SimpleNamespace(id="r2", output=[call], status="completed")
    monkeypatch.setattr(gpt, "client", lambda user: SimpleNamespace(responses=FakeResponses()))
    client.put("/api/settings/provider", json={"provider": "openai"}, headers=H)
    r = client.post("/api/stylist", json={"query": "etentje", "use_weather": False}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    search = next(a for a in asked if a.get("tools", [{}])[0].get("type") == "web_search")
    assert search["model"] == gpt.HELPER_MODEL and search["include"] == ["web_search_call.action.sources"]
    assert "geboortedatum" not in search["input"]
    assert [s["url"] for s in body["trend_search"]["sources"]] == ["https://www.whowhatwear.com/fashion/trends/brown"]
    # One billed search: opening a page isn't a search.
    luna = costs.from_openai(gpt.HELPER_MODEL, SimpleNamespace(input_tokens=5000, output_tokens=300,
                                                              input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0)))
    helper = next(m for m in body["analysis"]["per_model"] if m["model"] == gpt.HELPER_MODEL and m["cost_usd"])
    assert helper["cost_usd"] == pytest.approx(luna.cost_usd + 0.01)


def test_searches_are_priced_per_1000():
    usage = SimpleNamespace(input_tokens=0, output_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=3),
                            cache_creation_input_tokens=0, cache_read_input_tokens=0, service_tier="standard")
    call = costs.from_anthropic("claude-haiku-5-5", usage)
    assert call.searches == 3 and call.cost_usd == pytest.approx(0.03)
    response = SimpleNamespace(output=[{"type": "web_search_call", "action": {"type": "search"}},
                                       {"type": "web_search_call", "action": {"type": "find_in_page"}},
                                       {"type": "web_search_call"}])
    assert costs.openai_searches(response) == 2  # a step that doesn't say what it is counts as a search


# MARK: - The trend headlines

def test_headlines_without_clothes_are_left_out():
    xml = b'''<rss><channel>
      <item><title>7 Of The Best Iron Supplements To Beat Winter Fatigue</title><link>https://www.vogue.co.uk/article/iron</link></item>
      <item><title>New On Netflix 2026: Must-Watch Films</title><link>https://www.vogue.co.uk/gallery/netflix</link></item>
      <item><title>6 Ways To Style A Leather Jacket For Autumn</title><link>https://www.vogue.co.uk/article/leather-jacket</link></item>
    </channel></rss>'''
    found = trends.parse_feed(xml, "British Vogue", trends.FEEDS[0][1])
    assert [a["title"] for a in found] == ["6 Ways To Style A Leather Jacket For Autumn"]


def test_headlines_are_refreshed_before_they_expire():
    # Fetched weekly and kept two weeks: the stylist always has them, also when one weekly fetch fails.
    assert trends.REFRESH_SECONDS < trends.MAX_AGE_SECONDS
    assert trends.MAX_FEED_BYTES >= 3_000_000  # Who What Wear's feed is close to 3 MB
