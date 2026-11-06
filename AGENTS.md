# Zasady pracy w repo librus-watch

## Roadmapa

- Po zakończeniu każdego zadania (etapu) z `docs/roadmap.md` oznacz je jako wykonane: znak `✅` na początku wiersza zadania, czyli w pierwszej komórce tabeli przed nazwą etapu, np. `| ✅ **E0 Fundament** | ...`.
- Etap jest zakończony dopiero wtedy, gdy spełnia kryterium odbioru z roadmapy i przechodzi weryfikację poniżej.
- Te same zmiany statusu nanieś w lokalnym planie (`local/plan.md`, sekcja „Plan realizacji v2”).

## Weryfikacja (przed każdym commitem)

```sh
uv run pytest -q
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
