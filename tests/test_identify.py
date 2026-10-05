from app.identify import (
    Candidate,
    build_finding,
    merge_candidates,
    parse_episode_index,
    parse_movie_path,
    parse_series_path,
)

MARVEL_IDS = {"tmdb": "68716", "imdb": "tt4154858", "tvdb": "328844"}
HOTEL_IDS = {"tmdb": "276288"}


def marvel() -> Candidate:
    return Candidate(
        name="Marvel's Inhumans",
        year=2017,
        provider_ids=dict(MARVEL_IDS),
        seasons={1: 8},
        source="tmdb",
    )


def hotel() -> Candidate:
    return Candidate(
        name="Hotel Inhumans",
        original_name="ホテル・インヒューマンズ",
        year=2025,
        provider_ids=dict(HOTEL_IDS),
        seasons={1: 13},
        source="tmdb",
    )


def eight_episodes() -> list[tuple[int, int]]:
    return [(1, number) for number in range(1, 9)]


def test_series_folder_year_and_tvdb_id():
    title, year, ids = parse_series_path(
        "/mnt/user/serien/Marvel's Inhumans (2017) {tvdb-328844}"
    )
    assert title == "Marvel's Inhumans"
    assert year == 2017
    assert ids["tvdb"] == "328844"


def test_episode_index_from_filename():
    path = (
        "/mnt/user/serien/Marvel's Inhumans (2017)/Season 01/"
        "Marvel's Inhumans - S01E03 - Divide and Conquer.mkv"
    )
    assert parse_episode_index(path) == (1, 3)
    assert parse_episode_index("Show.Name.1x02.1080p.mkv") == (1, 2)


def test_movie_prefers_folder_year_over_scene_name():
    title, year, ids = parse_movie_path(
        "/mnt/user/filme/Dune (2021)/Dune.2021.1080p.BluRay.mkv"
    )
    assert title == "Dune"
    assert year == 2021
    assert ids == {}


def test_movie_keeps_title_year_like_2049():
    title, year, _ids = parse_movie_path("/mnt/user/filme/Blade Runner 2049 (2017)/Blade Runner 2049.mkv")
    assert title == "Blade Runner 2049"
    assert year == 2017


def test_matching_series_is_not_flagged():
    finding = build_finding(
        folder_title="Marvel's Inhumans",
        folder_year=2017,
        jellyfin_name="Marvel's Inhumans",
        jellyfin_year=2017,
        jellyfin_ids=MARVEL_IDS,
        candidates=[marvel(), hotel()],
        episodes=eight_episodes(),
    )
    assert finding is None


def test_hotel_inhumans_is_replaced_by_marvel():
    finding = build_finding(
        folder_title="Marvel's Inhumans",
        folder_year=2017,
        jellyfin_name="Hotel Inhumans",
        jellyfin_year=2025,
        jellyfin_ids=HOTEL_IDS,
        candidates=[hotel(), marvel()],
        episodes=eight_episodes(),
    )
    assert finding is not None
    assert finding["status"] == "sure"
    best = finding["candidates"][0]
    assert best["name"] == "Marvel's Inhumans"
    assert best["auto"] is True
    assert best["provider_ids"]["Tmdb"] == "68716"
    hotel_hit = finding["candidates"][1]
    assert hotel_hit["auto"] is False
    assert best["score"] - hotel_hit["score"] >= 0.15
    assert any("Hotel" in note for note in hotel_hit["notes"])
    assert any("2025" in note for note in hotel_hit["notes"])


def test_short_folder_name_without_year_is_not_automatic():
    finding = build_finding(
        folder_title="Inhumans",
        folder_year=None,
        jellyfin_name="Hotel Inhumans",
        jellyfin_year=2025,
        jellyfin_ids=HOTEL_IDS,
        candidates=[hotel(), marvel()],
        episodes=eight_episodes(),
    )
    assert finding is not None
    assert finding["status"] == "review"
    assert all(not item["auto"] for item in finding["candidates"])


def test_same_provider_id_is_not_a_mismatch():
    finding = build_finding(
        folder_title="Marvel's Inhumans",
        folder_year=2017,
        jellyfin_name="Inhumans",
        jellyfin_year=2017,
        jellyfin_ids=MARVEL_IDS,
        candidates=[marvel(), hotel()],
        episodes=eight_episodes(),
    )
    assert finding is None


def test_same_title_different_year_picks_the_folder_year():
    dune_1984 = Candidate(name="Dune", year=1984, provider_ids={"tmdb": "841"})
    dune_2021 = Candidate(name="Dune", year=2021, provider_ids={"tmdb": "438631"})
    finding = build_finding(
        folder_title="Dune",
        folder_year=2021,
        jellyfin_name="Dune",
        jellyfin_year=1984,
        jellyfin_ids={"tmdb": "841"},
        candidates=[dune_1984, dune_2021],
    )
    assert finding is not None
    assert finding["status"] == "sure"
    assert finding["candidates"][0]["year"] == 2021
    assert finding["candidates"][0]["auto"] is True
    assert finding["candidates"][1]["auto"] is False


def test_embedded_tvdb_id_pins_the_series():
    finding = build_finding(
        folder_title="Marvel's Inhumans",
        folder_year=2017,
        jellyfin_name="Hotel Inhumans",
        jellyfin_year=2025,
        jellyfin_ids=HOTEL_IDS,
        embedded_ids={"tvdb": "328844"},
        candidates=[hotel(), marvel()],
        episodes=eight_episodes(),
    )
    assert finding is not None
    assert finding["candidates"][0]["provider_ids"]["Tvdb"] == "328844"
    assert finding["candidates"][0]["auto"] is True
    assert any("Ordnernamen" in note for note in finding["candidates"][0]["notes"])


def test_merge_links_imdb_hit_with_tmdb_hit_of_the_same_show():
    from_jellyfin = Candidate(
        name="Marvel's Inhumans",
        year=2017,
        provider_ids={"imdb": "tt4154858"},
        source="jellyfin",
    )
    from_tmdb = Candidate(
        name="Marvel's Inhumans",
        year=2017,
        provider_ids={"tmdb": "68716"},
        seasons={1: 8},
        source="tmdb",
    )
    merged = merge_candidates([from_jellyfin, from_tmdb, hotel()])
    assert len(merged) == 2
    marvel_hit = next(item for item in merged if item.year == 2017)
    assert marvel_hit.provider_ids["imdb"] == "tt4154858"
    assert marvel_hit.provider_ids["tmdb"] == "68716"
    assert marvel_hit.seasons == {1: 8}


def test_ninth_episode_rejects_the_eight_episode_show():
    episodes = eight_episodes() + [(1, 9)]
    finding = build_finding(
        folder_title="Inhumans",
        folder_year=None,
        jellyfin_name="Marvel's Inhumans",
        jellyfin_year=2017,
        jellyfin_ids=MARVEL_IDS,
        candidates=[marvel(), hotel()],
        episodes=episodes,
    )
    assert finding is not None
    marvel_hit = next(item for item in finding["candidates"] if item["name"] == "Marvel's Inhumans")
    assert marvel_hit["episode_score"] == 0
    assert marvel_hit["auto"] is False
