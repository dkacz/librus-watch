# Plan realizacji

Plan budowy programu od fundamentu do pierwszego wydania. Decyzje, na których się opiera, są w [`decisions.md`](decisions.md), a wyniki rozpoznania danych Librusa w [`librus-apix-notes.md`](librus-apix-notes.md).

## Założenia

- Dane strukturalne pochodzą z JSON-owego API Synergii (`/gateway/api/2.0`), w tej samej sesji co logowanie (ADR-012). Biblioteka `librus-apix` służy do logowania i treści wiadomości.
- Każde dziecko to osobne konto rodzica w Synergii i osobna sesja (ADR-010).
- Odstępstwa w polach strukturalnych (odwołania, zastępstwa, dni wolne, zebrania, wycieczki) liczy program. Każdy wolny tekst, niezależnie od modułu, czyta model językowy z kontrolą cytatu (ADR-003, ADR-012).
- Powiadamiamy tylko o odstępstwach (ADR-001), w rytmie z ADR-005.

## Architektura

```
src/librus_watch/
  config.py      # konfiguracja bez sekretów; włączanie modułów, budżet modelu, sito
  secret_env.py  # sekrety z env i plików, redakcja logów
  logs.py        # zasada poziomów logowania (bez treści i nazwisk)
  errors.py      # klasy błędów: AuthError, TransientError, SchemaError, ModuleUnavailable
  librus/
    session.py   # logowanie, własny pojemnik na ciasteczka, limity czasu, odświeżanie tokenu
    gateway.py   # GET /gateway/api/2.0/<moduł>, walidacja JSON, normalizacja Id, słowniki z cache
    messages.py  # lista wiadomości i treść pobierana raz na wiadomość
    adapter.py   # JSON -> modele domenowe; jedyne miejsce znające kształt API
  models.py      # Lesson, PlanChange, FreeDay, Entry, TextItem, Finding
  store.py       # SQLite: snapshoty, teksty, zmiany planu, dni wolne, słowniki, stan pobierania
  baseline.py    # norma dnia z planu stałego (TimetableEntries) i historii planu
  deviations.py  # odstępstwa wyliczane przez program
  prefilter.py   # sito przed modelem: tylko nowe lub zmienione, data albo słowo-klucz, limit znaków
  extract.py     # model: tekst -> lista odstępstw (schemat JSON, kontrola cytatu)
  alerts.py      # rytm powiadomień, jedno podsumowanie dla wszystkich dzieci, „ZMIANA: było/jest”
  notify.py      # Apprise, escapowanie, kierowanie po tagach
  scheduler.py   # pętla, okna pobrań, godziny podsumowań, nadrabianie, raport o awarii
  cli.py         # run, run-once, fetch, brief, test-notify, doctor, --dry-run
```

### Cykl pracy (dla każdego dziecka osobno)

1. Logowanie albo ponowne użycie działającej sesji.
2. Moduły API, każdy osobno: błąd jednego (np. 403, gdy szkoła go nie używa) to ostrzeżenie, nie awaria.
3. Wiadomości: lista, treść tylko nowych.
4. Zapis snapshotów (historia tylko przybywa, ADR-008).
5. Odstępstwa wyliczane przez program.
6. Sito, potem model dla nowych tekstów.
7. Powiadomienia według rytmu, wspólne dla wszystkich dzieci, i wysyłka.

### Moduły API i ich rola

| Moduł | Rola |
|---|---|
| `Timetables`, `TimetableEntries`, `Calendars/Substitutions` | plan, norma dnia, odwołania i zastępstwa |
| `ClassFreeDays`, `SchoolFreeDays` | dni wolne (także na część lekcji) |
| `HomeWorks` | terminarz: daty i treść wpisów |
| `HomeWorkAssignments` | zadania domowe: treść dla modelu |
| `SchoolNotices` | ogłoszenia: treść dla modelu |
| `Notes` | uwagi: treść dla modelu |
| `Grades`, `Grades/Comments` | komentarze do ocen: treść dla modelu |
| `Realizations` | tematy lekcji (gdy szkoła je podaje): treść dla modelu |
| `ParentTeacherConferences`, `SchoolTrips` | zebrania i wycieczki |
| `Timetables/OtherActivitiesRegister` | zajęcia dodatkowe, także odwołane |
| `Subjects`, `Users`, `Classes`, kategorie | słowniki do łączenia odwołań |

Wolne teksty dla modelu: wiadomość, ogłoszenie, wpis w terminarzu, zadanie domowe, komentarz do oceny, uwaga, temat lekcji, zebranie, wycieczka.

## Etapy

Każdy etap kończy się testami, `ruff`, `mypy`, hookami `pre-commit` i commitem.

| Etap | Zakres | Kryterium odbioru |
|---|---|---|
| ✅ **E0 Fundament** | konfiguracja, sekrety, baza, poprawki obsługi błędów i logów, ADR-011 (poziomy logowania, klasy błędów) | testy regresji dla każdej znalezionej luki |
| **E1 Rozpoznanie, część 3** | drugie konto (starsza klasa: zadania domowe, tematy lekcji); czy pobranie wiadomości i ogłoszeń oznacza je jako przeczytane; czas życia sesji | uzupełnione `librus-apix-notes.md`, decyzja o ponownym użyciu sesji |
| **E2 Warstwa Librus** | `errors.py`, `session.py`, `gateway.py`, `messages.py`; fixtures syntetyczne odtworzone z kształtów | test kontraktu dla każdego modułu; błąd jednego modułu nie zatrzymuje reszty; 403 → `ModuleUnavailable`, zła odpowiedź → `SchemaError`; sesje dwojga dzieci się nie mieszają |
| **E3 Modele i baza v2** | `TextItem`, `PlanChange`, `FreeDay`, adapter, migracja schematu 1 → 2, blokada jednej instancji | `fetch --dry-run` wypisuje liczby rekordów per moduł, bez treści |
| **E4 Odstępstwa wyliczane** | `baseline.py`, `deviations.py` | scenariusze: odwołana lekcja, zastępstwo, dzień wolny klasy na część lekcji, krótszy dzień, przesunięty sprawdzian, zebranie zdublowane w terminarzu, wpis spoza pobranego zakresu nie znika |
| **E5 Model** | `prefilter.py`, `extract.py`, prompt po polsku, tryb zapasowy `json_object` | zestaw ok. 25 wymyślonych tekstów ze wszystkich kanałów (zgoda w treści zadania, kartkówka w kategorii „Inne”, termin poprawy w komentarzu do oceny, brak stroju w uwadze, zbiórka w ogłoszeniu, „w każdy wtorek”, tekst bez terminu); `pytest -m llm` na lokalnym modelu: każdy cytat zweryfikowany, zero jednorazowych wydarzeń z „w każdy wtorek”, zmierzona liczba zapytań dziennie po sicie |
| **E6 Powiadomienia** | `alerts.py`, `notify.py` | cisza w zwykły dzień, wieczorem tylko nietypowe, rano tylko zmiany, od razu pilne, nic w godzinach ciszy; „ZMIANA”; brak powtórek; `test-notify` dociera |
| **E7 Pętla i polecenia** | `scheduler.py`, `cli.py`, limity prób (ADR-007), raport o awarii, `doctor` | `run-once --dry-run` daje sensowne pozycje, drugi przebieg nic nie powtarza; złe hasło: jedna próba, przerwa do jutra, raport |
| **E8 Kontener** | `Containerfile` (obraz bazowy po digeście, bez roota, system plików tylko do odczytu), Quadlet, Compose | budowa na serwerze domowym; restart nie gubi stanu; w logach brak sekretów, nazwisk i treści |
| **E9 Pilot** | tydzień pracy z `--dry-run` i powiadomieniami do jednej osoby, codzienne porównanie z osią czasu w aplikacji Librus | żadne odstępstwo widoczne na osi czasu nie zostało przegapione, mało fałszywych alarmów |
| **E10 Wydanie** | README (instalacja krok po kroku, przykłady), uzupełnione ADR | — |

Po wydaniu: integracja z Google Calendar (osobna iteracja).

## Ryzyka

- **API jest nieoficjalne.** Zmiana kształtu odpowiedzi kończy się `SchemaError` i raportem o awarii, nie ciszą. Kształt zna tylko adapter.
- **Oznaczanie jako przeczytane.** Do sprawdzenia w E1 dla wiadomości i ogłoszeń. Jeśli pobranie oznacza je jako przeczytane, będzie opcja w konfiguracji i ostrzeżenie w README.
- **Liczba zapytań do modelu.** Tematy lekcji, komentarze i terminarz dają dziesiątki tekstów miesięcznie na dziecko. Sito i dzienny limit są w konfiguracji.
- **Różnice między szkołami i klasami.** Np. tematy lekcji w klasach 1–3 mogą być niedostępne. Każdy moduł jest opcjonalny.
- **Regulamin Librusa.** Automatyczne logowanie nie jest oficjalnie wspierane: rzadkie pobrania, limity prób, tylko odczyt.
