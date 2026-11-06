# Jak pomóc

Dziękujemy za chęć pomocy. Projekt jest dla polskich rodziców, więc dokumentacja, komunikaty i docstringi są po polsku.

## Zanim zaczniesz

- Przeczytaj [`docs/roadmap.md`](docs/roadmap.md) (co i w jakiej kolejności powstaje) i [`docs/decisions.md`](docs/decisions.md) (decyzje projektowe). Zmiana, która zmienia decyzję, zaczyna się od nowego ADR na końcu pliku; starych nie edytujemy.
- Zasady pracy w repo są w [`AGENTS.md`](AGENTS.md). Dotyczą ludzi tak samo jak agentów AI.

## Środowisko

```sh
uv sync
pre-commit install            # gitleaks, kontrola prywatnych ścieżek, ruff
uv run pytest -q              # unit + integration + e2e, próg pokrycia 90%
uv run ruff check
uv run mypy
```

Testy nie łączą się z Librusem ani z modelem: Librus jest podmieniany na poziomie transportu HTTP (`tests/fixtures/librus_http.py`), model na lokalnej atrapie. Testy na prawdziwych usługach (`-m llm`, `-m live`) uruchamia się ręcznie.

## Wymagania dla każdej zmiany

- **Testy na odpowiednim poziomie** w tym samym pull requeście: jednostkowe dla każdej funkcji i ścieżki błędu, integracyjne, gdy zmiana łączy moduły, end-to-end, gdy zmienia zachowanie polecenia.
- **Logowanie i obsługa błędów** według sekcji w `AGENTS.md` i ADR-011: żadnych treści z Librusa, imion, loginów ani sekretów w logach i komunikatach wyjątków; błędy zamieniane na klasy programu; błąd jednego modułu nie zatrzymuje cyklu.
- **Dane testowe tylko wymyślone.** Kształty odpowiedzi Librusa bierzemy z `tests/fixtures/`. Prawdziwe wiadomości ze szkoły, nawet „po zamazaniu”, nie trafiają do repo ani do zgłoszeń.
- **Docstringi w stylu Google, po polsku** (sprawdza ruff).
- **Bez nowych zależności bez uzasadnienia.** Wersje są przypięte w `uv.lock`, a resolver nie bierze wydań z ostatnich dni.

## Zgłoszenia

- **Librus się zmienił** (program zgłasza `SchemaError` dla modułu): podaj moduł i klasę błędu z logu. Bez treści odpowiedzi.
- **Program przegapił odstępstwo albo zgłosił zbędne:** opisz sytuację własnymi słowami albo wklej tekst po usunięciu imion, nazwisk, nazwy szkoły, miejsc i telefonów.
- **Podatność:** nie publicznie, patrz [`SECURITY.md`](SECURITY.md).

## Licencja

Wnosząc zmianę, zgadzasz się na jej udostępnienie na licencji projektu: [GPL-3.0-or-later](LICENSE).
