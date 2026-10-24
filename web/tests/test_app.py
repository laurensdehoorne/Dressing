"""The API end to end, with the AI and the weather replaced by fakes."""

import io
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from types import SimpleNamespace

from app import claude, costs, db, gpt, main, push, weather

H = {"X-Dressing": "1"}


@pytest.fixture
def client(monkeypatch):
    db.init()
    with db.connect() as conn:
        for table in ("worn", "rejected", "items", "inspiration", "sessions", "login_failures", "audit_log", "ai_usage", "request_keys", "push_subscriptions", "users"):
            conn.execute(f"DELETE FROM {table}")
    monkeypatch.setattr(main.keys, "active_key", lambda user, name="claude": ("sk-test", "env"))
    with TestClient(main.app) as c:
        r = c.post("/api/auth/setup", json={"username": "laurens", "password": "geheim123"}, headers=H)
        assert r.status_code == 200
        yield c


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 60), "navy").save(out, "JPEG")
    return out.getvalue()


def add(client, **data):
    r = client.post("/api/items", json={"data": {"name": "Stuk", "category": "top", **data}}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_changes_need_the_app_header(client):
    assert client.post("/api/items", json={"data": {"name": "x"}}).status_code == 403


def test_identify_keeps_the_photo_for_the_new_item(client, monkeypatch):
    async def fake(user, photo, hint):
        assert photo and hint == "merk Zara"
        return {"name": "Marineblauwe trui", "category": "trui", "colors": ["marineblauw"], "warmth": 4}
    monkeypatch.setattr(claude, "identify_item", fake)
    r = client.post("/api/identify", files={"photo": ("f.jpg", jpeg(), "image/jpeg")}, data={"hint": "merk Zara"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["info"]["category"] == "trui"
    r = client.post("/api/items", json={"data": {**body["info"], "location": "Linkerkast, plank 2"}, "photo_token": body["photo_token"]}, headers=H)
    item = r.json()
    assert item["photo"] and item["location"] == "Linkerkast, plank 2" and item["status"] == "dressing"
    assert client.get(item["photo"]).status_code == 200
    assert client.get(item["photo"] + "&w=1").status_code == 200


def test_identify_in_the_background(client, monkeypatch):
    async def fake(user, photo, hint):
        return {"name": "Jeans", "category": "broek"}
    monkeypatch.setattr(claude, "identify_item", fake)
    job = client.post("/api/identify?background=1", data={"hint": "blauwe jeans"}, headers=H).json()["job"]
    for _ in range(50):
        r = client.get(f"/api/jobs/{job}").json()
        if r["done"]:
            break
    assert r["result"]["info"]["name"] == "Jeans"


def test_clean_item_limits_fields_and_values():
    out = db.clean_item({"name": " Hemd ", "category": "bestaat niet", "seasons": ["Zomer", "regen"], "warmth": 9,
                         "colors": "wit, blauw", "secret": 1, "price": "12,5"})
    assert out == {"name": "Hemd", "category": "accessoire", "seasons": ["zomer"], "warmth": None,
                   "colors": ["wit", "blauw"], "price": None}


def test_update_and_delete_item(client):
    item = add(client)
    r = client.patch(f"/api/items/{item['id']}", json={"status": "was", "location": "Wasmand"}, headers=H)
    assert r.json()["status"] == "was" and r.json()["name"] == "Stuk"
    assert client.delete(f"/api/items/{item['id']}", headers=H).status_code == 200
    assert client.get("/api/state").json()["items"] == []


def test_worn_only_with_own_items(client):
    item = add(client)
    r = client.post("/api/worn", json={"item_ids": [item["id"], "IEMAND-ANDERS"], "rating": 4, "occasion": "kantoor"}, headers=H)
    assert r.status_code == 200
    assert r.json()["item_ids"] == [item["id"]]
    assert client.post("/api/worn", json={"item_ids": ["X"]}, headers=H).status_code == 400


def test_stylist_gets_only_what_is_in_the_dressing(client, monkeypatch):
    here, washing = add(client, name="Wit hemd"), add(client, name="Vuile jeans", category="broek", status="was")
    client.post("/api/worn", json={"item_ids": [here["id"]], "rating": 5, "note": "top"}, headers=H)
    client.put("/api/settings/place", json={"place": ""}, headers=H)
    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["text"] = "\n".join(c["text"] for c in content)
        return {"advies": "Fris.", "outfits": [{"titel": "Look", "stukken": [here["id"], washing["id"], "verzonnen"], "waarom": "Past."}]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    r = client.post("/api/stylist", json={"query": "kantoor"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["outfits"][0]["item_ids"] == [here["id"]]  # not the one in the wash, nor an invented id
    assert "Vuile jeans" not in seen["text"] and "score 5/5" in seen["text"] and "Vraag: kantoor" in seen["text"]


def test_stylist_uses_the_weather(client, monkeypatch):
    add(client)

    async def place(name):
        return {"name": "Kortrijk, België", "lat": 50.8, "lon": 3.3}

    async def forecast(p):
        return {"place": p["name"], "current": {"temp": 8, "feels": 5, "summary": "regen", "wind": 30}, "days": []}
    monkeypatch.setattr(weather, "find_place", place)
    monkeypatch.setattr(weather, "forecast", forecast)
    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["text"] = content[-1]["text"]
        return {"advies": "", "outfits": []}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    assert client.put("/api/settings/place", json={"place": "Kortrijk"}, headers=H).json()["place"]["name"] == "Kortrijk, België"
    r = client.post("/api/stylist", json={"query": "vandaag"}, headers=H)
    assert "8 °C (voelt als 5 °C), regen" in seen["text"] and r.json()["weather"]["current"]["temp"] == 8


def test_other_accounts_cannot_see_items(client):
    item = add(client)
    client.post("/api/users", json={"username": "gast", "password": "geheim123"}, headers=H)
    client.post("/api/auth/logout", headers=H)
    client.post("/api/auth/login", json={"username": "gast", "password": "geheim123"}, headers=H)
    assert client.get("/api/state").json()["items"] == []
    assert client.patch(f"/api/items/{item['id']}", json={"name": "Mijn"}, headers=H).status_code == 404


def test_api_key_is_stored_encrypted(client, monkeypatch):
    monkeypatch.undo()  # the real key lookup

    async def ok(key):
        return None
    monkeypatch.setattr(claude, "check_key", ok)
    r = client.put("/api/settings/api-key", json={"key": "sk-ant-api03-zyxwvutsrqponmabcdefghijkl"}, headers=H)
    assert r.json()["api_key"]["hint"] == "sk-ant-…ijkl"
    with db.connect() as conn:
        assert "abcdefghijkl" not in conn.execute("SELECT settings FROM users").fetchone()[0]
    assert client.get("/api/state").json()["ai"]["services"]["claude"]["api_key"]["source"] == "app"


def test_chatgpt_answers_when_chosen(client, monkeypatch):
    add(client, name="Wit hemd")
    asked = {}

    class FakeResponses:
        async def create(self, **kwargs):
            asked.update(kwargs)
            call = SimpleNamespace(type="function_call", name="geef_outfits",
                                   arguments='{"advies": "Ok.", "outfits": []}')
            return SimpleNamespace(id="r1", output=[call], status="completed")
    monkeypatch.setattr(gpt, "client", lambda user: SimpleNamespace(responses=FakeResponses()))

    async def no_claude(*a, **k):
        raise AssertionError("Claude was asked")
    monkeypatch.setattr(claude, "_ask_claude", no_claude)
    assert client.put("/api/settings/provider", json={"provider": "openai"}, headers=H).json()["provider"] == "openai"
    r = client.post("/api/stylist", json={"query": "kantoor", "use_weather": False}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["advice"] == "Ok."
    assert asked["model"] == gpt.DEFAULT_MODEL and asked["tools"][0]["name"] == "geef_outfits"
    assert asked["input"][1]["content"][0]["prompt_cache_breakpoint"]  # the dressing is cached, as with Claude
    assert client.get("/api/state").json()["ai"]["provider"] == "openai"


def test_chatgpt_reads_the_photo(monkeypatch):
    parts = gpt._content([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}},
                          {"type": "text", "text": "Beschrijf dit."}])
    assert parts[0] == {"type": "input_image", "image_url": "data:image/jpeg;base64,AAA", "detail": "auto"}
    assert parts[1] == {"type": "input_text", "text": "Beschrijf dit."}


def test_openai_key_is_checked_and_kept_apart(client, monkeypatch):
    monkeypatch.undo()

    async def ok(key):
        return None
    monkeypatch.setattr(gpt, "check_key", ok)
    bad = client.put("/api/settings/api-key", json={"key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "provider": "openai"}, headers=H)
    assert bad.status_code == 400
    r = client.put("/api/settings/api-key", json={"key": "sk-proj-abcdefghijklmnopqrstuvwxyz1234", "provider": "openai"}, headers=H)
    assert r.status_code == 200, r.text
    services = client.get("/api/state").json()["ai"]["services"]
    assert services["openai"]["api_key"]["source"] == "app" and services["claude"]["api_key"]["source"] is None


def test_order_mail_gives_pieces_with_their_photos(client, monkeypatch):
    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["tool"], seen["text"] = tool["name"], content[-1]["text"]
        seen["labels"] = [c["text"] for c in content if c["type"] == "text"][:-1]
        seen["images"] = sum(c["type"] == "image" for c in content)
        return {"winkel": "Zara", "besteldatum": "2026-10-20", "stukken": [
            {"naam": "Ecru gebreide trui", "categorie": "trui", "kleuren": ["ecru"], "maat": "M", "prijs": 39.95, "foto": 2},
            {"naam": "Zwarte jeans", "categorie": "broek", "kleuren": ["zwart"], "foto": 7},  # no such photo
        ]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    fetched = []
    monkeypatch.setattr(main, "_fetch_photo", lambda url: fetched.append(url) or jpeg())
    mail = "Bedankt voor je bestelling [foto 1]\n[foto 2] ECRU GEBREIDE TRUI maat M 39,95 EUR\nZWARTE JEANS 29,95 EUR"
    r = client.post("/api/order", json={"text": mail, "photos": ["https://static.zara.net/logo.png", "https://static.zara.net/trui.jpg"]}, headers=H)
    assert r.status_code == 200, r.text
    assert seen["tool"] == "registreer_bestelling" and "[foto 2] ECRU" in seen["text"]
    assert sorted(fetched) == ["https://static.zara.net/logo.png", "https://static.zara.net/trui.jpg"]
    assert seen["labels"] == ["[foto 1]", "[foto 2]"] and seen["images"] == 2  # the AI sees the pictures
    trui, jeans = r.json()["pieces"]
    assert trui["info"] == {"name": "Ecru gebreide trui", "category": "trui", "colors": ["ecru"], "brand": "Zara", "size": "M",
                            "price": 39.95, "purchase_date": "2026-10-20"}
    assert trui["thumb"].startswith("data:image/jpeg;base64,") and not jeans["photo_token"]
    item = client.post("/api/items", json={"data": {**trui["info"], "location": "Kast"}, "photo_token": trui["photo_token"]}, headers=H).json()
    assert item["photo"] and item["brand"] == "Zara"


def test_order_photos_only_from_the_internet():
    assert not main._public_host("http://static.zara.net/a.jpg")
    assert not main._public_host("https://127.0.0.1/a.jpg")
    assert not main._public_host("https://192.168.1.10/a.jpg")
    assert not main._public_host("https://localhost/a.jpg")
    assert main._fetch_photo("https://10.0.0.1/a.jpg") is None


def test_suitcase_trip_and_stylist(client, monkeypatch):
    shirt, coat = add(client, name="Linnen hemd"), add(client, name="Winterjas", category="jas")

    async def place(name):
        return {"name": "Barcelona, Spanje", "lat": 41.4, "lon": 2.2}

    async def trip_forecast(p, start, end):
        return {"place": p["name"], "start": start, "end": end,
                "days": [{"date": start, "summary": "helder", "min": 18, "max": 26, "feels_min": 18, "feels_max": 27, "rain": 5, "wind": 12}]}
    monkeypatch.setattr(weather, "find_place", place)
    monkeypatch.setattr(weather, "trip_forecast", trip_forecast)
    assert client.put("/api/trip/items", json={"item_ids": [shirt["id"]]}, headers=H).status_code == 400  # no trip yet
    assert client.put("/api/trip", json={"destination": "Barcelona", "start": "2026-11-03", "end": "2026-11-01"}, headers=H).status_code == 400
    trip = client.put("/api/trip", json={"destination": "Barcelona", "start": "2026-11-01", "end": "2026-11-05"}, headers=H).json()["trip"]
    assert trip["place"]["name"] == "Barcelona, Spanje" and trip["item_ids"] == []
    seen = []

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen.append("\n".join(c["text"] for c in content))
        return {"advies": "", "outfits": [{"titel": "Look", "stukken": [shirt["id"], coat["id"]], "waarom": "."}]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    # An empty suitcase: the stylist helps to pack, from the whole dressing.
    r = client.post("/api/stylist", json={"query": "inpakken", "trip": True}, headers=H).json()
    assert "Winterjas" in seen[-1] and "nog inpakken" in seen[-1] and "18 tot 26 °C" in seen[-1]
    packed = client.put("/api/trip/items", json={"item_ids": [shirt["id"], "VAN-IEMAND-ANDERS"]}, headers=H).json()["trip"]
    assert packed["item_ids"] == [shirt["id"]]
    r = client.post("/api/stylist", json={"query": "stad", "trip": True}, headers=H).json()
    assert "Mijn valies" in seen[-1] and "Winterjas" not in seen[-1]
    assert r["outfits"][0]["item_ids"] == [shirt["id"]]
    # Changing the dates keeps what is packed; ending the trip empties it.
    again = client.put("/api/trip", json={"destination": "Barcelona", "start": "2026-11-02", "end": "2026-11-05"}, headers=H).json()["trip"]
    assert again["item_ids"] == [shirt["id"]]
    assert client.get("/api/state").json()["trip"]["start"] == "2026-11-02"
    client.delete("/api/trip", headers=H)
    assert client.get("/api/state").json()["trip"] is None


def test_trip_weather_text():
    w = {"place": "Oslo, Noorwegen", "start": "2027-01-10", "end": "2027-01-12", "days": []}
    assert "gebruikelijke weer in Oslo" in weather.describe_trip(w)


def test_order_from_a_pdf_takes_its_pictures(client):
    out = io.BytesIO()
    Image.new("RGB", (54, 76), "beige").save(out, "PDF")
    text, photos = main._pdf_contents(out.getvalue())
    assert list(photos) == [1] and Image.open(io.BytesIO(photos[1])).size == (54, 76)
    r = client.post("/api/order/pdf", files={"pdf": ("b.pdf", out.getvalue(), "application/pdf")}, headers=H)
    assert r.status_code == 400 and "geen leesbare tekst" in r.json()["detail"]  # a picture only: nothing to read
    assert client.post("/api/order/pdf", files={"pdf": ("b.pdf", b"geen pdf", "application/pdf")}, headers=H).status_code == 400


def test_order_from_screenshots_cuts_out_the_photos(client, monkeypatch):
    shot = Image.new("RGB", (1170, 2532), "white")
    shot.paste(Image.new("RGB", (400, 600), "navy"), (200, 400))  # the product photo on the screenshot
    out = io.BytesIO()
    shot.save(out, "PNG")
    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["labels"] = [c["text"] for c in content if c["type"] == "text"]
        w = 725  # the screenshot as the AI sees it: 1170×2532 brought to 1568 high
        scale = w / 1170
        box = [round(200 * scale), round(400 * scale), round(600 * scale), round(1000 * scale)]
        return {"winkel": "H&M", "stukken": [
            {"naam": "Marineblauwe trui", "categorie": "trui", "kleuren": ["marineblauw"], "foto": 0, "screenshot": 1, "vak": box},
            {"naam": "Sokken", "categorie": "accessoire", "kleuren": ["wit"], "foto": 0, "screenshot": 1, "vak": [0, 0, 5, 5]}]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    r = client.post("/api/order/screenshots", files=[("shots", ("s.png", out.getvalue(), "image/png"))], headers=H)
    assert r.status_code == 200, r.text
    assert seen["labels"][0] == "[screenshot 1: 725×1568 pixels]" and "screenshots" in seen["labels"][-1]
    trui, sokken = r.json()["pieces"]
    assert trui["photo_token"] and not sokken["photo_token"]  # a box too small to be a photo: none
    photo = Image.open(db.TMP_DIR / f"{trui['photo_token']}.jpg")
    assert abs(photo.width - 400) < 6 and abs(photo.height - 600) < 6
    r, g, b = photo.getpixel((photo.width // 2, photo.height // 2))
    assert b > 100 and r < 50  # navy: the photo, not the white around it
    too_many = [("shots", (f"{n}.png", out.getvalue(), "image/png")) for n in range(7)]
    assert client.post("/api/order/screenshots", files=too_many, headers=H).status_code == 400


def _amsterdam(monkeypatch):
    async def place(name):
        assert name == "Amsterdam"
        return {"name": "Amsterdam, Nederland", "lat": 52.4, "lon": 4.9}

    async def trip_forecast(p, start, end):
        return {"place": p["name"], "start": start, "end": end, "days": [
            {"date": start, "summary": "regen", "min": 6, "max": 11, "feels_min": 3, "feels_max": 9, "rain": 80, "wind": 40}]}
    monkeypatch.setattr(weather, "find_place", place)
    monkeypatch.setattr(weather, "trip_forecast", trip_forecast)


def test_stylist_looks_up_the_weather_where_the_question_is_about(client, monkeypatch):
    shirt = add(client, name="Wit hemd")
    client.put("/api/settings/place", json={"place": ""}, headers=H)
    _amsterdam(monkeypatch)
    sent = []

    class FakeMessages:
        async def create(self, **kwargs):
            sent.append(kwargs)
            if len(sent) == 1:
                assert [t["name"] for t in kwargs["tools"]] == ["geef_outfits", "weer_opvragen"]
                call = SimpleNamespace(type="tool_use", id="t1", name="weer_opvragen", input={"plaats": "Amsterdam", "van": "2026-10-25"})
                return SimpleNamespace(content=[call], stop_reason="tool_use")
            result = kwargs["messages"][-1]["content"][0]
            assert result["tool_use_id"] == "t1" and "6 tot 11 °C" in result["content"] and "Weer in Amsterdam" in result["content"]
            done = SimpleNamespace(type="tool_use", id="t2", name="geef_outfits",
                                   input={"advies": "Regenjas mee.", "outfits": [{"titel": "Stad", "stukken": [shirt["id"]]}]})
            return SimpleNamespace(content=[done], stop_reason="tool_use")
    monkeypatch.setattr(claude, "client", lambda user: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    r = client.post("/api/stylist", json={"query": "uitstap naar Amsterdam"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["advice"] == "Regenjas mee." and body["elsewhere"][0]["place"] == "Amsterdam, Nederland"
    assert len(sent) == 2


def test_chatgpt_looks_up_the_weather_too(client, monkeypatch):
    shirt = add(client, name="Wit hemd")
    _amsterdam(monkeypatch)
    asked = []

    class FakeResponses:
        async def create(self, **kwargs):
            asked.append(kwargs)
            if len(asked) == 1:
                call = SimpleNamespace(type="function_call", name="weer_opvragen", call_id="c1",
                                       arguments='{"plaats": "Amsterdam", "van": "2026-10-25"}')
                return SimpleNamespace(id="r1", output=[call], status="completed")
            assert kwargs["previous_response_id"] == "r1" and kwargs["input"][0]["call_id"] == "c1"
            assert "6 tot 11 °C" in kwargs["input"][0]["output"]
            call = SimpleNamespace(type="function_call", name="geef_outfits",
                                   arguments='{"advies": "Ok.", "outfits": [{"titel": "Stad", "stukken": ["%s"]}]}' % shirt["id"])
            return SimpleNamespace(id="r2", output=[call], status="completed")
    monkeypatch.setattr(gpt, "client", lambda user: SimpleNamespace(responses=FakeResponses()))
    client.put("/api/settings/provider", json={"provider": "openai"}, headers=H)
    r = client.post("/api/stylist", json={"query": "uitstap naar Amsterdam"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["elsewhere"][0]["days"][0]["max"] == 11


def test_photo_work_runs_off_the_event_loop(client, monkeypatch):
    """Decoding and scaling photos happens in a thread, so the server keeps answering in the meantime."""
    import asyncio
    real, in_loop = main._jpeg, []

    def watched(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            in_loop.append(args[1:])
        except RuntimeError:
            pass
        return real(*args, **kwargs)
    monkeypatch.setattr(main, "_jpeg", watched)

    async def fake_identify(user, photo, hint):
        return {"name": "Trui", "category": "trui"}
    monkeypatch.setattr(claude, "identify_item", fake_identify)

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        return {"winkel": "Zara", "stukken": [{"naam": "Trui", "categorie": "trui", "foto": 1, "screenshot": 1, "vak": [10, 10, 200, 300]}]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    monkeypatch.setattr(main, "_fetch_photo", lambda url: real(jpeg(), 1200, 78))

    item = add(client)
    assert client.post(f"/api/items/{item['id']}/photo", files={"photo": ("f.jpg", jpeg(), "image/jpeg")}, headers=H).status_code == 200
    assert client.post("/api/identify", files={"photo": ("f.jpg", jpeg(), "image/jpeg")}, headers=H).status_code == 200
    assert client.post("/api/order", json={"text": "Bestelling: [foto 1] trui 39,95 EUR", "photos": ["https://x.example/t.jpg"]},
                       headers=H).status_code == 200
    shot = io.BytesIO()
    Image.new("RGB", (600, 900), "navy").save(shot, "PNG")
    assert client.post("/api/order/screenshots", files=[("shots", ("s.png", shot.getvalue(), "image/png"))], headers=H).status_code == 200
    assert in_loop == []


def test_server_days_are_belgian():
    """The image sets TZ, so 'today' is the day it is in Belgium, also just after midnight."""
    assert "TZ=Europe/Brussels" in (main.STATIC_DIR.parent / "Dockerfile").read_text()


# MARK: - AI costs, Zuinig, saving once, notifications

def _fake_claude(monkeypatch, answer: dict, sent: list):
    """Claude answering with `answer` as its tool call, and the usage Anthropic reports."""
    class FakeMessages:
        async def create(self, **kwargs):
            sent.append(kwargs)
            usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=3000,
                                    cache_read_input_tokens=0, service_tier="standard")
            call = SimpleNamespace(type="tool_use", id="t1", name=kwargs["tools"][0]["name"], input=answer)
            return SimpleNamespace(content=[call], stop_reason="tool_use", model=kwargs["model"], usage=usage)
    monkeypatch.setattr(claude, "client", lambda user: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))


def test_price_of_one_claude_answer():
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 5000}
    call = costs.from_anthropic("claude-sonnet-5-5", usage)
    # 1000 plain × $2 + 3000 written × $2.50 + 5000 read × $0.10 + 200 out × $10, per million tokens
    assert call.status == "estimated" and call.input_tokens == 9000
    assert abs(call.cost_usd - (1000 * 2 + 3000 * 2.5 + 5000 * 0.1 + 200 * 10) / 1e6) < 1e-12
    assert costs.from_anthropic("claude-nieuw-9", usage).status == "unknown"  # not on the price list: no guess
    assert costs.from_openai("gpt-6-luna", {"input_tokens": 10, "output_tokens": 5}).status == "partial"  # no cache details


def test_stylist_answer_is_metered_and_shown_in_settings(client, monkeypatch):
    shirt = add(client, name="Wit hemd")
    client.put("/api/settings/place", json={"place": ""}, headers=H)
    _fake_claude(monkeypatch, {"advies": "Ok.", "outfits": [{"titel": "Kantoor", "stukken": [shirt["id"]]}]}, [])
    r = client.post("/api/stylist", json={"query": "kantoor"}, headers=H).json()
    assert r["analysis"]["status"] == "estimated" and r["analysis"]["cost_eur"] > 0
    usage = client.get("/api/state").json()["ai_usage"]
    assert usage["count"] == 1 and usage["latest"][0]["task"] == "stylist"
    assert usage["per_model"][0]["models"] == claude.DEFAULT_MODEL


def test_zuinig_reads_a_photo_with_the_helper_model(client, monkeypatch):
    sent = []
    _fake_claude(monkeypatch, {"naam": "Trui", "categorie": "trui"}, sent)
    client.post("/api/identify", data={"hint": "blauwe trui"}, headers=H)
    assert sent[-1]["model"] == claude.DEFAULT_MODEL  # standaard: the account's model
    assert client.put("/api/settings/quality", json={"quality": "zuinig"}, headers=H).json()["quality"] == "zuinig"
    client.post("/api/identify", data={"hint": "blauwe trui"}, headers=H)
    assert sent[-1]["model"] == claude.HELPER_MODEL
    shirt = add(client)
    _fake_claude(monkeypatch, {"advies": "", "outfits": [{"titel": "x", "stukken": [shirt["id"]]}]}, sent)
    client.post("/api/stylist", json={"query": "kantoor", "use_weather": False}, headers=H)
    assert sent[-1]["model"] == claude.DEFAULT_MODEL  # the stylist keeps the account's model
    assert client.put("/api/settings/quality", json={"quality": "gratis"}, headers=H).status_code == 400


def test_a_save_sent_twice_is_kept_once(client):
    body = {"data": {"name": "Wit hemd", "category": "top"}, "request_id": "save-1234-abcd"}
    first = client.post("/api/items", json=body, headers=H).json()
    again = client.post("/api/items", json=body, headers=H).json()
    assert first["id"] == again["id"] and len(client.get("/api/state").json()["items"]) == 1
    worn = {"item_ids": [first["id"]], "request_id": "wear-1234-abcd"}
    assert client.post("/api/worn", json=worn, headers=H).json()["id"] == client.post("/api/worn", json=worn, headers=H).json()["id"]
    assert len(client.get("/api/state").json()["worn"]) == 1
    # A save that failed gives its id back: the retry does it.
    bad = {"data": {"name": ""}, "request_id": "save-5678-abcd"}
    assert client.post("/api/items", json=bad, headers=H).status_code == 400
    assert client.post("/api/items", json={**bad, "data": {"name": "Jeans"}}, headers=H).status_code == 200


def test_ask_how_the_outfit_was_in_the_evening():
    from datetime import datetime, timezone
    morning = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)  # 8:00 in Brussels (summer time)
    day = morning.astimezone().date().isoformat()
    assert push.ask_when(day, morning) == datetime(2026, 10, 24, 20, 0).astimezone().astimezone(timezone.utc).isoformat(timespec="seconds")
    late = datetime(2026, 10, 24, 20, 30, tzinfo=timezone.utc)
    assert push.ask_when(late.astimezone().date().isoformat(), late) == "2026-10-24T21:00:00+00:00"  # later: in half an hour
    assert push.ask_when("2026-10-20", morning) is None  # an outfit noted afterwards for an earlier day


def test_outfit_question_goes_to_the_phone(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    shirt = add(client, name="Wit hemd")
    sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post("/api/push/subscribe", json={"subscription": sub}, headers=H).json()["devices"][0]["device"]
    today = datetime.now().astimezone().date().isoformat()
    unrated = client.post("/api/worn", json={"item_ids": [shirt["id"]], "date": today}, headers=H).json()
    rated = client.post("/api/worn", json={"item_ids": [shirt["id"]], "date": today, "rating": 4}, headers=H).json()
    assert unrated["ask_at"] and "ask_at" not in rated  # a score already given: nothing to ask
    sent = []
    monkeypatch.setattr(push, "_send_one", lambda subscription, message: sent.append((subscription["endpoint"], message)) or True)
    import asyncio
    asyncio.run(push.send_questions(datetime.fromisoformat(unrated["ask_at"]) + timedelta(minutes=1)))
    assert sent == [(sub["endpoint"], {"title": "👕 Hoe viel je outfit?", "body": "Wit hemd: geef een score, dan kiest de stylist "
                     "voortaan beter.", "url": f"/#gedragen={unrated['id']}", "tag": f"dressing-rate-{unrated['id']}"})]
    asyncio.run(push.send_questions(datetime.now(timezone.utc) + timedelta(days=1)))
    assert len(sent) == 1  # asked once
    assert client.delete(f"/api/push/devices/{push.device_id(sub['endpoint'])}", headers=H).json()["devices"] == []


def shot_png() -> bytes:
    """A phone screenshot with an outfit (navy) amid the app's white."""
    shot = Image.new("RGB", (1170, 2532), "white")
    shot.paste(Image.new("RGB", (400, 600), "navy"), (200, 400))
    out = io.BytesIO()
    shot.save(out, "PNG")
    return out.getvalue()


def test_inspiration_from_screenshots_makes_the_style_the_stylist_uses(client, monkeypatch):
    seen = []

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen.append((tool["name"], "\n".join(c["text"] for c in content if c["type"] == "text")))
        if tool["name"] == "geef_outfits":
            return {"advies": "", "outfits": []}
        scale = 725 / 1170  # the screenshot as the AI sees it
        return {"looks": [{"foto": 1, "vak": [round(200 * scale), round(400 * scale), round(600 * scale), round(1000 * scale)],
                           "beschrijving": "Marineblauwe wijde broek met wit hemd", "kleuren": ["marineblauw", "wit"], "stijl": ["minimalistisch"]},
                          {"foto": 2, "geen_outfit": True}],
                "profiel": "Rustige marine en wit, wijde silhouetten."}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    files = [("shots", ("a.png", shot_png(), "image/png")), ("shots", ("b.png", shot_png(), "image/png"))]
    r = client.post("/api/inspiration", files=files, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] == 1 and body["style_profile"] == "Rustige marine en wit, wijde silhouetten."
    look = body["added"][0]
    assert look["colors"] == ["marineblauw", "wit"] and "url" not in look
    photo = Image.open(db.PHOTO_DIR / f"{look['id']}.jpg")
    assert abs(photo.width - 400) < 6 and abs(photo.height - 600) < 6  # the outfit cut out of the screenshot
    assert client.get(look["photo"]).status_code == 200 and client.get(look["photo"] + "&w=1").status_code == 200
    state = client.get("/api/state").json()
    assert state["style_profile"] == body["style_profile"] and [l["id"] for l in state["inspiration"]] == [look["id"]]

    # A second time, the earlier look and the profile go along to be built on.
    client.post("/api/inspiration", files=files[:1], headers=H)
    assert "Marineblauwe wijde broek met wit hemd" in seen[-1][1] and "Rustige marine en wit" in seen[-1][1]

    add(client, name="Wit hemd")
    client.post("/api/stylist", json={"query": "kantoor", "use_weather": False}, headers=H)
    assert seen[-1][0] == "geef_outfits" and "Mijn stijl, uit outfits die ik mooi vind:\nRustige marine en wit" in seen[-1][1]

    # Only for this account; and gone with its photo when removed.
    client.post("/api/users", json={"username": "ander", "password": "geheim456"}, headers=H)
    other = TestClient(main.app)
    other.post("/api/auth/login", json={"username": "ander", "password": "geheim456"}, headers=H)
    assert other.get(look["photo"]).status_code == 404 and other.get("/api/state").json()["inspiration"] == []
    assert other.delete(f"/api/inspiration/{look['id']}", headers=H).status_code == 404
    assert client.delete(f"/api/inspiration/{look['id']}", headers=H).status_code == 200
    assert not (db.PHOTO_DIR / f"{look['id']}.jpg").exists()


def test_inspiration_from_a_pinterest_link(client, monkeypatch):
    fetched = []
    monkeypatch.setattr(main, "_fetch_page_photo", lambda url: fetched.append(url) or jpeg())

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        return {"looks": [{"foto": 1, "beschrijving": "Camel jas over grijze trui"}], "profiel": "Warme neutrale tinten."}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    r = client.post("/api/inspiration", data={"url": " http://pin.it/abc "}, headers=H)
    assert r.status_code == 200, r.text
    assert fetched == ["https://pin.it/abc"] and r.json()["added"][0]["url"] == "https://pin.it/abc"
    assert client.post("/api/inspiration", data={"url": "geen link"}, headers=H).status_code == 400
    monkeypatch.setattr(main, "_fetch_page_photo", lambda url: None)
    r = client.post("/api/inspiration", data={"url": "https://www.instagram.com/p/xyz/"}, headers=H)
    assert r.status_code == 400 and "screenshot" in r.json()["detail"]


def test_page_photo_comes_from_its_preview_picture(monkeypatch):
    page = b'<html><head><meta content="https://i.pinimg.com/736x/a.jpg" property="og:image"></head></html>'

    class Resp(io.BytesIO):
        headers = {"Content-Type": "text/html; charset=utf-8"}
        def geturl(self):
            return "https://www.pinterest.com/pin/1/"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
    monkeypatch.setattr(main, "_public_host", lambda url: True)
    monkeypatch.setattr(main._opener, "open", lambda req, timeout: Resp(page))
    asked = []
    monkeypatch.setattr(main, "_fetch_photo", lambda url: asked.append(url) or b"jpeg")
    assert main._fetch_page_photo("https://pin.it/abc") == b"jpeg" and asked == ["https://i.pinimg.com/736x/a.jpg"]
    monkeypatch.setattr(main, "_public_host", lambda url: False)
    assert main._fetch_page_photo("https://192.168.1.1/") is None


def test_style_profile_rewritten_and_edited(client, monkeypatch):
    assert client.post("/api/inspiration/profile", headers=H).status_code == 400  # nothing to write it from yet
    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["text"] = content[-1]["text"]
        return {"looks": [{"foto": 1, "beschrijving": "Zwarte jurk met sneakers"}] if len(content) > 1 else [],
                "profiel": "Zwart, sportief-chic."}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    client.post("/api/inspiration", files=[("shots", ("a.png", shot_png(), "image/png"))], headers=H)
    r = client.post("/api/inspiration/profile", headers=H)
    assert r.json()["style_profile"] == "Zwart, sportief-chic." and "Geen nieuwe foto's" in seen["text"]
    r = client.put("/api/settings/style-profile", json={"profile": " Liever geen zwart. "}, headers=H)
    assert r.json()["style_profile"] == "Liever geen zwart."


def test_board_link_gives_its_feed():
    assert main._board_feed("https://nl.pinterest.com/laurens/outfits/") == "https://nl.pinterest.com/laurens/outfits.rss"
    assert main._board_feed("https://www.pinterest.co.uk/laurens/zomer-looks") == "https://www.pinterest.co.uk/laurens/zomer-looks.rss"
    assert main._board_feed("https://www.pinterest.com/pin/12345/") is None  # a pin, not a board
    assert main._board_feed("https://www.pinterest.com/laurens/") is None  # a profile
    assert main._board_feed("https://evil.example/laurens/outfits") is None


def test_board_pins_from_its_feed(monkeypatch):
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Outfits</title>
      <item><link>https://www.pinterest.com/pin/1/</link><description>&lt;a href="/pin/1/"&gt;&lt;img src="https://i.pinimg.com/236x/aa/a.jpg"&gt;&lt;/a&gt;Linnen</description></item>
      <item><link>https://www.pinterest.com/pin/2/</link><description>Geen foto</description></item>
    </channel></rss>"""

    class Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
    monkeypatch.setattr(main, "_public_host", lambda url: True)
    monkeypatch.setattr(main._opener, "open", lambda req, timeout: Resp(feed))
    assert main._board_pins("https://www.pinterest.com/laurens/outfits.rss") == [("https://www.pinterest.com/pin/1/", "https://i.pinimg.com/236x/aa/a.jpg")]
    asked = []
    monkeypatch.setattr(main, "_fetch_photo", lambda url: asked.append(url) or (b"jpeg" if "236x" in url else None))
    assert main._pin_photo("https://i.pinimg.com/236x/aa/a.jpg") == b"jpeg"  # the larger one first, else the feed's
    assert asked == ["https://i.pinimg.com/736x/aa/a.jpg", "https://i.pinimg.com/236x/aa/a.jpg"]


def test_import_a_board_in_batches_without_pins_already_there(client, monkeypatch):
    pins = [(f"https://www.pinterest.com/pin/{n}/", f"https://i.pinimg.com/236x/{n}.jpg") for n in range(8)]
    monkeypatch.setattr(main, "_board_pins", lambda feed: pins if feed == "https://www.pinterest.com/laurens/outfits.rss" else [])
    monkeypatch.setattr(main, "_pin_photo", lambda picture: None if "/3.jpg" in picture else jpeg())
    batches = []

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        n = sum(c["type"] == "image" for c in content)
        batches.append(n)
        return {"looks": [{"foto": i, "beschrijving": f"Look {len(batches)}.{i}"} for i in range(1, n + 1)],
                "profiel": f"Profiel na {len(batches)} keer."}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    r = client.post("/api/inspiration", data={"url": "https://www.pinterest.com/laurens/outfits/"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert batches == [5, 2]  # 6 and 2 pins, one without a picture
    assert len(body["added"]) == 7 and body["skipped"] == 1 and body["known"] == 0 and body["style_profile"] == "Profiel na 2 keer."
    assert {l["url"] for l in body["added"]} == {p for p, _ in pins} - {"https://www.pinterest.com/pin/3/"}

    # Again: only what is new on the board.
    pins.insert(0, ("https://www.pinterest.com/pin/new/", "https://i.pinimg.com/236x/new.jpg"))
    body = client.post("/api/inspiration", data={"url": "https://www.pinterest.com/laurens/outfits"}, headers=H).json()
    assert batches[-1] == 1 and body["known"] == 7 and [l["url"] for l in body["added"]] == ["https://www.pinterest.com/pin/new/"]

    r = client.post("/api/inspiration", data={"url": "https://www.pinterest.com/laurens/geheim"}, headers=H)
    assert r.status_code == 400 and "geheim" in r.json()["detail"]


def test_rejected_outfit_is_remembered_by_the_stylist(client, monkeypatch):
    blazer, jeans = add(client, name="Zwarte blazer", category="blazer"), add(client, name="Blauwe jeans", category="broek")
    assert client.post("/api/rejected", json={"item_ids": [blazer["id"]]}, headers=H).status_code == 400  # why, at least
    r = client.post("/api/rejected", json={"item_ids": [blazer["id"], jeans["id"], "verzonnen"], "reasons": ["Te formeel", "onbekend"],
                                           "note": "  te stijf voor vrijdag ", "query": "kantoor", "request_id": "rej-12345678"}, headers=H)
    assert r.status_code == 200, r.text
    rejected = r.json()
    assert rejected["item_ids"] == [blazer["id"], jeans["id"]] and rejected["reasons"] == ["Te formeel"] and rejected["note"] == "te stijf voor vrijdag"
    again = client.post("/api/rejected", json={"item_ids": [blazer["id"]], "reasons": ["Te formeel"], "request_id": "rej-12345678"}, headers=H)
    assert again.json()["id"] == rejected["id"]  # a second tap keeps one
    state = client.get("/api/state").json()
    assert [x["id"] for x in state["rejected"]] == [rejected["id"]] and "Te formeel" in state["reject_reasons"]

    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["text"] = "\n".join(c["text"] for c in content)
        return {"advies": "", "outfits": []}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    client.post("/api/stylist", json={"query": "kantoor", "use_weather": False}, headers=H)
    assert "die ik afwees" in seen["text"]
    assert "Zwarte blazer + Blauwe jeans (voor 'kantoor'): Te formeel, 'te stijf voor vrijdag'" in seen["text"]

    # Forgotten: the stylist doesn't see it any more; only the own account can.
    client.post("/api/users", json={"username": "ander", "password": "geheim456"}, headers=H)
    other = TestClient(main.app)
    other.post("/api/auth/login", json={"username": "ander", "password": "geheim456"}, headers=H)
    assert other.delete(f"/api/rejected/{rejected['id']}", headers=H).status_code == 404
    assert other.post("/api/rejected", json={"item_ids": [blazer["id"]], "reasons": ["Te formeel"]}, headers=H).status_code == 400
    assert client.delete(f"/api/rejected/{rejected['id']}", headers=H).status_code == 200
    client.post("/api/stylist", json={"query": "kantoor", "use_weather": False}, headers=H)
    assert "die ik afwees" not in seen["text"]


def test_follow_up_adjusts_the_earlier_suggestion(client, monkeypatch):
    hemd, sneakers = add(client, name="Wit hemd"), add(client, name="Witte sneakers", category="schoenen")
    seen = {}

    async def fake_ask(user, tool, *, system, content, effort="low", helpers=None, helper=False):
        seen["question"] = content[-1]["text"]
        return {"advies": "Andere schoenen.", "outfits": [{"titel": "Look", "stukken": [hemd["id"]], "waarom": "Past."}]}
    monkeypatch.setattr(claude, "_ask", fake_ask)
    r = client.post("/api/stylist", json={"query": "andere schoenen", "use_weather": False, "previous": {
        "queries": ["etentje", "iets chiquer"], "outfits": [{"title": "Smart casual", "item_ids": [hemd["id"], sneakers["id"]]}]}}, headers=H)
    assert r.status_code == 200, r.text
    q = seen["question"]
    assert "Eerder vroeg ik: etentje → iets chiquer" in q and f"{sneakers['id']} (Witte sneakers)" in q
    assert "Bijsturing: andere schoenen" in q and "Vraag:" not in q
    client.post("/api/stylist", json={"query": "etentje", "use_weather": False}, headers=H)
    assert "Vraag: etentje" in seen["question"] and "Eerder vroeg ik" not in seen["question"]
