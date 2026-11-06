# Decyzje projektowe (ADR)

Krótki rejestr decyzji: co postanowiliśmy, dlaczego i jakie ma to skutki. Nowe decyzje dopisujemy na końcu, starych nie zmieniamy. Gdy decyzja przestaje obowiązywać, dopisujemy nową, która ją zastępuje.

---

## ADR-001: Pilnujemy odstępstw, nie przepisujemy dziennika

**Kontekst.** Rodzic nie potrzebuje kopii Librusa. Potrzebuje wiedzieć, kiedy jutro wygląda inaczej niż zwykle: zbiórka pół godziny przed lekcjami, coś do przyniesienia, odwołane zajęcia, przesunięty sprawdzian. Najczęściej dowiaduje się o tym za późno, rano przed wyjściem.

**Decyzja.** Program porównuje dane z Librusa z normalnym dniem i z wcześniejszym stanem. Powiadamia tylko o różnicach. Gdy dzień jest zwykły, nic nie wysyła.

**Skutki.** Alertów jest mało, więc się nie opatrzą. Ceną jest konieczność utrzymywania normy dnia (patrz ADR-004).

## ADR-002: Działa jako usługa w kontenerze, nie na komputerze rodzica

**Kontekst.** Powiadomienie ma przyjść wieczorem i rano niezależnie od tego, czy ktoś ma włączony komputer. Program nie może też zależeć od jednego systemu operacyjnego ani od aplikacji zainstalowanych u rodzica.

**Decyzja.** Jeden obraz kontenera (OCI) z wbudowanym harmonogramem. Ten sam obraz działa w Podman Quadlet, Docker Compose albo na dowolnym serwerze. Jest też tryb jednorazowy `run-once` dla crona lub timera systemd.

**Skutki.** Potrzebny jest zawsze włączony komputer: domowy serwer, NAS, Raspberry Pi albo VPS.

## ADR-003: Treść wiadomości rozumie model językowy, nie regexy

**Kontekst.** Wiadomości od nauczycieli są pisane swobodnie („w czwartek zbiórka 7:30 pod szkołą, proszę o strój galowy”). Reguły oparte na wyrażeniach regularnych łamią się na każdym nowym sformułowaniu i wymagają ciągłego łatania.

**Decyzja.** Nowe wiadomości i ogłoszenia czyta model językowy przez dowolne API zgodne z OpenAI (lokalne: llama.cpp, llama-swap, Ollama; zdalne: OpenRouter, OpenAI i inne). Model zwraca wyłącznie dane w ustalonym schemacie JSON. Każda pozycja musi zawierać dosłowny cytat z wiadomości, a program sprawdza, czy ten cytat naprawdę tam jest. Tekst powiadomienia składa program z gotowego szablonu, nie model.

**Skutki.** Zmyślone przez model terminy odpadają, bo nie mają pokrycia w treści. Przy zdalnym API treść wiadomości szkolnych opuszcza dom, o czym mówi README i komentarz w konfiguracji. Domyślna konfiguracja wskazuje lokalny endpoint.

## ADR-004: Norma dnia pochodzi z planu lekcji

**Kontekst.** Żeby wiedzieć, że coś jest „wcześniej niż zwykle”, trzeba wiedzieć, co jest zwykle.

**Decyzja.** Plan lekcji z Librusa jest zapisywany co tydzień. Typowa godzina pierwszej i ostatniej lekcji w dany dzień tygodnia to najczęstsza wartość z ostatnich tygodni. Odstępstwa wykryte w samym planie (zastępstwa, odwołania, inny początek lub koniec) liczy program, bez modelu. Ta sama norma trafia do modelu jako kontekst przy czytaniu wiadomości.

**Skutki.** Przez pierwszy tydzień normą jest bieżący plan. Gdy plan zmieni się na stałe (nowy semestr), norma dostosuje się po kilku tygodniach.

## ADR-005: Powiadomienia przez Apprise, rytm do ustawienia

**Decyzja.** Wysyłka idzie przez bibliotekę Apprise. Jeden plik obsługuje Telegram, Signal, ntfy, Pushover, e-mail i ponad sto innych kanałów. Domyślny rytm: wieczorem podsumowanie na jutro (tylko gdy jest coś nietypowego), rano tylko zmiany od wieczora, w ciągu dnia od razu rzeczy pilne na dziś i jutro, a w godzinach ciszy nic. Wszystko to da się zmienić w konfiguracji, łącznie z tym, który kanał dostaje które powiadomienia.

**Skutki.** Ta sama informacja nie przychodzi dwa razy. Zmiana wcześniej zgłoszonej rzeczy przychodzi jako „ZMIANA: było… jest…”. Gdy program przestaje działać, wysyła raport o awarii, więc cisza nie oznacza po cichu, że coś się zepsuło.

## ADR-006: Bezpieczeństwo jest wymaganiem, nie dodatkiem

**Decyzja.**
- Hasła, klucze API i adresy kanałów powiadomień podaje się wyłącznie przez zmienne środowiskowe albo pliki sekretów (`NAZWA_FILE`, zgodnie z sekretami Podmana i Dockera). Plik konfiguracyjny odrzuca klucze wyglądające na sekrety.
- Każda wczytana wartość sekretu jest wycinana z logów, także z komunikatów błędów bibliotek.
- Dane zostają w jednym katalogu danych (SQLite) z uprawnieniami tylko dla właściciela. Nigdzie nie są publikowane.
- Każde połączenie sieciowe ma limit czasu. Każde dziecko ma osobną sesję HTTP i osobny pojemnik na ciasteczka, więc ciasteczka kont się nie mieszają. Biblioteka `librus-apix` domyślnie współdzieli jeden pojemnik między wszystkimi klientami, dlatego adapter zawsze podaje własny.
- Z biblioteki Librusa używamy wyłącznie funkcji do odczytu. Funkcji wysyłania wiadomości ani jednorazowego podglądu „dodane od ostatniego logowania” nie wywołujemy.
- Treść wiadomości trafia do modelu jako niezaufane dane. Model nie ma narzędzi, a jego odpowiedź przechodzi walidację schematu i kontrolę cytatu.
- Kontener działa bez roota, z systemem plików tylko do odczytu, bez uprawnień jądra i z `NoNewPrivileges`.
- Wersje zależności są przypięte w `uv.lock`. Resolver nie bierze wersji opublikowanych w ostatnich dniach.
- Repozytorium jest publiczne, dlatego prywatne pliki są w `.gitignore`, w repo leżą tylko wzorce `.example`, a hook `pre-commit` (gitleaks plus kontrola prywatnych ścieżek) blokuje przypadkowe commity. Dane testowe są wymyślone.

## ADR-007: Ostrożnie z kontem w Librusie

**Kontekst.** Zbyt częste logowanie i ponawianie po błędach mogą zablokować konto albo zwrócić uwagę operatora.

**Decyzja.** Domyślnie pobieramy dane co 60 minut, tylko w ciągu dnia. Liczba nieudanych prób na dobę jest ograniczona, a odstępy między próbami rosną. Błąd logowania wstrzymuje próby do następnego dnia i wysyła raport. O błędzie logowania decyduje typ wyjątku, bo treść komunikatów Librusa może się zmienić, a nieznany błąd przy logowaniu traktujemy jak zły login. Treść wiadomości pobieramy tylko raz, dla nowych wiadomości. Sesja jest używana ponownie, dopóki działa.

**Skutki.** Librus oznacza otwartą wiadomość jako przeczytaną, także gdy otwiera ją program. Jest to opisane w README.

## ADR-008: Niczego nie kasujemy z historii

**Kontekst.** Pobrane dane obejmują tylko wybrany zakres dat, a pojedyncze pobranie może się nie udać w całości. Gdyby brak wpisu w jednym pobraniu oznaczał jego usunięcie, z historii znikałyby minione sprawdziany i wycieczki.

**Decyzja.** Historia w bazie tylko przybywa. Wpis znika z terminarza (staje się „odwołany”) wyłącznie wtedy, gdy dana sekcja została pobrana w całości, a wpis miał datę w pobranym zakresie. Przyszła integracja z kalendarzem nie będzie kasować przeszłych wydarzeń.

## ADR-009: Licencja GPL-3.0

**Kontekst.** Projekt korzysta z biblioteki `librus-apix`, która jest na licencji GPL-3.0.

**Decyzja.** Projekt jest wydany na GPL-3.0-or-later.

## ADR-010: Osobne konto rodzica w Synergii dla każdego dziecka

**Kontekst.** Konto w Synergii jest zawsze przypięte do jednego ucznia. Rodzic dwojga dzieci dostaje ze szkoły dwa loginy rodzica. Konto ucznia (login z końcówką `u`) może nie widzieć wiadomości i ogłoszeń kierowanych do rodziców, a to tam są zbiórki, zgody i płatności. Konto LIBRUS (logowanie e-mailem, „Multikonto”) łączy kilka kont Synergii, ale biblioteka `librus-apix` go nie obsługuje.

**Decyzja.** Program loguje się loginem rodzica w Synergii. Każde dziecko to osobny wpis w `children` z własnymi sekretami `LIBRUS_<ID>_LOGIN` i `LIBRUS_<ID>_PASSWORD`.

**Skutki.** Przy dwójce dzieci potrzebne są dwa loginy rodzica. Logowanie e-mailem do Konta LIBRUS nie jest obsługiwane.

## ADR-011: Poziomy logowania i klasy błędów

**Kontekst.** Logi trafiają do journald albo silnika kontenerów i czyta je właściciel serwera, czasem ktoś pomagający przy awarii. Dane pobierane z Librusa dotyczą dzieci, a błędy bibliotek potrafią zawierać adresy z tokenami. Od rodzaju błędu przy pobieraniu zależy, czy ponawiać próbę, czy wstrzymać logowanie (ADR-007).

**Decyzja.** Poziomy logowania:
- `DEBUG`: liczby i czasy (rekordy per moduł, czas odpowiedzi, liczba zapytań do modelu). Bez treści.
- `INFO`: początek i koniec cyklu, liczby rekordów, wysłane powiadomienia (typ, id dziecka, liczba pozycji), migracja schematu, porządki w bazie.
- `WARNING`: ponowienie próby, moduł niedostępny (np. 403), pominięty lub odrzucony tekst (odpowiedź modelu bez pokrycia w treści), zawężone uprawnienia plików.
- `ERROR`: nieudany cykl, błąd logowania, zmiana kształtu odpowiedzi.
- `CRITICAL`: nieobsłużony wyjątek kończący program.

Na żadnym poziomie nie logujemy treści wiadomości, ogłoszeń i wpisów, cytatów, imion i nazwisk, loginów ani wartości sekretów. Dziecko identyfikuje `id` z konfiguracji, komunikat Librusa: jego id. Przy błędach logujemy typ wyjątku. Wszystkie logi, także nieobsłużone wyjątki, przechodzą przez redakcję sekretów (ADR-006).

Klasy błędów pobierania:
- `AuthError`: zły login albo hasło, wygasłe konto. Wstrzymuje próby do następnego dnia i wysyła raport (ADR-007).
- `TransientError`: limit czasu, błąd sieci, 5xx, prace serwisowe Librusa. Ponowienie z rosnącym odstępem, w limicie prób na dobę.
- `SchemaError`: odpowiedź o nieoczekiwanym kształcie (zmiana API albo strony). Moduł pomijany w tym cyklu, raport o awarii po pierwszym wystąpieniu, bo samo ponawianie tego nie naprawi.
- `ModuleUnavailable`: 403 albo 404 dla modułu, którego szkoła nie używa. Ostrzeżenie raz na dobę, bez raportu o awarii.

**Skutki.** Z logów da się odtworzyć przebieg cyklu i przyczynę awarii bez wglądu w dane dzieci. Do diagnozy treści (np. dlaczego model nie znalazł zbiórki) potrzebny jest dostęp do bazy w katalogu danych, a nie do logów.

## ADR-012: Dane z JSON-owego API Synergii, każdy wolny tekst czyta model

**Kontekst.** Aplikacja mobilna Librus pokazuje na „Osi czasu” wpisy z wielu modułów: wiadomości, ogłoszenia, terminarz, zadania domowe, oceny z komentarzami, uwagi, zmiany w planie. Nauczyciele nie zawsze informują o odstępstwie przewidzianym do tego kanałem: „przynieście zgodę” bywa w treści zadania domowego, kartkówka w terminarzu pod kategorią „Inne”, a zmiana godziny w komentarzu albo w temacie lekcji. Strony HTML Synergii gubią przy tym strukturę (np. opis wpisu w terminarzu rozbity na pola), a zastępstwa i odwołania są tylko adnotacjami tekstowymi.

**Decyzja.** Dane pobieramy z JSON-owego API, z którego korzysta Synergia (`/gateway/api/2.0`), w tej samej sesji co logowanie. Moduły: plan lekcji i historia zmian planu, dni wolne klasy i szkoły, terminarz, zadania domowe, ogłoszenia, uwagi, oceny z komentarzami, tematy lekcji, zebrania, wycieczki i zajęcia dodatkowe. Pola strukturalne (odwołanie, zastępstwo, dzień wolny, termin zebrania) program porównuje sam. Każde pole z wolnym tekstem, niezależnie od modułu i kategorii, przechodzi przez model na tych samych zasadach co wiadomości (ADR-003). Przed modelem działa tanie sito: tylko nowe lub zmienione wpisy. Biblioteka `librus-apix` zostaje do logowania i treści wiadomości.

**Skutki.** Odstępstwo zostanie zauważone niezależnie od tego, gdzie nauczyciel je wpisał. Zapytań do modelu jest więcej (komentarze, tematy lekcji), więc sito i limit znaków na wpis są wymagane. API jest nieoficjalne: każdy moduł pobieramy osobno, a błąd jednego (np. 403, gdy szkoła nie używa modułu) nie zatrzymuje pozostałych i trafia do raportu jako ostrzeżenie, nie awaria.

## ADR-013: Model dostaje tylko to, czego nie da się rozstrzygnąć taniej

**Kontekst.** Model lokalny ładuje się do minuty i dłużej, karta bywa współdzielona z innymi usługami, a każde przełączenie modelu kosztuje także je. Model zdalny kosztuje pieniądze i wysyła treść poza dom. Tekstów jest mało, ale pierwsza synchronizacja przynosi setki starych, a te same ogłoszenia szkolne widzi każde dziecko. Ważniejsze od przepustowości jest to, żeby sprawy na jutro były przeczytane przed wieczornym podsumowaniem.

**Decyzja.** Tekst przechodzi przez stopnie od najtańszego: normalizacja i odcisk treści, sito deterministyczne (niezmienione, przeterminowane, stara historia, komentarze bez dat i słów-kluczy), cache wyników kluczowany odciskiem treści, wersją promptu i modelem (także dla pustych wyników, wspólny dla dzieci), kolejka z priorytetem terminu i dziennym budżetem, triage wsadowy tym samym modelem, ekstrakcja ze stałym prefiksem promptu (cache promptu po stronie serwera), walidacja z jedną próbą naprawy. Jeden model na cykl, zapytania kolejno, osobny limit czasu na zimny start. Możliwości endpointu sprawdzane sondą i zapamiętywane na dobę. Brak automatycznego przełączania na model zdalny. Szczegóły: `docs/llm.md`.

**Skutki.** W zwykły dzień model dostaje kilka zapytań. Pomyłki sita i triage są ustawione w bezpieczną stronę: wątpliwy tekst idzie do ekstrakcji. Zmiana promptu albo modelu nie przelicza historii, tylko teksty wciąż aktualne. Awaria modelu nie zatrzymuje odstępstw wyliczanych przez program, a przedłużona daje raport o awarii.

## ADR-014: Typy treści ignorowane w całości, domyślnie oceny

**Kontekst.** Oś czasu w aplikacji Librus pokazuje wszystko, także informacje, które nie zmieniają dnia rodzica, np. nową ocenę. Różne rodziny chcą pomijać różne rzeczy. Komentarze do ocen bywają jednak kanałem informacji o odstępstwach („poprawa w czwartek na 1. lekcji”).

**Decyzja.** Konfiguracja ma listę typów treści ignorowanych w całości (`ignore_content`). Typ z listy nie trafia do bazy, do modelu ani do powiadomień. Jeśli żaden niezignorowany typ nie potrzebuje danego modułu API, moduł nie jest w ogóle pobierany. Domyślnie ignorowane są same oceny (`grade`). Komentarze do ocen (`grade_comment`) są osobnym typem i domyślnie zostają, a moduł ocen jest wtedy pobierany tylko jako słownik do komentarzy. Nieznany typ na liście to błąd konfiguracji; zignorowanie zmian planu albo dni wolnych jest dozwolone, ale program ostrzega o tym przy starcie.

Oceny i komentarze do ocen wybiera się niezależnie:

| `ignore_content` zawiera | Oceny | Komentarze do ocen | Moduły pobierane |
|---|---|---|---|
| nic z tej pary | w strumieniu | czyta model | `Grades`, `Grades/Comments` |
| `grade` (domyślnie) | pominięte | czyta model | oba (`Grades` tylko jako słownik przedmiotu i daty komentarza) |
| `grade_comment` | w strumieniu | pominięte | tylko `Grades` |
| `grade`, `grade_comment` | pominięte | pominięte | żaden |

**Skutki.** Rodzic decyduje, co jest szumem, bez zmian w kodzie. Wyłączenie typu zmniejsza też liczbę zapytań do Librusa i do modelu. Typy: `message`, `notice`, `agenda`, `homework`, `grade`, `grade_comment`, `note`, `lesson_topic`, `conference`, `trip`, `activity`, `plan_change`, `free_day`.

## ADR-015: Ochrona konta przy logowaniu: znane urządzenie, limit, przerwa po odmowie

**Kontekst.** Przy kilku logowaniach w ciągu dwóch godzin Librus odpowiedział na jednym koncie stroną zamiast wyniku logowania, najpewniej captchą albo blokadą. Każde logowanie programu wyglądało dla Librusa na nowe urządzenie, bo program nie zachowywał ciasteczka `DeviceCookie`, po którym Librus rozpoznaje znane urządzenia. Uruchomienia ręczne (testy, diagnoza) nie przechodziły przez żadne limity.

**Decyzja.** Każde logowanie, także ręczne i w przebiegu bez zapisu danych, przechodzi przez tę samą ochronę:
- `DeviceCookie` jest zapamiętywane w bazie osobno dla każdego dziecka (uprawnienia 0600, wartość wycinana z logów) i wysyłane przy kolejnych logowaniach.
- Liczba logowań na dobę jest ograniczona (`fetch.max_logins_per_day`), a logowanie liczy się przed próbą.
- Odmowa logowania, także odpowiedź inna niż JSON (możliwa captcha), wstrzymuje próby do początku aktywnych godzin następnego dnia; komunikat mówi wprost o możliwej captchy.
- Świadome obejście blokady i limitu daje tylko `--force`, z ostrzeżeniem w logu.
- Stan ochrony (licznik, blokada, `DeviceCookie`) jest zapisywany zawsze; przebieg bez zapisu nie zapisuje tylko danych z Librusa.

**Skutki.** Kolejne logowania wyglądają na to samo urządzenie, a błąd po stronie Librusa nie zamienia się w serię prób. Ceną jest możliwość przegapienia dnia, gdy odmowa była jednorazowa; wtedy pomaga `--force` albo raport o awarii.
