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

## Wnioski dla adaptera

1. Plan lekcji: odstępstwa z `info`, nie z braku lekcji.
2. Terminarz to drugie (obok wiadomości) źródło treści dla modelu, a nie tylko daty.
3. Klucze: `href` wiadomości i wpisów terminarza, gdy jest; skróty pól, gdy nie ma.
4. `"unknown"` zamieniać na brak wartości.
5. Ogłoszenia filtrować przed modelem po dacie (tylko nowe).
