"""Filling in thin pieces from their own photo, with the AI replaced by a fake."""

from app import claude
from tests.test_app import H, add, client, jpeg  # noqa: F401  (client is a fixture)


def _with_photo(client, **data):
    item = add(client, **data)
    r = client.post(f"/api/items/{item['id']}/photo", files={"photo": ("f.jpg", jpeg(), "image/jpeg")}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


THIN = {"name": "Zwarte leren jas", "category": "jas", "kind": "leren jack", "colors": ["zwart"], "material": "leer",
        "brand": "Zara", "warmth": 2, "formality": 4}
RICH = {"name": "Ecru trui", "category": "trui", "colors": ["ecru"], "material": "wol", "fit": "los", "pattern": "effen",
        "occasions": ["weekend"], "style_tags": ["klassiek"], "description": "Zachte trui."}
SEEN = {"name": "Zwarte bikerjas", "category": "top", "colors": ["grijs"], "brand": "Mango", "material": "katoen",
        "warmth": 4, "fit": "getailleerd", "pattern": "effen", "occasions": ["etentje", "weekend"],
        "style_tags": ["rock", "klassiek"], "description": "Korte leren bikerjas met rits.", "seasons": ["herfst", "lente"]}


def test_only_thin_pieces_with_a_photo_are_offered(client):
    _with_photo(client, **THIN)
    _with_photo(client, **RICH)
    add(client, **THIN)  # no photo: nothing to look at
    assert client.get("/api/state").json()["enrich_candidates"] == 1


def test_thin_pieces_get_only_their_empty_fields_filled(client, monkeypatch):
    thin = _with_photo(client, **THIN)
    rich = _with_photo(client, **RICH)
    seen = []

    async def fake_identify(user, photo, hint):
        seen.append((photo, hint))
        return dict(SEEN)
    monkeypatch.setattr(claude, "identify_item", fake_identify)
    r = client.post("/api/items/fill-details", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pieces"] == 1 and body["failed"] == 0
    # The AI saw the photo itself, told what the piece already is.
    assert len(seen) == 1 and seen[0][0][:2] == b"\xff\xd8" and "Zwarte leren jas" in seen[0][1] and "leer" in seen[0][1]
    items = {i["id"]: i for i in client.get("/api/state").json()["items"]}
    after = items[thin["id"]]
    # Empty fields are filled in ...
    assert after["fit"] == "getailleerd" and after["occasions"] == ["etentje", "weekend"]
    assert after["style_tags"] == ["rock", "klassiek"] and after["description"] == "Korte leren bikerjas met rits."
    assert after["seasons"] == ["herfst", "lente"] and after["pattern"] == "effen"
    # ... what was there stays, and what identifies the piece is never touched.
    assert after["name"] == "Zwarte leren jas" and after["category"] == "jas" and after["colors"] == ["zwart"]
    assert after["brand"] == "Zara" and after["material"] == "leer" and after["warmth"] == 2
    assert body["fields"] == 6
    assert items[rich["id"]]["description"] == "Zachte trui."
    # Done once: not offered again.
    assert client.get("/api/state").json()["enrich_candidates"] == 0
    assert client.post("/api/items/fill-details", headers=H).status_code == 400


def test_one_piece_that_fails_does_not_stop_the_others(client, monkeypatch):
    first = _with_photo(client, **THIN)
    second = _with_photo(client, **{**THIN, "name": "Gouden pailletten jasje"})

    async def fake_identify(user, photo, hint):
        if "Gouden" in hint:
            raise claude.ClaudeError("Het antwoord duurde te lang.")
        return dict(SEEN)
    monkeypatch.setattr(claude, "identify_item", fake_identify)
    body = client.post("/api/items/fill-details", headers=H).json()
    assert (body["pieces"], body["fields"], body["failed"]) == (1, 6, 1)
    state = client.get("/api/state").json()
    items = {i["id"]: i for i in state["items"]}
    assert items[first["id"]]["description"] and not items[second["id"]].get("description")
    assert state["enrich_candidates"] == 1  # the failed one can be tried again


def test_when_nothing_works_the_reason_is_shown(client, monkeypatch):
    _with_photo(client, **THIN)

    async def fake_identify(user, photo, hint):
        raise claude.ClaudeError("De API-sleutel is ongeldig. Pas hem aan bij Instellingen.")
    monkeypatch.setattr(claude, "identify_item", fake_identify)
    r = client.post("/api/items/fill-details", headers=H)
    assert r.status_code == 502 and "API-sleutel is ongeldig" in r.json()["detail"]


def test_progress_counts_pieces_not_photos(client, monkeypatch):
    import asyncio
    from app import db, main
    _with_photo(client, **THIN)
    _with_photo(client, **{**THIN, "name": "Tweede jas"})
    steps = []

    async def fake_identify(user, photo, hint):
        claude.report(step="Foto bekijken")  # what the real one reports
        return dict(SEEN)
    monkeypatch.setattr(claude, "identify_item", fake_identify)

    class Shown(dict):  # what the app's progress shows, step by step
        def update(self, changes):
            steps.append(changes["step"])
    with db.connect() as conn:
        user = dict(conn.execute("SELECT * FROM users").fetchone())
    todo = client.get("/api/state").json()["items"]

    async def run():
        claude.PROGRESS.set(Shown())
        return await main._fill_details(user, todo)
    assert asyncio.run(run())["pieces"] == 2
    assert steps == ["Stukken aanvullen: 0 van 2", "Stukken aanvullen: 1 van 2", "Stukken aanvullen: 2 van 2"]


def test_what_could_not_be_seen_is_left_out():
    got = claude.without_guesses({
        "material": "gebreid, materiaal onbekend", "pattern": "effen", "fit": "niet vermeld",
        "kind": "laarzen", "description": "Zwarte midi-jurk met bandjes. Combineert mooi met sandalen; het merk is niet zichtbaar op de foto."})
    assert got["material"] == "gebreid" and got["pattern"] == "effen" and got["fit"] == "" and got["kind"] == "laarzen"
    assert got["description"] == "Zwarte midi-jurk met bandjes. Combineert mooi met sandalen."
    clear = "Kort jasje; de stof oogt als tweed, niet als duidelijke pailletten."
    assert claude.without_guesses({"description": clear})["description"] == clear
    assert claude.without_guesses({"material": "niet met zekerheid te bepalen; glanzende, lederachtige afwerking"})["material"] \
        == "glanzende, lederachtige afwerking"


def test_pieces_filled_in_before_are_cleaned_once(client, monkeypatch):
    from app import main
    filled = _with_photo(client, **THIN)
    mine = add(client, name="Eigen stuk", material="onbekend")  # the user's own words: left alone

    async def fake_identify(user, photo, hint):
        return {**SEEN, "fit": "niet vermeld", "description": "Kort leren jack. Merk niet zichtbaar op de foto."}
    monkeypatch.setattr(claude, "identify_item", fake_identify)
    client.post("/api/items/fill-details", headers=H)
    assert main._clean_recognitions() == 1 and main._clean_recognitions() == 0
    items = {i["id"]: i for i in client.get("/api/state").json()["items"]}
    assert "fit" not in items[filled["id"]] or not items[filled["id"]]["fit"]
    assert items[filled["id"]]["description"] == "Kort leren jack." and items[filled["id"]]["material"] == "leer"
    assert items[mine["id"]]["material"] == "onbekend"
