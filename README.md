# librus-watch

> **Wersja robocza.** Program jest w budowie i jeszcze nie wysyła powiadomień. Ten opis pokazuje, czym będzie. Szczegóły w sekcji [Na jakim etapie jest projekt](#na-jakim-etapie-jest-projekt).

> Projekt nieoficjalny, niezwiązany z Librus Sp. z o.o. „Librus” i „Synergia” są znakami towarowymi ich właściciela i są tu użyte wyłącznie do opisu, z jakim dziennikiem program współpracuje.

![alt text](image.png)

Powiadomienia z Librusa o tym, że jutro wygląda inaczej niż zwykle.

Nie dostajesz kopii dziennika. Dostajesz krótką wiadomość wtedy, gdy trzeba coś zrobić albo wyjść z domu o innej porze, i to z wyprzedzeniem, a nie pięć minut przed wyjściem. Gdy dzień jest zwykły, program milczy.

## Na jakim etapie jest projekt

_Stan na 2026-11-06. Pełny plan z kryteriami odbioru: [docs/roadmap.md](docs/roadmap.md)._

| Co | Stan |
|---|---|
| Logowanie do Librusa i pobieranie danych (plan lekcji, zmiany planu, dni wolne, terminarz, zadania, ogłoszenia, oceny i komentarze, wiadomości) | gotowe |
| Ochrona konta przy logowaniu (limit, przerwa po odmowie, zapamiętane urządzenie) | gotowe |
| Wykrywanie odstępstw bez sztucznej inteligencji: dzień wolny, inny początek lub koniec lekcji, odwołania, zastępstwa, nowe i przesunięte sprawdziany, wycieczki, zebrania | gotowe |
| Przygotowanie tekstów dla modelu: odrzucanie nieaktualnych i nieistotnych, zapamiętywanie wyników, dzienny limit | gotowe |
| Czytanie wiadomości, ogłoszeń, zadań i komentarzy przez model językowy (zbiórki, rzeczy do przyniesienia, zgody), z kontrolą, że każda informacja ma dosłowny cytat; wybrany najlepszy model lokalny | gotowe (na zestawie testowym 83% trafień, zero wpadek w pułapkach) |
| Powiadomienia: wieczorem o jutrze, rano o zmianach, od razu o pilnych, bez powtórek, z „ZMIANA: było… jest…”; Telegram z przyciskami „Trafne” / „Błędne” i poleceniem `/brak`, inne kanały przez Apprise | gotowe (wysyłane ręcznie poleceniem `brief`) |
| Działanie jako usługa w kontenerze | planowane |
| Tydzień próbny i pierwsze wydanie | planowane |

Dziś można uruchomić `librus-watch fetch --dry-run`: program loguje się, pobiera dane i wypisuje liczbę rekordów, listę wykrytych odstępstw (dzień i rodzaj, bez treści) oraz ile tekstów czekałoby na odczyt przez model. Zwykłe `librus-watch fetch` (bez `--dry-run`) czyta też teksty modelem, jeśli jest włączony w konfiguracji, i zapisuje wyniki w bazie. Powiadomienie wysyła na żądanie: `librus-watch brief evening` (albo `morning`, `instant`; z `--dry-run` tylko wypisuje), a `librus-watch test-notify` wysyła próbne. Samo, w tle i o stałych porach, zacznie działać w kolejnym etapie.

## Przykładowe powiadomienia

```
Jutro (czwartek) Ania:
• zbiórka 7:30 pod szkołą, zwykle lekcje od 8:00
  „w czwartek zbiórka 7:30 pod szkołą, proszę o strój galowy”
• strój galowy
```

```
ZMIANA, Kuba: sprawdzian z matematyki
było: wtorek 12.11, jest: czwartek 14.11
```

```
Kuba, dziś: ostatnia lekcja odwołana, koniec o 12:35 zamiast 13:30
```

## Co program wyłapuje

- **Inny początek albo koniec dnia:** wcześniejsza zbiórka, później pierwsza lekcja, odwołane ostatnie lekcje.
- **Coś do przyniesienia lub załatwienia:** strój, materiały, pieniądze, zgoda do podpisania, płatność.
- **Zmiany wcześniejszych ustaleń:** przesunięty sprawdzian, odwołana wycieczka, zmieniona godzina.
- **Zastępstwa i dni wolne.**

Plan lekcji porównuje z tym, jak zwykle wygląda dany dzień tygodnia. Wiadomości od nauczycieli czyta model językowy (AI), a każda informacja w powiadomieniu ma dosłowny cytat z wiadomości, więc łatwo sprawdzić źródło.

## Kiedy przychodzą powiadomienia

Domyślnie:

- **wieczorem o 19:00:** podsumowanie na jutro, tylko gdy jest coś nietypowego;
- **rano o 6:30:** tylko to, co zmieniło się od wieczora;
- **w ciągu dnia:** od razu, gdy pojawi się coś pilnego na dziś lub jutro;
- **w nocy (21:30–6:15):** nic.

Jeśli program przestanie dostawać dane z Librusa, wyśle raport o awarii. Cisza oznacza więc „nic nietypowego”, a nie „coś się zepsuło”.

Godziny, kanały i to, kto dostaje które powiadomienia, zmienisz w konfiguracji.

## Czego potrzebujesz

- **Loginu rodzica w Synergii dla każdego dziecka.** To login ze szkoły w postaci samych cyfr (np. `1234567`), nie login ucznia z końcówką `u`. Każde dziecko ma osobny login rodzica. Logowanie e-mailem (Konto LIBRUS w aplikacji mobilnej) nie jest obsługiwane.
- **Komputera, który jest zawsze włączony:** domowy serwer, NAS, Raspberry Pi albo serwer w chmurze. Program działa w kontenerze (Podman albo Docker).
- **Kanału powiadomień**, np. bota Telegrama i grupy z drugim rodzicem. Działa też Signal, ntfy, Pushover, e-mail i ponad sto innych (przez [Apprise](https://github.com/caronc/apprise)).
- **Modelu językowego** dostępnego przez API zgodne z OpenAI: lokalnie (llama.cpp, Ollama) albo w chmurze (OpenRouter, OpenAI).

## Warto wiedzieć

- **Prywatność.** Dane zostają w jednym pliku bazy na Twoim komputerze. Jeśli wybierzesz model w chmurze, treść wiadomości ze szkoły (imiona, szczegóły wydarzeń) zostanie wysłana do tej firmy. Model lokalny tego nie robi.
- **Wiadomości oznaczone jako przeczytane.** Librus oznacza wiadomość jako przeczytaną, gdy otworzy ją program. Program pobiera treść każdej wiadomości tylko raz.
- **Regulamin Librusa.** Automatyczne logowanie do Synergii nie jest oficjalnie wspierane przez Librusa. Program loguje się rzadko (domyślnie co godzinę w ciągu dnia), ma limit nieudanych prób i po błędzie logowania czeka do następnego dnia. Używasz go na własną odpowiedzialność.
- **Tylko odczyt.** Program niczego w Librusie nie wysyła ani nie zmienia.
- **Wspólny korpus przykładów: domyślnie włączony (opt-out).** Gdy ta funkcja powstanie (etap E11, jeszcze jej nie ma), program będzie domyślnie i automatycznie wysyłał zanonimizowane przykłady tekstów z ocenami („ten alarm był trafny”, „to przegapiliśmy”), żeby poprawiać działanie u wszystkich użytkowników. Wysyłane są tylko teksty po anonimizacji: imiona, nazwiska, nazwy szkoły, klasy i miejsc, telefony i adresy są zamieniane na znaczniki, daty na określenia względne („jutro”, „czwartek”), a przykład budzący wątpliwości nie jest wysyłany wcale. Nigdy nie są wysyłane surowe wiadomości, loginy ani dane konta. Przed pierwszą wysyłką program wyświetli o tym informację, a każdą wysłaną paczkę zobaczysz w lokalnym dzienniku. **Wyłączenie:** `feedback.share_corpus: false` w `config.yaml`. Telemetria zbiorcza (same liczby, np. ile alarmów było trafnych) będzie działać tylko po włączeniu (opt-in).

## Instalacja

Do uzupełnienia, gdy powstanie obraz kontenera. Wzorce już są:

- `config.example.yaml`: wszystkie ustawienia z opisem;
- `.env.example` i `secrets.example/`: jakie sekrety są potrzebne i jak je podać;
- `deploy/quadlet/librus-watch.container.example`: usługa systemd przez Podman Quadlet;
- `deploy/compose.example.yaml`: Docker Compose.

## Część techniczna

- Python 3.12, [`uv`](https://docs.astral.sh/uv/), scraper [`librus-apix`](https://github.com/poroknights/librus-apix), Apprise, SQLite.
- Decyzje projektowe: [`docs/decisions.md`](docs/decisions.md). Plan realizacji: [`docs/roadmap.md`](docs/roadmap.md).
- Jak pomóc: [`CONTRIBUTING.md`](CONTRIBUTING.md). Zgłaszanie podatności: [`SECURITY.md`](SECURITY.md).
- Sekrety tylko przez zmienne środowiskowe lub pliki (`NAZWA_FILE`). Plik konfiguracyjny odrzuca klucze wyglądające na sekrety, a wartości sekretów są wycinane z logów.

### Rozwój

```sh
uv sync
pre-commit install          # gitleaks, kontrola prywatnych ścieżek, ruff
uv run pytest
uv run ruff check
uv run mypy
```

Repozytorium jest publiczne. Prywatne pliki (`config.yaml`, `.env`, `secrets/`, `data/`, `deploy/local/`, `local/`) są w `.gitignore`, a hook `pre-commit` blokuje ich przypadkowy commit. Dane w testach są wymyślone.

## Licencja

[GPL-3.0-or-later](LICENSE), tak jak biblioteka `librus-apix`.
