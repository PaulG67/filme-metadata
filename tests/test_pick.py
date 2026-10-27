from app import scanner as scanner_module
from app.jellyfin import JellyfinClient
from app.scanner import Scanner


def test_write_fields_keeps_unpicked_values_and_other_ids(monkeypatch):
    client = JellyfinClient("http://127.0.0.1:9", "token", verify=False)
    item = {
        "Id": "abc",
        "Name": "Hotel Inhumans",
        "ProductionYear": 2025,
        "Overview": "alt",
        "ProviderIds": {"Tmdb": "276288", "Imdb": "tt9", "Zap2It": "z1"},
    }
    sent = []
    monkeypatch.setattr(client, "item_record", lambda item_id, fields="": dict(item))
    monkeypatch.setattr(client, "_request", lambda method, path, **kwargs: sent.append((method, path, kwargs)))
    client.write_fields("abc", {"year": 2017, "ids": {"Tmdb": "68716", "Imdb": ""}})
    method, path, kwargs = sent[0]
    assert (method, path) == ("POST", "/Items/abc")
    body = kwargs["json"]
    assert body["Name"] == "Hotel Inhumans"
    assert body["Overview"] == "alt"
    assert body["ProductionYear"] == 2017
    assert body["ProviderIds"] == {"Tmdb": "68716", "Zap2It": "z1"}
    client.http.close()


class FakeJellyfin:
    def __init__(self):
        self.calls = []

    def unlock(self, item_id):
        self.calls.append(("unlock", item_id))

    def write_fields(self, item_id, values):
        self.calls.append(("write", item_id, values))

    def download_poster(self, item_id, url):
        self.calls.append(("poster", item_id, url))

    def apply_remote(self, item_id, body):
        self.calls.append(("remote", item_id))

    def refresh(self, item_id):
        self.calls.append(("refresh", item_id))


def _scanner_with_finding(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JELLYFIN_BASEURL", "http://127.0.0.1:9")
    monkeypatch.setenv("JELLYFIN_TOKEN", "token")
    fake = FakeJellyfin()
    monkeypatch.setattr(scanner_module, "_client", lambda settings: fake)
    scanner = Scanner()
    scanner._state["findings"] = [
        {
            "item_id": "abc",
            "kind": "movie",
            "status": "review",
            "jellyfin_name": "Hotel Inhumans",
            "jellyfin_year": 2025,
            "jellyfin_ids": {"Tmdb": "276288"},
            "jellyfin_overview": "alt",
            "jellyfin_original": "",
            "jellyfin_accepted": False,
            "candidates": [
                {
                    "key": "tmdb:68716",
                    "name": "Marvel's Inhumans",
                    "year": 2017,
                    "original_name": "",
                    "provider_ids": {"Tmdb": "68716"},
                    "overview": "neu",
                    "poster": "https://image.tmdb.org/t/p/w500/inhumans.jpg",
                    "auto": False,
                    "raw": None,
                }
            ],
        }
    ]
    return scanner, fake


def test_apply_writes_only_the_picked_fields(monkeypatch, tmp_path):
    scanner, fake = _scanner_with_finding(monkeypatch, tmp_path)
    result = scanner.apply("abc", "tmdb:68716", ["jellyfin"], ["tmdb", "poster"])
    assert result["fields"] == ["tmdb", "poster"]
    assert ("write", "abc", {"ids": {"Tmdb": "68716"}}) in fake.calls
    assert ("poster", "abc", "https://image.tmdb.org/t/p/w500/inhumans.jpg") in fake.calls
    assert not any(call[0] == "remote" for call in fake.calls)
    accepted = scanner._state["accepted"]["jellyfin"]["abc"]
    assert accepted == scanner_module.metadata_signature("Hotel Inhumans", 2025, {"Tmdb": "68716"})


def test_apply_without_choice_takes_the_whole_edition(monkeypatch, tmp_path):
    scanner, fake = _scanner_with_finding(monkeypatch, tmp_path)
    scanner.apply("abc", "tmdb:68716", ["jellyfin"])
    assert ("remote", "abc") in fake.calls
    assert not any(call[0] == "write" for call in fake.calls)
