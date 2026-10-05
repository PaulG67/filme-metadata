from app.jellyfin import JellyfinClient, pick_library_user
from app.scanner import _apply_body


def test_apply_body_keeps_jellyfin_search_result():
    raw = {
        "Name": "Marvel's Inhumans",
        "ProductionYear": 2017,
        "ProviderIds": {"Tmdb": "68716", "Imdb": "tt4154858"},
    }
    body = _apply_body({"name": "andere Anzeige", "year": 1999, "provider_ids": {}, "raw": raw})
    assert body["ProviderIds"]["Tmdb"] == "68716"
    assert body["ProductionYear"] == 2017


def test_apply_body_builds_result_from_tmdb_candidate():
    body = _apply_body(
        {
            "name": "Marvel's Inhumans",
            "year": 2017,
            "overview": "Royal Family",
            "provider_ids": {"Tmdb": "68716"},
            "raw": None,
        }
    )
    assert body["Name"] == "Marvel's Inhumans"
    assert body["ProviderIds"]["Tmdb"] == "68716"
    assert body["SearchProviderName"] == "TheMovieDb"


def test_api_key_goes_in_authorization_header():
    client = JellyfinClient("http://127.0.0.1:9", ' "abc123" ', verify=False)
    assert client.token == "abc123"
    assert client.headers["Authorization"] == (
        'MediaBrowser Client="filme-metadata", Device="unraid", '
        'DeviceId="filme-metadata", Version="1.0.0", Token="abc123"'
    )
    assert "X-Emby-Token" not in client.headers
    assert 'Token="abc123"' in client.ffmpeg_headers()
    client.http.close()


def test_item_read_sends_the_admin_user_id(monkeypatch):
    users = [
        {"Name": "gast", "Id": "guest-id", "Policy": {"IsAdministrator": False}},
        {"Name": "paul", "Id": "admin-id", "Policy": {"IsAdministrator": True}},
    ]
    assert pick_library_user(users, "paul") == "admin-id"
    assert pick_library_user(users, "") == "admin-id"
    client = JellyfinClient("http://127.0.0.1:9", "token", verify=False, username="paul")
    calls = []

    def fake_get(path, params=None):
        calls.append((path, params))
        if path == "/Users":
            return users
        return {"Id": "5b59ee28e3440db7cb677553f7bbe440", "LockData": False}

    monkeypatch.setattr(client, "_get", fake_get)
    item = client.item_record("5b59ee28e3440db7cb677553f7bbe440")
    assert item["LockData"] is False
    assert calls[1] == (
        "/Items/5b59ee28e3440db7cb677553f7bbe440",
        {"userId": "admin-id"},
    )
    client.http.close()


def test_clip_route_serves_only_a_local_preview(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import create_app

    client = create_app().test_client()
    assert client.get("/api/clip/series-1-id").status_code == 404
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "series-1-id.mp4").write_bytes(b"0" * 1200)
    found = client.get("/api/clip/series-1-id")
    assert found.status_code == 200
    assert found.mimetype == "video/mp4"
    assert found.data == b"0" * 1200
    assert client.get("/api/clip/abcd").status_code == 404


def test_pick_trailer_prefers_an_official_german_trailer():
    from app.tmdb import clean_provider_id, pick_trailer

    key = pick_trailer([
        {"site": "YouTube", "type": "Trailer", "key": "english1", "iso_639_1": "en", "official": True},
        {"site": "YouTube", "type": "Trailer", "key": "german01", "iso_639_1": "de", "official": True},
        {"site": "Vimeo", "type": "Trailer", "key": "vimeo123", "official": True},
        {"site": "YouTube", "type": "Teaser", "key": "teaser01", "iso_639_1": "de", "official": True},
    ])
    assert key == "german01"
    assert clean_provider_id("imdb", "4154858") == "tt4154858"
    assert clean_provider_id("tmdb", "68716") == "68716"
    assert clean_provider_id("tmdb", "../etc") == ""


def test_title_lookup_without_key_still_offers_links(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TMDB_API_KEY", "")
    from app.main import create_app

    client = create_app().test_client()
    response = client.get("/api/title?kind=series&tmdb=68716&imdb=tt4154858&tvdb=328844&name=Inhumans&year=2017")
    data = response.get_json()
    assert response.status_code == 200
    assert data["ok"] is True
    assert data["trailer"] is None
    assert "TMDB-API-Key" in data["hint"]
    assert data["links"]["tmdb"] == "https://www.themoviedb.org/tv/68716"
    assert data["links"]["imdb"] == "https://www.imdb.com/title/tt4154858"
    assert data["links"]["tvdb"] == "https://www.thetvdb.com/dereferrer/series/328844"
    icon = client.get("/icon.svg")
    assert icon.status_code == 200
    assert icon.mimetype == "image/svg+xml"
    assert b"<svg" in icon.data


def test_health(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import create_app

    response = create_app().test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
