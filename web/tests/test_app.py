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
