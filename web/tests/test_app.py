"""The API end to end, with the AI and the weather replaced by fakes."""

import io
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from types import SimpleNamespace

from app import claude, db, gpt, main, weather

H = {"X-Dressing": "1"}


@pytest.fixture
def client(monkeypatch):
    db.init()
    with db.connect() as conn:
        for table in ("worn", "items", "sessions", "login_failures", "audit_log", "users"):
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

    async def fake_ask(user, tool, *, system, content, effort="low"):
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

    async def fake_ask(user, tool, *, system, content, effort="low"):
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

    async def fake_ask(user, tool, *, system, content, effort="low"):
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

    async def fake_ask(user, tool, *, system, content, effort="low"):
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
