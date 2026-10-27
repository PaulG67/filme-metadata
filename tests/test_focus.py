from app import scanner as scanner_module
from app.identify import is_collection_name, parse_movie_path
from app.scanner import Scanner, is_collection_finding


def test_collection_folder_is_not_used_as_title():
    assert is_collection_name("James Bond Collection")
    assert is_collection_name("Harry Potter Sammlung")
    assert not is_collection_name("Scandal")
    title, year, _ids = parse_movie_path("/filme/James Bond Collection/Goldfinger.1964.1080p.mkv")
    assert (title, year) == ("Goldfinger", 1964)


def test_old_collection_findings_are_dropped():
    assert is_collection_finding({"folder_title": "Alien Collection", "folder_year": None, "candidates": []})
    assert is_collection_finding(
        {"folder_title": "Alien", "folder_year": 1979, "candidates": [{"name": "Alien Collection"}]}
    )
    assert not is_collection_finding(
        {"folder_title": "The Collection", "folder_year": 2012, "candidates": [{"name": "The Collection"}]}
    )


class FakeClient:
    def __init__(self, has_image=True):
        self.calls = []
        self.has_image = has_image
        self.http = self

    def close(self):
        pass

    def media_item(self, item_id):
        tags = {"Primary": "x"} if self.has_image else {}
        return {
            "Id": item_id,
            "Type": "Series",
            "Name": "Scandal",
            "ProductionYear": 2012,
            "Path": "/serien/Scandal (2012)",
            "ProviderIds": {"Tmdb": "39269"},
            "ImageTags": tags,
        }

    def remote_search(self, kind, title, year, item_id):
        return []

    def remote_posters(self, item_id):
        return [
            {"Url": "https://image.tmdb.org/t/p/original/a.jpg", "ThumbnailUrl": None, "Language": "de", "ProviderName": "TheMovieDb"},
            {"Url": "http://127.0.0.1/evil.jpg", "Language": "en", "ProviderName": "X"},
        ]

    def episodes(self, item_id):
        return []

    def unlock(self, item_id):
        self.calls.append(("unlock", item_id))

    def apply_remote(self, item_id, body):
        self.calls.append(("remote", item_id))

    def refresh(self, item_id, images=True):
        self.calls.append(("refresh", item_id, images))

    def write_fields(self, item_id, values):
        self.calls.append(("write", item_id, values))

    def download_poster(self, item_id, url):
        self.calls.append(("poster", item_id, url))


def _scanner(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JELLYFIN_BASEURL", "http://127.0.0.1:9")
    monkeypatch.setenv("JELLYFIN_TOKEN", "token")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(scanner_module, "_client", lambda settings: fake)
    return Scanner()


def test_searched_title_is_shown_even_when_it_matches(monkeypatch, tmp_path):
    fake = FakeClient(has_image=True)
    scanner = _scanner(monkeypatch, tmp_path, fake)
    result = scanner.focus("scandal")
    assert result["clean"] is True
    assert result["finding"]["manual"] is True
    assert result["finding"]["status"] == "manual"
    assert scanner.status()["focus"]["item_id"] == "scandal"
    assert scanner.status()["findings"] == []
    options = scanner.poster_options("scandal", "")
    assert [option["url"] for option in options] == ["https://image.tmdb.org/t/p/original/a.jpg"]
    assert options[0]["thumb"] == options[0]["url"]
    scanner.clear_focus()
    assert scanner.status()["focus"] is None


def test_chosen_poster_is_written_alone(monkeypatch, tmp_path):
    fake = FakeClient(has_image=False)
    scanner = _scanner(monkeypatch, tmp_path, fake)
    finding = scanner.focus("scandal")["finding"]
    assert finding["status"] == "poster"
    key = finding["candidates"][0]["key"]
    scanner.apply("scandal", key, ["jellyfin"], ["poster"], "https://image.tmdb.org/t/p/w500/b.jpg")
    assert ("poster", "scandal", "https://image.tmdb.org/t/p/w500/b.jpg") in fake.calls
    assert not any(call[0] in {"remote", "write"} for call in fake.calls)
    assert scanner.status()["focus"]["jellyfin_accepted"] is True


def test_full_write_keeps_the_chosen_poster(monkeypatch, tmp_path):
    fake = FakeClient(has_image=False)
    scanner = _scanner(monkeypatch, tmp_path, fake)
    key = scanner.focus("scandal")["finding"]["candidates"][0]["key"]
    scanner.apply("scandal", key, ["jellyfin"], None, "https://image.tmdb.org/t/p/w500/b.jpg")
    assert fake.calls.index(("refresh", "scandal", False)) < fake.calls.index(
        ("poster", "scandal", "https://image.tmdb.org/t/p/w500/b.jpg")
    )


def test_unsafe_poster_is_refused(monkeypatch, tmp_path):
    fake = FakeClient(has_image=False)
    scanner = _scanner(monkeypatch, tmp_path, fake)
    key = scanner.focus("scandal")["finding"]["candidates"][0]["key"]
    try:
        scanner.apply("scandal", key, ["jellyfin"], ["poster"], "http://192.168.1.5/x.jpg")
    except RuntimeError as exc:
        assert "Plakat" in str(exc)
    else:
        raise AssertionError("private poster address was accepted")
