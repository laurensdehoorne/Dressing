import time

from app import trends


def test_trend_feed_parser_keeps_only_safe_fashion_articles():
    xml = b'''<rss><channel>
      <item><title>Autumn 2026 runway trends</title><link>https://www.vogue.co.uk/article/autumn-trends</link><description><![CDATA[Rich textures &amp; playful layers]]></description></item>
      <item><title>Celebrity beauty news</title><link>https://www.vogue.co.uk/article/beauty</link></item>
      <item><title>Trend story with unsafe link</title><link>https://example.com/story</link></item>
    </channel></rss>'''
    result = trends.parse_feed(xml, "British Vogue", trends.FEEDS[0][1])
    assert len(result) == 1
    assert result[0]["title"] == "Autumn 2026 runway trends"
    assert result[0]["description"] == "Rich textures & playful layers"


def test_trends_cache_persists_refreshes_monthly_and_limits_forced_refreshes(tmp_path):
    path = tmp_path / "fashion-trends.json"
    calls = []

    def fetch(url):
        calls.append(url)
        domain = "www.vogue.co.uk" if "vogue.co.uk" in url else "www.whowhatwear.com"
        return f'<rss><channel><item><title>Fashion trends 2026</title><link>https://{domain}/article/trends</link></item></channel></rss>'.encode()

    now = time.time()
    assert trends.refresh(path, fetch, now)
    assert len(calls) == 2
    calls.clear()
    assert trends.refresh(path, fetch, now + trends.REFRESH_SECONDS - 1) is False
    assert calls == []
    trends._articles = []  # simulate a process restart, then restore from the persistent snapshot
    trends.restore(path)
    prompt, sources = trends.context()
    assert "Fashion trends 2026" in prompt and len(sources) == 2
    assert trends.refresh(path, fetch, now + trends.REFRESH_SECONDS + 1)
    assert len(calls) == 2
    calls.clear()
    assert trends.refresh(path, fetch, now + trends.REFRESH_SECONDS + 2, force=True) is False
    assert calls == []  # pressing the manual refresh again cannot fetch more than once per 12 hours
    assert trends.refresh(path, fetch, now + trends.REFRESH_SECONDS + trends.FORCED_REFRESH_MIN_SECONDS + 2, force=True)
    assert len(calls) == 2
