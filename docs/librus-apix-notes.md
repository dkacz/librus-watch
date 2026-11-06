# `librus-apix` 1.6.0: co zwraca dla konta rodzica

Wyniki spike'u na prawdziwym koncie rodzica (szkoła podstawowa, klasa 1–3, 2026-11-06). Opisana jest wyłącznie struktura. Przykłady wartości są wymyślone.

Zakres spike'u: jedno logowanie, tylko funkcje do odczytu, bez otwierania treści wiadomości. Wszystkie sekcje pobrały się bez błędów i bez przekroczenia limitu czasu 30 s.

## Klient

- `Client(Token(), extra_cookies=RequestsCookieJar())`: własny pojemnik na ciasteczka jest konieczny, bo domyślny argument jest współdzielony przez wszystkie klienty (ADR-006).
- Biblioteka nie ustawia limitu czasu żądań. Adapter podaje go przez `functools.partial(session.request, timeout=...)`.
- Logowanie idzie przez `api.librus.pl/OAuth/Authorization?client_id=46` loginem i hasłem z Synergii.

## Plan lekcji: `timetable.get_timetable(client, monday)`

- Zwraca 7 list (pon–nd), w każdej wszystkie sloty dnia, także puste (`subject == ""`). Sloty mają numery od 0 (lekcja zerowa, np. 7:40).
- Pola `Period`: `subject`, `teacher_and_classroom`, `date` (`YYYY-MM-DD`), `date_from` i `date_to` (`HH:MM`), `weekday`, `info`, `number`, `next_recess_from`, `next_recess_to`.
- **Odstępstwa są w `info` jako klucze ze pustą wartością**, np. `{"dzień wolny szkoły": ""}` albo `{"nieobecność klasy 1x SP": ""}`. Dzień wolny nie usuwa lekcji z planu: lekcje zostają z przedmiotem i adnotacją.
- Spike nie trafił na zastępstwo ani odwołaną lekcję. Format tych adnotacji jest nieznany; adapter musi traktować każdy niepusty `info` jako odstępstwo (`LessonStatus.other`, tekst w `note`) i rozpoznawać znane słowa (`odwołan`, `zastępstw`, `dzień wolny`, `nieobecność klasy`).

## Terminarz: `schedule.get_schedule(client, "MM", "YYYY")`

- Zwraca słownik `dzień miesiąca (int) -> lista Event`. Pola: `title`, `subject`, `data`, `day` (dzień miesiąca jako tekst), `number`, `hour`, `href`.
- Brakujące wartości to napis `"unknown"`, nie `None` (`number`, `hour`, `data["Nauczyciel"]`, `data["Opis"]`).
- `href` daje stabilny identyfikator tylko dla części wpisów (`szczegoly_wolne/1234567`); inne mają `href == ""`. Klucz wpisu: id z `href`, a gdy go nie ma, skrót z dnia, tytułu i przedmiotu.
- Rodzaje wpisów widziane w spike'u: dzień wolny („Dzień wolny: …”), nieobecność nauczyciela, nieobecność klasy („Od lekcji: 0 do 4”), wycieczka lub wyjście.
- **Parser rozbija opis na klucze `data`.** Kolejne linie opisu trafiają do kluczy słownika, np. `{"Zbiórka": "...", "Powrót ok. 12.00": "", "Proszę o zgodę …": ""}`, razem z imieniem i nazwiskiem nauczyciela. Adapter nie może polegać na strukturze `data`: składa klucze i wartości w jeden tekst opisu i przekazuje go do modelu jako treść, tak jak wiadomość.
- To w terminarzu (wpisy wycieczek) są informacje kluczowe dla programu: zbiórka, godzina powrotu, zgoda, koszt, co zabrać.

## Prace domowe: `homework.get_homework(client, "YYYY-MM-DD", "YYYY-MM-DD")`

- Dla zakresu −7…+21 dni wynik był pusty. W klasach 1–3 szkoła może nie używać tego modułu. Do sprawdzenia na koncie starszego dziecka.

## Ogłoszenia: `announcements.get_announcements(client)`

- Lista `Announcement` z pełną treścią od razu (`title`, `author`, `description`, `date` jako `YYYY-MM-DD`). Brak identyfikatora: klucz to skrót z daty, autora i tytułu.
- Wszystkie ogłoszenia szkoły w jednym wywołaniu (w spike'u 17), większość nie dotyczy dnia dziecka (wyniki konkursów).

## Wiadomości: `messages.get_received(client, page)`

- Strona ma do 50 wiadomości. `get_max_page_number` zwraca numer ostatniej strony liczony od 0.
- Pola `Message`: `author` (z rolą w nawiasie kwadratowym, np. `[Wychowawca]`), `title` (ze spacją na początku), `date` (`YYYY-MM-DD HH:MM:SS`), `href` (numeryczne id, stabilny klucz), `unread`, `has_attachment`.
- Lista nie zawiera treści. Treść daje `message_content`, którego spike nie wywołał.
- **Do sprawdzenia:** czy `message_content` oznacza wiadomość jako przeczytaną. W chwili spike'u wszystkie wiadomości na 1. stronie były przeczytane, więc test wymaga nowej, nieprzeczytanej wiadomości.

## JSON-owe API Synergii (`/gateway/api/2.0`), spike część 2

Ta sama sesja, po `client.refresh_oauth()` (ciasteczko `oauth_token`). Wszystkie sprawdzone moduły odpowiedziały 200 na dwóch kontach rodzica (ADR-012). Liczby z konta dziecka z klasy 1–3, listopad; różnice na koncie dziecka ze starszej klasy opisane niżej:

| Moduł | Rekordy | Najważniejsze pola | Uwagi |
|---|---|---|---|
| `Timetables?weekStart=` | 23–33 lekcje / tydzień | `LessonNo`, `HourFrom`, `HourTo`, `IsCanceled`, `IsSubstitutionClass`, `SubstitutionNote`, `Subject.Id`, `Teacher.Id` | Słownik data → sloty → lekcje. Dzień wolny szkoły **nie** ustawia `IsCanceled`. |
| `Calendars/Substitutions` | 31 | `OrgDate`, `OrgLessonNo`, `IsCancelled`, `IsShifted`, `Date`, `LessonNo`, `Subject`, `Teacher` | Cała historia zmian planu od początku roku (10 odwołań, 21 zastępstw). Źródło zmian planu zamiast adnotacji HTML. |
| `ClassFreeDays` | 45 (3 tej klasy) | `Class.Id`, `Type.Id`, `DateFrom`, `DateTo`, `LessonNoFrom`, `LessonNoTo` | Wszystkie klasy szkoły; filtrować po `Classes.Class.Id`. |
| `SchoolFreeDays` | 1 | `Name`, `DateFrom`, `DateTo` | |
| `HomeWorks` (terminarz) | 10 | `Date`, `TimeFrom`, `TimeTo`, `LessonNo`, `Category.Id`, `Content`, `AddDate` | `Content` w całości (w HTML opis był rozbity na klucze). Kategorie w `HomeWorks/Categories` (22). |
| `HomeWorkAssignments` | 0 | | Moduł działa, brak zadań u tego dziecka. |
| `SchoolNotices` | 17 | `Subject`, `Content`, `StartDate`, `EndDate`, `CreationDate`, `WasRead`, `AddedBy.Id` | Wszystkie miały `WasRead=true`. Do sprawdzenia, czy pobranie oznacza je jako przeczytane. |
| `Notes` (uwagi) | 0 | | Moduł działa. |
| `Grades` + `Grades/Comments` | 2 + 2 | `Comments` (lista id) → `Text` | Komentarze do ocen mają tekst: kanał, którym może przyjść informacja. |
| `Realizations` | 122 | `Date`, `LessonNo`, `Lesson.Id`, `IsTrip` | **Bez pola `Topic`** na tym koncie (możliwe, że w klasach 1–3 tematy są gdzie indziej). |
| `ParentTeacherConferences` | 1 | `Date`, `Time`, `Topic`, `Name`, `Room` | Ten sam termin bywa też w `HomeWorks`: deduplikacja po dacie i godzinie. |
| `SchoolTrips` | 3 | `destination`, `route`, `locomotion`, `termFrom`, `termTo` | Pola małymi literami (camelCase). |
| `Timetables/OtherActivitiesRegister` | 0 | | Zajęcia dodatkowe; `status: CANCELED` dla odwołanych. |
| `TimetableEntries` | 105–117 | `Lesson.Id`, `DayOfTheWeek`, `LessonNo`, `DateFrom`, `DateTo` | Plan stały, podstawa normy dnia (E4); działa na obu kontach. |
| `Subjects`, `Users`, `Classes`, `Me` | słowniki | | Odwołania w rekordach to obiekty `{"Id"}`; łączymy u siebie. |

### Drugie konto (starsza klasa) i oznaczanie jako przeczytane

- `HomeWorkAssignments`: 3 zadania z niepustymi `Topic` i `Text`, pola `Date` (dodania) i `DueDate`. Zadanie widoczne na osi czasu aplikacji jest w API z tym samym terminem: moduł działa, a pusty wynik na pierwszym koncie oznaczał brak zadań u tego dziecka.
- `Grades/Comments`: 30 komentarzy z tekstem przy 59 ocenach. Najliczniejszy kanał wolnego tekstu poza wiadomościami.
- `HomeWorks`: 28 wpisów, w tym 25 z przedmiotem. Szkolne wpisy (zebrania, apele) nie mają przedmiotu.
- `Calendars/Substitutions`: 36 zmian planu (9 odwołań, 27 zastępstw).
- `Realizations`: 0 rekordów, `ParentTeacherConferences`: 0. Tematów lekcji nie ma na żadnym z dwóch kont, więc temat lekcji to kanał opcjonalny.
- **Ogłoszenia przez API nie są oznaczane jako przeczytane:** `SchoolNotices.WasRead` zostało `false` dla 17 z 17 po dwóch pobraniach. Na pierwszym koncie wszystkie ogłoszenia były przeczytane po wcześniejszym pobraniu strony HTML `/ogloszenia` przez `librus-apix`. Wniosek: ogłoszenia pobieramy wyłącznie z API, nigdy ze strony HTML.
- Treść wiadomości: nadal do sprawdzenia przy pierwszej nieprzeczytanej wiadomości.

## Wnioski dla adaptera

1. Dane strukturalne z API (ADR-012): zmiany planu z `Calendars/Substitutions` i `Timetables`, dni wolne z `ClassFreeDays`/`SchoolFreeDays`, zebrania, wycieczki. Strony HTML tylko tam, gdzie API nie wystarcza (treść wiadomości).
2. Wolny tekst dla modelu: `HomeWorks.Content`, `HomeWorkAssignments` (temat i treść), `SchoolNotices.Content`, `Grades/Comments.Text`, `Notes.Text`, tematy lekcji (gdy są), wiadomości.
3. Klucze: `Id` z API (normalizowane do tekstu, bo typy są niespójne); dla wiadomości numeryczne `href`.
4. W HTML brakujące wartości to `"unknown"`: zamieniać na brak wartości.
5. Ogłoszenia tylko z API (`SchoolNotices`), bo strona HTML prawdopodobnie oznacza je jako przeczytane. Do sprawdzenia przy nowej wiadomości: czy pobranie treści oznacza ją jako przeczytaną.
