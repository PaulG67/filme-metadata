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


def test_health(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import create_app

    response = create_app().test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
