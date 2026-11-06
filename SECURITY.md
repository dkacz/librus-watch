# Bezpieczeństwo

librus-watch przetwarza hasła rodziców do Librusa i dane dotyczące dzieci. Każde zgłoszenie podatności traktujemy poważnie.

## Jak zgłosić podatność

- **Nie zakładaj publicznego zgłoszenia (issue)** dla podatności.
- Użyj prywatnego zgłoszenia na GitHubie: zakładka **Security** → **Report a vulnerability** w repozytorium `hretheum/librus-watch`.
- Opisz problem, kroki do odtworzenia i możliwy skutek. **Nie dołączaj prawdziwych danych:** haseł, loginów, treści wiadomości ze szkoły, imion dzieci ani nauczycieli. Jeśli przykład jest potrzebny, użyj wymyślonych danych.

Potwierdzenie odbioru w ciągu 7 dni. Poprawka krytycznej podatności (wyciek haseł, sesji lub danych dzieci) ma pierwszeństwo przed każdą inną pracą.

## Co jest w zakresie

- wyciek sekretów (hasła, tokeny, ciasteczka sesji, adresy kanałów powiadomień) do logów, komunikatów błędów, bazy bez ochrony albo do repozytorium;
- wyciek treści z Librusa lub danych dzieci poza katalog danych, w tym przez wspólny korpus (anonimizacja, ADR-017, ADR-018);
- możliwość sterowania programem przez treść wiadomości ze szkoły (prompt injection), która prowadzi do fałszywych powiadomień albo ujawnienia danych;
- obejście ochrony konta przy logowaniu (ADR-015), które naraża konto na blokadę;
- słabe uprawnienia plików, uruchamianie kontenera z nadmiernymi uprawnieniami.

## Poza zakresem

- podatności samego Librusa (zgłaszaj je do Librus Sp. z o.o.);
- konfiguracja serwera użytkownika poza tym, co opisuje README.

## Zasady projektu

Wymagania bezpieczeństwa opisują ADR-006 (sekrety, redakcja logów, kontener), ADR-011 (logowanie bez danych), ADR-015 (ochrona konta) i ADR-017/018 (korpus) w [`docs/decisions.md`](docs/decisions.md).
