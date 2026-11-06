# Zasady pracy w repo librus-watch

## Roadmapa

- Po zakończeniu każdego zadania (etapu) z `docs/roadmap.md` oznacz je jako wykonane: znak `✅` na początku wiersza zadania, czyli w pierwszej komórce tabeli przed nazwą etapu, np. `| ✅ **E0 Fundament** | ...`.
- Etap jest zakończony dopiero wtedy, gdy spełnia kryterium odbioru z roadmapy i przechodzi weryfikację poniżej.
- Te same zmiany statusu nanieś w lokalnym planie (`local/plan.md`, sekcja „Plan realizacji v2”).

## Testy: trzy poziomy, każdy kod pokryty

Każdy nowy lub zmieniony kod ma testy na odpowiednim poziomie, w tym samym commicie:

- **Jednostkowe** (`tests/unit/`): każda funkcja i gałąź, także ścieżki błędów. Pokrycie linii i gałęzi całego `src/` co najmniej 90% (`fail_under` w `pyproject.toml`); nowy moduł nie obniża pokrycia.
- **Integracyjne** (`tests/integration/`): kilka modułów razem na prawdziwej bazie SQLite w katalogu tymczasowym. Librus podmieniony na poziomie transportu HTTP (prawdziwy `librus-apix` i nasza sesja), model i Apprise na lokalnych atrapach. Obowiązkowe, gdy etap łączy moduły (od E3).
- **End-to-end** (`tests/e2e/`): cały cykl przez CLI (`run-once --dry-run` i z wysyłką) na lokalnych atrapach serwerów Synergii, API modelu i Apprise; sprawdzamy powiadomienia na wyjściu, stan bazy i brak powtórek. Obowiązkowe od E7.
- Testy na prawdziwych usługach mają znaczniki `llm` i `live` i nie uruchamiają się domyślnie.
- Dane w testach są wymyślone. Kształty odpowiedzi Librusa bierzemy z `tests/fixtures/` (odtworzone z rozpoznania).
- Etap z roadmapy nie dostaje ✅ bez testów wymaganych dla jego poziomu.

## Weryfikacja (przed każdym commitem)

```sh
uv run pytest -q              # unit + integration + e2e, z pokryciem (próg 90%)
uv run pytest -m integration --no-cov  # jeden poziom (próg pokrycia liczy się tylko dla całości)
uv run ruff check
uv run mypy
pre-commit run --all-files
```

## Repo jest publiczne

- Prywatne pliki (`config.yaml`, `.env`, `secrets/`, `data/`, `deploy/local/`, `local/`) są w `.gitignore`; hook `pre-commit` blokuje ich commit oraz wyrażenia z `local/forbidden-terms.txt`.
- Dokumenty w `docs/` są neutralne: bez nazw serwerów, imion, loginów i adresów. Szczegóły lokalne trafiają do `local/plan.md` (źródło prawdy planu).
- Sekretów nie czytamy ani nie wypisujemy; skrypty dostają je przez `NAZWA_FILE`.
- Dane z Librusa w logach i na ekranie: tylko liczby, nazwy pól i typy błędów (ADR-011).

## Konwencje

- Docstringi w stylu Google, po polsku, dla każdego modułu, klasy i funkcji (wymusza ruff `D`).
- Commity lokalne; push tylko na wyraźne polecenie.
- Decyzje: nowe ADR na końcu `docs/decisions.md`, starych nie zmieniamy.
