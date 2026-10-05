# filme-metadata

Erkennt Serien und Filme, die Jellyfin der falschen Datenbank zugeordnet hat, und setzt auf Klick die richtige ID.

Beispiel: der Ordner heisst `Marvel's Inhumans (2017)`, Jellyfin zeigt **Hotel Inhumans** (2025). Beides enthält „Inhumans“. Es sind zwei verschiedene Werke:

| | Marvel's Inhumans | Hotel Inhumans |
| --- | --- | --- |
| Jahr | 2017 | 2025 |
| TMDB | 68716 | 276288 |
| Staffel 1 | 8 Folgen | 13 Folgen |

Die App schreibt nichts von selbst. In der Web-UI siehst du den Vorschlag und übernimmst ihn einzeln oder alle als sicher markierten Treffer.

## So wird identifiziert

Der **Ordner** sagt, was du hast. Die **Provider-ID** sagt, welches Werk das ist. Der Titel allein reicht nicht.

1. **Serie:** letzter Ordnername, Jahr in Klammern. `Marvel's Inhumans (2017)` oder `Marvel's Inhumans (2017) {tvdb-328844}`. Eine ID in `{tvdb-…}`, `{tmdb-…}` oder `{imdb-tt…}` gilt sofort.
2. **Film:** Ordner `Dune (2021)` schlägt den Dateinamen. So bleiben `Dune (1984)` und `Dune (2021)` getrennt. Eine Zahl im Titel wie `Blade Runner 2049` wird nicht als Erscheinungsjahr gelesen.
3. **Folge:** `S01E03` oder `1x03` im Dateinamen. Der Folgentitel wird nicht zum Abgleich benutzt, weil er übersetzt oder abgeschnitten sein kann.
4. **Vergleich mit Jellyfin:** weicht der angezeigte Titel ab oder liegt das Jahr mehr als ein Jahr daneben, ist der Eintrag verdächtig. Stimmt die vorhandene TMDB-, TVDB- oder IMDb-ID schon mit dem besten Treffer überein, bleibt er unangetastet.
5. **Suche:** Jellyfin-Remote-Search mit dem Ordnernamen, einmal mit Jahr und einmal ohne. Mit optionalem TMDB-Key kommen Staffelfolgenzahlen und die fehlenden IDs dazu.
6. **Bewertung jedes Treffers:**
   - Wörter, die im Ordner fehlen oder im Treffer zu viel sind („Hotel“ zusätzlich, „Marvel's“ fehlt)
   - Jahr: gleich, ein Jahr daneben, oder Widerspruch
   - Folgen: 8 von 8 in Staffel 1 ist ein voller Treffer. Eine Datei `S01E09` kann nicht die 8-teilige Marvel-Serie sein
7. **Sicher** nur bei hohem Score, mindestens 15 Punkten Abstand und ohne Jahreswiderspruch. `Inhumans` ohne Jahr bleibt auf „Prüfen“, auch wenn die Folgenzahl Marvel wahrscheinlicher macht.
8. **Ausschnitt:** Button „Ausschnitt erkennen“ liest die Datei über Jellyfin. Zuerst der OpenSubtitles-Fingerabdruck (Anfang und Ende der Datei, dieselbe Idee wie ein Shazam-Signaturabgleich). Trifft der die Fassung nicht, werden 20 Sekunden Dialog transkribiert und mit den Untertiteln der Kandidaten verglichen. Ein Satz, der nur in einer Fassung vorkommt, entscheidet.

Übernehmen setzt die Provider-IDs und lässt Jellyfin Metadaten und Bilder ersetzen. Bei Serien werden die Folgen danach neu geladen. Der Gesehen-Status liegt in den Benutzerdaten und bleibt.

Ein abweichender Folgenindex (`S01E03` in der Datei, Jellyfin zeigt E01) kann getrennt gesetzt werden.

## Unraid

Ab Unraid 6.10 liegt die Vorlage als User-Template auf dem USB-Stick. Image: `ghcr.io/paulg67/filme-metadata:latest`

### 1. Vorlage installieren (Unraid-Terminal)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/filme-metadata/main/unraid/install-template.sh)
```

### 2. Container anlegen

1. **Docker → Container hinzufügen**
2. Vorlage **filme-metadata**
3. **Jellyfin URL:** `http://172.17.0.1:8096`
4. **Jellyfin API-Key:** Admin-Key aus Dashboard → API-Keys. Die Korrektur braucht Elevation.
5. **TMDB API-Key:** optional, verbessert die Folgenzahl-Prüfung
6. Kein Router-Port. Web-UI nur im LAN: `http://UNRAID-IP:8792`

In Jellyfin das Docker-Netz zu den bekannten LAN-Netzen nehmen, falls die Anmeldung scheitert.

### 3. Prüfen

**Bibliothek prüfen** vergleicht Ordner und Jellyfin-Titel. **Übernehmen** schreibt einen Treffer. **Alle sicheren übernehmen** nur die klaren Fälle.

Sonarr-Ordner mit Jahr, optional mit `{tvdb-ID}`, machen die Entscheidung eindeutig. Die Medienplatte muss nicht in den Container gemountet werden: Jellyfin liefert den Pfad als Text.

## Variablen

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `JELLYFIN_BASEURL` | `http://172.17.0.1:8096` | Jellyfin |
| `JELLYFIN_TOKEN` | leer | Admin-API-Key |
| `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD` | leer | Nur ohne API-Key, Benutzer muss Admin sein |
| `TMDB_API_KEY` | leer | Optionale zweite Quelle |
| `OPENSUBTITLES_API_KEY` | leer | Ausschnitt-Erkennung, Consumer-Key von opensubtitles.com |
| `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD` | leer | Nur für den Dialog-Vergleich |
| `WHISPER_MODEL` | `small` | Sprachmodell, erster Lauf lädt es nach Appdata |
| `PORT` | `8792` | Port im Container |
| `SSL_BYPASS` | `false` | `true` bei selbstsigniertem Zertifikat |
| `PUID` / `PGID` | `99` / `100` | Unraid nobody/users |
| `DATA_DIR` | `/data` | Scan-Stand und Ignorier-Liste |

## Lokal

```bash
docker compose up -d --build
```

API-Key in `docker-compose.yml` oder einer `.env` setzen. `.env` nicht committen.
