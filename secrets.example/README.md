# Sekrety: wzorzec

Ten katalog pokazuje, jakie sekrety są potrzebne. Nie ma w nim wartości.
Prawdziwe sekrety trzymasz poza gitem: w katalogu `secrets/` (jest w `.gitignore`) albo w sekretach Podmana lub Dockera.

| Sekret | Zawartość | Kiedy |
|---|---|---|
| `LIBRUS_<ID>_LOGIN` | login rodzica w Synergii dla tego dziecka (np. `1234567`, bez `u`) | dla każdego dziecka z `config.yaml` |
| `LIBRUS_<ID>_PASSWORD` | hasło do tego loginu rodzica | dla każdego dziecka |
| `APPRISE_CONFIG` | kanały powiadomień, w każdej linii `tagi=URL` | zawsze |
| `LLM_API_KEY` | klucz API modelu | gdy `llm.api_key_required: true` |

`<ID>` to `children[].id` z konfiguracji, wielkimi literami (`ania` → `LIBRUS_ANIA_PASSWORD`).

Przykład `APPRISE_CONFIG`:

```
rodzice=tgram://<token_bota>/<id_czatu_grupy>
mama=ntfy://<temat>
```

## Podman

```sh
printf '%s' 'haslo-ani' | podman secret create librus_ania_password -
```

W pliku Quadlet sekret trafia do kontenera jako plik, a program dostaje do niego ścieżkę:

```ini
Secret=librus_ania_password
Environment=LIBRUS_ANIA_PASSWORD_FILE=/run/secrets/librus_ania_password
```

## Lokalnie

```sh
mkdir -p secrets && chmod 700 secrets
printf '%s' 'haslo-ani' > secrets/librus_ania_password && chmod 600 secrets/librus_ania_password
export LIBRUS_ANIA_PASSWORD_FILE=$PWD/secrets/librus_ania_password
```

- `telegram_bot_token`: token bota od @BotFather (sam token, bez cudzysłowów). Własny bot, nieużywany nigdzie indziej.
- `telegram_chat_ids`: identyfikatory czatów, jeden w linii (grupa: liczba ujemna). Bot pisze i odpowiada tylko tam.
