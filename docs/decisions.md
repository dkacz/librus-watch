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
