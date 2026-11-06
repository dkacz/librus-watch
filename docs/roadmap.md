# Plan realizacji

Plan budowy programu od fundamentu do pierwszego wydania. Decyzje, na których się opiera, są w [`decisions.md`](decisions.md), projekt obsługi modeli w [`llm.md`](llm.md), pętla zwrotna w [`feedback.md`](feedback.md), a wyniki rozpoznania danych Librusa w [`librus-apix-notes.md`](librus-apix-notes.md).

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
  prefilter.py   # normalizacja, odcisk treści, sito deterministyczne, kolejka i budżet
  llm_cache.py   # wyniki modelu (także puste) po odcisku treści, wersji promptu i modelu
  extract.py     # triage wsadowy i ekstrakcja: schemat JSON, kontrola cytatu, naprawa, sonda trybu JSON
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

## Testy

Cały kod ma testy na trzech poziomach. Etap jest zakończony dopiero z testami wymaganymi dla jego zakresu.

| Poziom | Katalog | Co sprawdza | Od etapu |
|---|---|---|---|
| Jednostkowe | `tests/unit/` | każda funkcja i gałąź, także ścieżki błędów; pokrycie linii i gałęzi `src/` co najmniej 90% | E0 |
| Integracyjne | `tests/integration/` | moduły razem: sesja i `librus-apix` na atrapie transportu HTTP Librusa, adapter, prawdziwa baza SQLite, atrapy modelu i Apprise | E3 |
| End-to-end | `tests/e2e/` | cały cykl przez CLI na lokalnych atrapach serwerów Synergii, API modelu i Apprise: treść powiadomień, stan bazy, brak powtórek, raport o awarii | E7 |
| Na żywo | znaczniki `llm`, `live` | prawdziwy model i prawdziwe konto; uruchamiane ręcznie | E5, E9 |

## Logowanie i obsługa błędów

Obowiązuje w każdym etapie (szczegóły w ADR-006, ADR-007 i ADR-011):

- Poziomy logowania według ADR-011. W logach i komunikatach wyjątków nigdy nie ma treści z Librusa, cytatów, imion i nazwisk, loginów ani sekretów; dziecko to `id` z konfiguracji, błąd to moduł i typ wyjątku.
- Wyjątki bibliotek i sieci zamieniane na klasy programu (`AuthError`, `TransientError`, `SchemaError`, `ModuleUnavailable`, `ConfigError`, `MissingSecretError`); od klasy zależy reakcja: przerwa do jutra, ponowienie z limitem, pominięcie modułu z raportem albo ostrzeżenie.
- Błąd jednej jednostki pracy (moduł API, wiadomość, tekst dla modelu, kanał powiadomień) nie zatrzymuje reszty cyklu. Każde połączenie ma limit czasu.
- Każda ścieżka błędu ma test; testy sprawdzają poziom logu i brak danych prywatnych, a testy end-to-end całe wyjście programu.

## Etapy

Każdy etap kończy się testami (sekcja „Testy”), spełnieniem zasad z sekcji „Logowanie i obsługa błędów”, `ruff`, `mypy`, hookami `pre-commit` i commitem.

| Etap | Zakres | Kryterium odbioru |
|---|---|---|
| ✅ **E0 Fundament** | konfiguracja, sekrety, baza, poprawki obsługi błędów i logów, ADR-011 (poziomy logowania, klasy błędów) | testy regresji dla każdej znalezionej luki |
| **E1 Rozpoznanie, część 3** | drugie konto (starsza klasa: zadania domowe, tematy lekcji); czy pobranie wiadomości i ogłoszeń oznacza je jako przeczytane; czas życia sesji | uzupełnione `librus-apix-notes.md`, decyzja o ponownym użyciu sesji |
| ✅ **E2 Warstwa Librus** | `errors.py`, `session.py`, `gateway.py`, `messages.py`; fixtures syntetyczne odtworzone z kształtów | test kontraktu dla każdego modułu; błąd jednego modułu nie zatrzymuje reszty; 403 → `ModuleUnavailable`, zła odpowiedź → `SchemaError`; sesje dwojga dzieci się nie mieszają |
| ✅ **E3 Modele i baza v2** | `TextItem`, `PlanChange`, `FreeDay`, adapter, migracja schematu 1 → 2, blokada jednej instancji, cache słowników (24 h), rzadsze pobieranie modułów rzadko zmiennych, odciski rekordów do wykrywania zmian, typy treści ignorowane w całości (`ignore_content`, domyślnie oceny, ADR-014) | `fetch --dry-run` wypisuje liczby rekordów per moduł, bez treści; typ z `ignore_content` nie trafia do bazy, a moduł potrzebny tylko jemu nie jest pobierany; test integracyjny: logowanie przez prawdziwy `librus-apix` na atrapie transportu HTTP → moduły API → adapter → baza (także migracja 1 → 2 na bazie z danymi); `fetch` loguje per moduł tylko liczby (INFO) i błędy z klasą (WARNING/ERROR) |
| **E4 Odstępstwa wyliczane** | `baseline.py`, `deviations.py` | scenariusze: odwołana lekcja, zastępstwo, dzień wolny klasy na część lekcji, krótszy dzień, przesunięty sprawdzian, zebranie zdublowane w terminarzu, wpis spoza pobranego zakresu nie znika; testy integracyjne: snapshoty z bazy → odstępstwa przez dwa kolejne cykle; błędny rekord z API pomijany z ostrzeżeniem (id i pole), nie przerywa wyliczeń |
| **E5a Sito i cache** | normalizacja i odcisk treści, `prefilter.py` (reguły per źródło, okno pierwszej synchronizacji), tabela `llm_cache` (także puste wyniki, wspólna dla dzieci), kolejka z priorytetem terminu i dziennym budżetem; bez modelu (`docs/llm.md`) | testy jednostkowe każdej reguły sita, klucza cache i budżetu; na danych z obu kont rozpoznania zmierzona liczba tekstów po sicie dla pierwszej synchronizacji i dla zwykłego dnia |
| **E5b Ekstrakcja** | `extract.py`: prompt ze stałym prefiksem, schemat JSON, sonda możliwości endpointu (cache 24 h), walidacja (cytat, okno dat, pewność) z jedną próbą naprawy, bezpiecznik i dwa limity czasu (zimny start), jeden model na cykl | zestaw ok. 25 wymyślonych tekstów ze wszystkich kanałów (zgoda w treści zadania, kartkówka w kategorii „Inne”, termin poprawy w komentarzu do oceny, brak stroju w uwadze, zbiórka w ogłoszeniu, „w każdy wtorek”, tekst bez terminu); `pytest -m llm`: każdy cytat zweryfikowany, zero jednorazowych wydarzeń z „w każdy wtorek”; testy jednostkowe na atrapie modelu (złe JSON-y, zmyślony cytat, data poza oknem, przekroczony limit) i integracyjne z lokalnym serwerem zgodnym z API OpenAI (liczba zapytań na cykl, trafienia w cache w drugim cyklu); odrzucona odpowiedź logowana jako WARNING bez cytatu i treści; awaria modelu nie blokuje odstępstw wyliczanych |
| **E5c Optymalizacja i wybór modelu** | triage wsadowy tym samym modelem, wsady krótkich tekstów, opcjonalny wybór już załadowanego modelu z listy, pseudonimizacja dla modelu zdalnego; porównanie kandydatów na zestawie oceny | raport z oceny: trafienia, pominięcia, fałszywe pozycje, czas i tokeny per model i wariant; triage nie gubi żadnej pozycji z zestawu oceny; wybrany model domyślny zapisany w ADR |
| **E6 Powiadomienia** | `alerts.py`, `notify.py` | cisza w zwykły dzień, wieczorem tylko nietypowe, rano tylko zmiany, od razu pilne, nic w godzinach ciszy; „ZMIANA”; brak powtórek; `test-notify` dociera; testy integracyjne z lokalną atrapą Apprise (treść, escapowanie, routing po tagach); awaria jednego kanału nie blokuje pozostałych, adres kanału nigdy w logu |
| **E7 Pętla i polecenia** | `scheduler.py`, `cli.py`, limity prób (ADR-007), raport o awarii, `doctor` | `run-once --dry-run` daje sensowne pozycje, drugi przebieg nic nie powtarza; złe hasło: jedna próba, przerwa do jutra, raport; testy end-to-end przez CLI na atrapach serwerów: zwykły dzień (cisza), dzień z odstępstwami, zmiana (ZMIANA), awaria modułu, zły login, restart w trakcie; wyjście programu w e2e bez sekretów, imion i treści z danych testowych |
| **E7a Pętla zwrotna lokalna** | zdarzenia oceny (`confirmed`, `wrong`, `missed`, `late`), `librus-watch review`, audyt w cieniu (próbka odrzuconych przez sito i triage), zgodność odstępstw wyliczonych i odczytanych, raport tygodniowy i metryki lokalne, biblioteka przykładów w prompcie; opcjonalnie bot Telegrama z przyciskami (ADR-016, `docs/feedback.md`) | testy jednostkowe ocen, audytu i doboru przykładów; integracyjne: pominięcie z audytu trafia do przeglądu, ocena trafia do lokalnego zestawu oceny, zmiana promptu gorsza na tym zestawie jest odrzucana; w metrykach i raporcie brak treści |
| **E8 Kontener** | `Containerfile` (obraz bazowy po digeście, bez roota, system plików tylko do odczytu), Quadlet, Compose | budowa na serwerze domowym; restart nie gubi stanu; w logach brak sekretów, nazwisk i treści; zestaw end-to-end uruchomiony w zbudowanym kontenerze |
| **E9 Pilot** | tydzień pracy z `--dry-run` i powiadomieniami do jednej osoby, codzienne porównanie z osią czasu w aplikacji Librus | żadne odstępstwo widoczne na osi czasu nie zostało przegapione, mało fałszywych alarmów |
| **E10 Wydanie** | README (instalacja krok po kroku, przykłady), uzupełnione ADR | — |
| **E11 Wspólny korpus (opt-out)** | domyślnie włączona automatyczna wysyłka zanonimizowanych, ocenionych przykładów (`feedback.share_corpus`, ADR-018): anonimizacja lokalna (słowniki, NER dla polskiego, uogólnienie, „w razie wątpliwości odrzuć”), informacja przy pierwszym uruchomieniu przed pierwszą wysyłką, lokalny dziennik wysłanych przykładów, endpoint zbierania z uwierzytelnieniem i limitem, przegląd po stronie korpusu, wydania z podpisaną paczką zestawu oceny, przykładów i reguł; przegląd prawny przed włączeniem | testy anonimizacji na wymyślonych tekstach z imionami, adresami, telefonami i rzadkimi wydarzeniami: zero przecieków; przykład ryzykowny odrzucany; `share_corpus: false` zatrzymuje wysyłkę od następnego cyklu; bez informacji przy pierwszym uruchomieniu nic nie wychodzi; dziennik zawiera dokładnie to, co wysłano; wkład obniżający wynik na zestawie bazowym nie wchodzi do wydania |
| **E12 Telemetria zbiorcza (opt-in)** | liczby z raportu tygodniowego, zaokrąglone i z szumem, bez identyfikatorów; ten sam kanał co korpus (ADR-017) | test: paczka telemetrii nie zawiera identyfikatorów ani treści; szum i zaokrąglenie zgodne z ustawieniem |

Po wydaniu: wspólny korpus i telemetria (E11, E12) oraz integracja z Google Calendar (osobna iteracja).

## Ryzyka

- **API jest nieoficjalne.** Zmiana kształtu odpowiedzi kończy się `SchemaError` i raportem o awarii, nie ciszą. Kształt zna tylko adapter.
- **Oznaczanie jako przeczytane.** Do sprawdzenia w E1 dla wiadomości i ogłoszeń. Jeśli pobranie oznacza je jako przeczytane, będzie opcja w konfiguracji i ostrzeżenie w README.
- **Liczba zapytań do modelu.** Tematy lekcji, komentarze i terminarz dają dziesiątki tekstów miesięcznie na dziecko. Sito i dzienny limit są w konfiguracji.
- **Różnice między szkołami i klasami.** Np. tematy lekcji w klasach 1–3 mogą być niedostępne. Każdy moduł jest opcjonalny.
- **Regulamin Librusa.** Automatyczne logowanie nie jest oficjalnie wspierane: rzadkie pobrania, limity prób, tylko odczyt.
