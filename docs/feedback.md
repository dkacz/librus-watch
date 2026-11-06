# Pętla zwrotna i telemetria

Jak program dowiaduje się, że się pomylił, i jak z tego korzysta: najpierw u jednego użytkownika (lokalnie), potem, na wyraźną zgodę, dzięki doświadczeniom wszystkich (wspólny korpus i telemetria zbiorcza). Decyzje: ADR-016, ADR-017 i ADR-018 (korpus domyślnie włączony, opt-out).

„Model się uczy” znaczy tu: rośnie zestaw oceny, z którego wybieramy model i prompt, rośnie biblioteka przykładów dokładanych do promptu, a sito i reguły są strojone na prawdziwych pomyłkach. Dostrajanie wag modelu (fine-tuning) jest możliwe dopiero przy dużym, sprawdzonym zbiorze i nie jest częścią MVP.

## Część 1: pętla lokalna

### Co zbieramy (u użytkownika, w bazie)

Program już przechowuje teksty, decyzje sita, wyniki modelu i pozycje. Pętla dokłada **zdarzenia oceny**: kto, kiedy i jak ocenił pozycję albo tekst. Wszystko zostaje w katalogu danych (SQLite, 0600), nic nie wychodzi z domu.

| Zdarzenie | Znaczenie | Źródło |
|---|---|---|
| `confirmed` | pozycja trafna | przycisk przy powiadomieniu, przegląd w CLI |
| `wrong` | pozycja błędna (fałszywy alarm), z powodem: nie ten dzień, nie to dziecko, nie odstępstwo, zmyślone | jw. |
| `missed` | coś powinno było przyjść, a nie przyszło (pominięcie), z odnośnikiem do tekstu albo dnia | zgłoszenie, przegląd, wykrycie automatyczne |
| `late` | przyszło, ale za późno (np. po wyjściu z domu) | zgłoszenie |

### Jak wykrywamy pomyłki

**Fałszywe alarmy (FP):**
- ocena użytkownika przy powiadomieniu (`wrong`);
- pozycja wycofana w kolejnym cyklu, choć tekst się nie zmienił (model raz tak, raz nie);
- pozycja odrzucona przez walidację (cytat bez pokrycia, data poza oknem): liczona osobno, bo do rodzica nie dotarła.

**Pominięcia (FN), których nikt nie zgłosi sam:**
- **audyt sita w cieniu:** codziennie mała losowa próbka tekstów odrzuconych przez sito (np. 3) idzie do modelu bez wysyłania powiadomień. Pozycja znaleziona w odrzuconym tekście to pominięcie sita i kandydat do poprawy reguł;
- **audyt triage w cieniu:** to samo dla tekstów, które triage uznał za puste;
- **zgodność źródeł:** odstępstwo wyliczone przez program (np. sprawdzian w terminarzu, odwołana lekcja) przy wiadomości z tego dnia, w której model nic nie znalazł, albo odwrotnie: model znalazł zmianę godziny, której nie widać w planie. Rozjazd trafia do przeglądu, nie do powiadomień;
- **słowa alarmowe:** tekst ze słowami w rodzaju „jutro”, „zbiórka”, „zgoda” bez żadnej pozycji trafia do przeglądu;
- **pilot (E9):** codzienne porównanie z osią czasu aplikacji Librus, wpisywane jako `missed` lub `confirmed`.

### Jak użytkownik ocenia

- **Przy powiadomieniu w Telegramie:** przyciski „trafne” i „błędne” pod każdą pozycją oraz polecenie „czegoś brakuje” (np. `/brak jutro zbiórka 7:30`). Obsługuje to własny bot Telegrama od etapu E6 (ADR-019), bo Apprise tylko wysyła; cotygodniowe pytania i raport też przychodzą przez bota.
- **Przegląd w CLI:** `librus-watch review` pokazuje pozycje z ostatnich dni i kandydatów z audytu (teksty w terminalu użytkownika, lokalnie), a ocenę zapisuje jednym klawiszem.
- Każda ocena jest odwracalna i ma znacznik czasu.

### Jak program z tego korzysta

1. **Zestaw oceny rośnie z prawdziwych przypadków** (`data_dir`, nigdy w repo). Każda zmiana promptu, modelu, sita i reguł jest sprawdzana na nim przed włączeniem. Spadek trafności blokuje zmianę.
2. **Biblioteka przykładów:** ocenione przypadki, zwłaszcza pominięcia i fałszywe alarmy, są przykładami do promptu. Przy ekstrakcji dokładamy kilka najbardziej podobnych (po rodzaju źródła i słowach), **za** stałym prefiksem promptu, żeby nie psuć cache promptu.
3. **Strojenie sita i reguł:** pominięcia z audytu w cieniu podpowiadają nowe słowa-klucze i wyjątki; zmiana reguły jest wersjonowana i oceniana jak zmiana promptu.
4. **Progi:** `min_confidence`, okna dat i budżet audytu dobierane na podstawie ocen.
5. **Raport tygodniowy (lokalny):** trafność, pominięcia i fałszywe alarmy według źródła treści, bez treści.

### Telemetria lokalna

Liczby z każdego cyklu (już logowane) są dodatkowo zapisywane w bazie i opcjonalnie wystawiane jako plik metryk dla `node_exporter` (textfile collector) albo przez `librus-watch stats`: liczba tekstów i pozycji, skuteczność sita i cache, czasy i tokeny modelu, odrzucenia walidacji, FP/FN według źródła, błędy modułów Librusa. Bez treści, bez nazwisk.

## Część 2: wspólna pętla (korpus opt-out, telemetria opt-in)

Cel: model każdego użytkownika korzysta z pomyłek wszystkich, a żadne dane dziecka, rodzica ani nauczyciela nie opuszczają domu w formie, z której da się kogoś rozpoznać.

### Zasady

- **Korpus domyślnie włączony (opt-out, ADR-018).** Wysyłka zanonimizowanych przykładów działa automatycznie; `feedback.share_corpus: false` wyłącza ją od następnego cyklu. Informacja o tym jest w README i przy pierwszym uruchomieniu, przed pierwszą wysyłką.
- **Telemetria zbiorcza na zgodę (opt-in).**
- **Wychodzą tylko przykłady zanonimizowane i ocenione,** nigdy surowe teksty, nigdy całe skrzynki.
- **Użytkownik widzi dokładnie to, co wyszło:** lokalny dziennik wysłanych przykładów i polecenie do jego podglądu; może też przejrzeć paczkę przed wysyłką (`librus-watch contribute --review`).
- **Kanał zbierania:** automatyczna wysyłka wymaga endpointu z uwierzytelnieniem paczki i limitem (projekt w E11); po stronie korpusu przegląd i historia są jawne.
- **Wynik wraca do wszystkich w wydaniach programu:** zestaw oceny, biblioteka przykładów, reguły sita i prompty, wersjonowane i podpisane (hash przypięty w kodzie).

### Anonimizacja (lokalnie, przed przeglądem)

1. **Słowniki Librusa:** imiona i nazwiska z `Users`, dzieci z konfiguracji, nazwy klas, szkoły i sal zamieniane na znaczniki (`[NAUCZYCIEL_1]`, `[UCZEŃ_1]`, `[KLASA]`, `[SZKOŁA]`).
2. **Wykrywanie danych osobowych** spoza słowników (inne dzieci w ogłoszeniach o konkursach, rodzice, adresy, telefony, e-maile, numery kont, kwoty z nazwiskiem): model NER dla polskiego (np. Presidio z `pl_core_news_lg`) plus reguły; każde trafienie to znacznik.
3. **Uogólnienie:** daty względem daty wysłania (`[D+1]`, `[czwartek]`), nazwy miejsc i wydarzeń lokalnych na kategorie (`[MIEJSCE]`, `[WYDARZENIE]`), godziny zostają (są istotą odstępstwa).
4. **Odrzucenie ryzykownych:** tekst z niewykrytymi danymi (wysoki udział słów od wielkiej litery po anonimizacji), bardzo długi albo o rzadkim, rozpoznawalnym wydarzeniu nie trafia do paczki.
5. **Zasada „w razie wątpliwości odrzuć”:** przykład z niewyjaśnionym słowem od wielkiej litery, liczbą wyglądającą na telefon, numer konta lub adres nie jest wysyłany. Przegląd ręczny (`librus-watch contribute --review`) jest możliwy, ale przy opt-out nie jest warunkiem wysyłki.

Cytat w oczekiwanej pozycji odnosi się do tekstu **po** anonimizacji, więc przykład zachowuje wartość do oceny.

### Telemetria zbiorcza (opt-in)

- Tylko liczby z raportu tygodniowego, zaokrąglone i z szumem (prywatność różnicowa w prostej postaci), bez identyfikatorów szkoły, klasy i dziecka.
- Najcenniejszy sygnał dla wszystkich: **błędy modułów Librusa** (zmiana kształtu API) i **skuteczność według źródła treści** po wydaniu nowego promptu.
- Ten sam kanał co korpus (plik do PR albo zgłoszenie), dopóki nie ma powodu stawiać serwera.

### Zagrożenia i odpowiedzi

| Zagrożenie | Odpowiedź |
|---|---|
| Rozpoznanie osoby z treści (rzadkie wydarzenie, nazwa szkoły, inne dziecko) | słowniki + NER + uogólnienie + odrzucenie ryzykownych + ręczny przegląd |
| Rozpoznanie z metadanych | brak id szkoły, klasy, dziecka i dat bezwzględnych; telemetria zaokrąglona i z szumem |
| Zatrucie korpusu (złośliwe przykłady) | przegląd PR, wkład wchodzi do wydania dopiero, gdy nie obniża wyniku na zestawie bazowym; pochodzenie przykładu zapisane |
| Podmiana paczki przy wydaniu | paczka podpisana, hash przypięty w kodzie programu |
| Prawo (RODO, dane dzieci, treści nauczycieli, regulamin Librusa) | przed uruchomieniem korpusu przegląd prawny; do tego czasu działa tylko pętla lokalna |

## Etapy

- **Pętla lokalna** po E7 (pętla i polecenia), przed pilotem E9, żeby pilot zbierał oceny: tabela zdarzeń oceny, `review`, audyt w cieniu, zgodność źródeł, raport tygodniowy, metryki lokalne; bot Telegrama z przyciskami jako opcja.
- **Biblioteka przykładów w prompcie** razem z E5c albo po pierwszym tygodniu ocen.
- **Wspólny korpus (opt-out) i telemetria zbiorcza (opt-in)** po wydaniu (E10), po przeglądzie prawnym.
