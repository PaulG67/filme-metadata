from app.identify import Candidate, build_finding, merge_candidates
from app.jellyfin import candidate_from_jellyfin
from app.posters import poster_url, tmdb_poster
from app.tmdb import TmdbClient


def test_poster_url_rejects_local_targets_and_keeps_public_https():
    assert poster_url("file:///etc/passwd") == ""
    assert poster_url("http://127.0.0.1/poster.jpg") == ""
    assert poster_url("http://localhost/poster.jpg") == ""
    assert poster_url("http://10.1.1.1/a.jpg") == ""
    assert poster_url("http://image.tmdb.org/t/p/w500/abc.jpg") == "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert poster_url("https://image.tmdb.org/t/p/w500/abc.jpg") == "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert tmdb_poster("/abc.jpg") == "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert tmdb_poster("../abc.jpg") == ""


def test_jellyfin_image_url_becomes_the_public_poster():
    candidate = candidate_from_jellyfin(
        {
            "Name": "Marvel's Inhumans",
            "ProductionYear": 2017,
            "ProviderIds": {"Tmdb": "68716", "Imdb": "tt4154858"},
            "ImageUrl": "http://image.tmdb.org/t/p/original/abc.jpg",
        }
    )
    finding = build_finding(
        folder_title="Marvel's Inhumans",
        folder_year=2017,
        jellyfin_name="Hotel Inhumans",
        jellyfin_year=2025,
        jellyfin_ids={"tmdb": "276288"},
        candidates=[candidate],
        episodes=[(1, number) for number in range(1, 9)],
    )
    assert finding is not None
    assert finding["candidates"][0]["poster"] == "https://image.tmdb.org/t/p/original/abc.jpg"


def test_tmdb_poster_path_is_kept_when_candidates_merge():
    client = TmdbClient("test-key")
    try:
        movie = client._movie({"id": 438631, "title": "Dune", "release_date": "2021-10-22", "poster_path": "/dune.jpg"})
    finally:
        client.http.close()
    other = Candidate(name="Dune", year=2021, provider_ids={"tmdb": "438631"}, source="jellyfin")
    merged = merge_candidates([other, movie])
    assert merged[0].poster == "https://image.tmdb.org/t/p/w500/dune.jpg"


def test_poster_route_rejects_odd_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JELLYFIN_BASEURL", "http://127.0.0.1:9")
    monkeypatch.setenv("JELLYFIN_TOKEN", "token")
    from app.main import create_app

    client = create_app().test_client()
    assert client.get("/api/poster/bad_id").status_code == 404
