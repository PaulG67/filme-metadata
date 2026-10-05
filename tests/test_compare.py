from app.identify import metadata_signature, systems_differ
from app.plex import PlexIndex, direct_guid, folder_keys, pick_match_guid
from app.scanner import still_open


def test_signature_uses_provider_ids_not_display_title():
    left = metadata_signature("Hotel Inhumans", 2025, {"Tmdb": "276288", "Imdb": "tt1"})
    right = metadata_signature("Hotel Inhumans (DE)", 2025, {"imdb": "tt1", "tmdb": "276288"})
    assert left == right
    changed = metadata_signature("Marvel's Inhumans", 2017, {"Tmdb": "68716"})
    assert left != changed


def test_systems_differ_on_distinct_ids():
    assert systems_differ("Hotel Inhumans", 2025, {"Tmdb": "276288"}, "Marvel's Inhumans", 2017, {"Tmdb": "68716"})
    assert systems_differ("Marvel's Inhumans", 2017, {"Imdb": "tt4154858"}, "Inhumans", 2017, {"Imdb": "tt4154858"}) is None


def test_folder_match_ignores_wrong_display_title():
    library = PlexIndex(
        [
            {
                "item_id": "9",
                "kind": "series",
                "name": "Marvel's Inhumans",
                "year": 2017,
                "ids": {"Tmdb": "68716"},
                "folders": folder_keys("/data/serien/Marvel's Inhumans (2017)/Season 01/S01E01.mkv"),
            },
            {
                "item_id": "10",
                "kind": "series",
                "name": "Hotel Inhumans",
                "year": 2025,
                "ids": {"Tmdb": "276288"},
                "folders": folder_keys("/data/serien/Hotel Inhumans (2025)"),
            },
        ]
    )
    found = library.match(
        kind="series",
        path="/mnt/user/serien/Marvel's Inhumans (2017)",
        folder_title="Marvel's Inhumans",
        folder_year=2017,
    )
    assert found["item_id"] == "9"


def test_same_folder_name_does_not_cross_years():
    library = PlexIndex(
        [
            {
                "item_id": "old",
                "kind": "movie",
                "name": "Dune",
                "year": 1984,
                "ids": {"Tmdb": "841"},
                "folders": folder_keys("/filme/Dune (1984)/Dune.mkv"),
            }
        ]
    )
    assert library.match(kind="movie", path="/media/Dune (2021)/Dune.mkv", folder_title="Dune", folder_year=2021) is None


def test_pick_match_guid_prefers_result_containing_imdb():
    assert pick_match_guid(["plex://movie/abc", "plex://movie/tt4154858"], {"Imdb": "tt4154858"}) == "plex://movie/tt4154858"
    assert pick_match_guid([], {"Tmdb": "68716"}) == direct_guid({"Tmdb": "68716"})
    assert direct_guid({"Tmdb": "68716"}) == "tmdb://68716"


def test_accepted_jellyfin_stays_open_while_plex_differs():
    finding = {
        "jellyfin_accepted": True,
        "jellyfin_name": "Marvel's Inhumans",
        "jellyfin_year": 2017,
        "jellyfin_ids": {"Tmdb": "68716"},
        "plex": {"accepted": False, "name": "Hotel Inhumans", "year": 2025, "ids": {"Tmdb": "276288"}},
    }
    assert still_open(finding) is True
    finding["plex"]["accepted"] = True
    assert still_open(finding) is False
