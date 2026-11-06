"""Konfiguracja programu: plik YAML bez sekretów plus sekrety z env/plików.

Plik konfiguracyjny jest walidowany przez pydantic z `extra="forbid"` na
każdym poziomie, a dodatkowo `load_config` odrzuca klucze wyglądające na
sekrety (`password`, `token`, `api_key`...). Dzięki temu hasło wpisane omyłkowo
do pliku kończy się czytelnym błędem, a nie cichym przechowywaniem go jawnym
tekstem.

Wzorzec wszystkich opcji z komentarzami: `config.example.yaml`.
"""

from __future__ import annotations

import re
from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from librus_watch.secret_env import MissingSecretError, get_secret

_FORBIDDEN_KEY = re.compile(r"pass|secret|token|api_?key|login|credential", re.IGNORECASE)
_ALLOWED_KEYS = frozenset({"api_key_required"})
_LOCAL_HTTP = re.compile(
    r"^http://(localhost|127\.0\.0\.1|\[::1\]|[\w-]+|[\w.-]+\.(lan|local|internal|home\.arpa)"
    r"|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?(/|$)"
)


class Strict(BaseModel):
    """Baza modeli konfiguracji: nieznane pola są błędem, obiekty niezmienne."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Priority(StrEnum):
    """Waga powiadomienia; decyduje o tym, co idzie od razu, a co w podsumowaniu."""

    low = "low"
    normal = "normal"
    high = "high"

    @property
    def rank(self) -> int:
        """Liczba do porównań (low=0, normal=1, high=2)."""
        return {"low": 0, "normal": 1, "high": 2}[self.value]


class Kind(StrEnum):
    """Rodzaj odstępstwa od normalnego dnia."""

    early_start = "early_start"
    """Trzeba być w szkole wcześniej niż zwykle (zbiórka, wcześniejsza lekcja)."""
    late_start = "late_start"
    """Zajęcia zaczynają się później niż zwykle."""
    early_end = "early_end"
    """Zajęcia kończą się wcześniej niż zwykle (trzeba wcześniej odebrać dziecko)."""
    late_end = "late_end"
    """Zajęcia kończą się później niż zwykle."""
    day_off = "day_off"
    """Dzień bez zajęć, choć zwykle są."""
    cancel = "cancel"
    """Odwołana lekcja lub wydarzenie."""
    substitution = "substitution"
    """Zastępstwo (inny nauczyciel lub przedmiot)."""
    bring = "bring"
    """Coś do przyniesienia (strój, materiały, pieniądze w gotówce)."""
    consent = "consent"
    """Zgoda lub dokument do podpisania i oddania."""
    payment = "payment"
    """Płatność (przelew, składka)."""
    trip = "trip"
    """Wycieczka lub wyjście."""
    test = "test"
    """Sprawdzian, kartkówka, klasówka."""
    homework = "homework"
    """Praca domowa z terminem."""
    change = "change"
    """Zmiana wcześniej ogłoszonego terminu lub szczegółów."""
    other = "other"
    """Inna jednorazowa informacja wymagająca działania rodzica."""


HHMM = Annotated[time, Field(description="Godzina w formacie HH:MM")]


class TimeWindow(Strict):
    """Przedział godzin w ciągu doby; może przechodzić przez północ (np. 21:30–06:15)."""

    start: HHMM
    end: HHMM

    def contains(self, t: time) -> bool:
        """Sprawdza, czy godzina mieści się w przedziale.

        Args:
            t: Godzina do sprawdzenia.

        Returns:
            `True`, gdy `start <= t < end` (z obsługą przejścia przez północ).
        """
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


class Child(Strict):
    """Dziecko i jego konto w Librusie.

    Dane logowania nie są tutaj, tylko w sekretach
    `LIBRUS_<ID>_LOGIN` i `LIBRUS_<ID>_PASSWORD` (np. `LIBRUS_ANIA_PASSWORD_FILE`).
    """

    id: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    """Identyfikator techniczny: małe litery, cyfry, podkreślenie."""
    name: Annotated[str, Field(min_length=1, max_length=40)]
    """Imię wyświetlane w powiadomieniach."""

    @property
    def env_prefix(self) -> str:
        """Prefiks nazw sekretów tego dziecka, np. `LIBRUS_ANIA`."""
        return f"LIBRUS_{self.id.upper()}"


class FetchConfig(Strict):
    """Jak często i jak dużo pobierać z Librusa."""

    interval_minutes: Annotated[int, Field(ge=20, le=720)] = 60
    """Odstęp między pobraniami. Minimum 20 minut, żeby nie obciążać Librusa."""
    active_hours: TimeWindow = TimeWindow(start=time(6, 0), end=time(21, 30))
    """W jakich godzinach wolno pobierać dane."""
    max_failures_per_day: Annotated[int, Field(ge=1, le=10)] = 4
    """Po tylu nieudanych próbach w ciągu doby program czeka do następnego dnia."""
    backoff_base_minutes: Annotated[int, Field(ge=5, le=240)] = 20
    """Odstęp po pierwszej porażce; każda kolejna podwaja odstęp."""
    http_timeout_seconds: Annotated[float, Field(ge=5, le=120)] = 30
    """Limit czasu pojedynczego żądania HTTP do Librusa."""
    timetable_weeks: Annotated[int, Field(ge=1, le=3)] = 2
    """Ile tygodni planu lekcji pobierać (bieżący + kolejne)."""
    schedule_months: Annotated[int, Field(ge=1, le=3)] = 2
    """Ile miesięcy terminarza pobierać (bieżący + kolejne)."""
    homework_days_ahead: Annotated[int, Field(ge=1, le=60)] = 21
    """Zakres prac domowych w przód (w dniach)."""
    max_message_pages: Annotated[int, Field(ge=1, le=50)] = 5
    """Bezpiecznik: maksymalna liczba stron wiadomości przeglądanych w jednym pobraniu."""


class LLMConfig(Strict):
    """Model językowy przez API zgodne z OpenAI (`/v1/chat/completions`)."""

    enabled: bool = True
    """Wyłączenie pomija analizę wiadomości (zostają odstępstwa z planu i terminarza)."""
    base_url: str = "http://localhost:8080/v1"
    """Adres API. http:// jest dozwolone tylko dla adresów w sieci lokalnej."""
    model: str = "local-model"
    """Nazwa modelu w danym API."""
    json_mode: Literal["auto", "json_schema", "json_object"] = "auto"
    """Wymuszenie JSON; `auto` próbuje schematu, a bez wsparcia używa `json_object`."""
    timeout_seconds: Annotated[float, Field(ge=5, le=600)] = 120
    """Limit czasu jednego zapytania do modelu."""
    max_input_chars: Annotated[int, Field(ge=500, le=50_000)] = 8000
    """Dłuższe wiadomości są przycinane przed wysłaniem do modelu."""
    min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    """Pozycje z niższą pewnością modelu są odrzucane."""
    max_attempts_per_message: Annotated[int, Field(ge=1, le=10)] = 3
    """Po tylu nieudanych analizach wiadomość jest pomijana (i odnotowana w logu)."""
    api_key_required: bool = False
    """Czy sekret `LLM_API_KEY` jest wymagany (lokalne serwery zwykle go nie potrzebują)."""

    @field_validator("base_url")
    @classmethod
    def _https_or_local(cls, v: str) -> str:
        """Wymusza HTTPS dla adresów spoza sieci lokalnej.

        Args:
            v: Adres API z konfiguracji.

        Returns:
            Adres bez końcowego ukośnika.

        Raises:
            ValueError: Gdy adres używa http:// poza siecią lokalną.
        """
        if not v.startswith("https://") and not _LOCAL_HTTP.match(v):
            raise ValueError(
                "base_url musi używać https:// (http dozwolone tylko w sieci lokalnej)"
            )
        return v.rstrip("/")


class AlertType(StrEnum):
    """Typ powiadomienia."""

    evening = "evening"
    """Wieczorne podsumowanie na jutro."""
    morning = "morning"
    """Poranne powiadomienie o zmianach od wieczora."""
    instant = "instant"
    """Natychmiastowe powiadomienie o pilnej rzeczy na dziś lub jutro."""
    failure = "failure"
    """Raport o awarii (brak danych, zły login)."""


class ScheduledBrief(Strict):
    """Podsumowanie wysyłane o stałej porze."""

    enabled: bool = True
    at: HHMM
    """Godzina wysyłki."""
    mode: Literal["only_if_unusual", "always"] = "only_if_unusual"
    """`only_if_unusual`: tylko gdy jest coś nietypowego; `always`: także „bez zmian”."""
    catch_up_minutes: Annotated[int, Field(ge=0, le=240)] = 90
    """Jeśli program był wyłączony o tej godzinie, nadrabia wysyłkę w tym oknie."""


class InstantConfig(Strict):
    """Powiadomienia wysyłane od razu po wykryciu."""

    enabled: bool = True
    lookahead_days: Annotated[int, Field(ge=0, le=7)] = 1
    """Ile dni w przód uznajemy za pilne (0 = tylko dziś, 1 = dziś i jutro)."""
    min_priority: Priority = Priority.high
    """Minimalna waga, żeby wysłać od razu; reszta czeka na podsumowanie."""


class FailureConfig(Strict):
    """Raport o awarii, żeby cisza nie oznaczała po cichu zepsutego programu."""

    enabled: bool = True
    after_hours_without_success: Annotated[int, Field(ge=2, le=168)] = 26
    """Po ilu godzinach bez udanego pobrania wysłać raport."""


class Route(Strict):
    """Reguła kierowania powiadomień do kanałów Apprise (po tagach)."""

    tags: Annotated[list[str], Field(min_length=1)]
    """Tagi z konfiguracji Apprise, np. `rodzice`, `mama`."""
    alert_types: list[AlertType] = list(AlertType)
    """Które typy powiadomień kierować tą regułą."""
    children: list[str] | None = None
    """Id dzieci; `None` oznacza wszystkie."""
    kinds: list[Kind] | None = None
    """Rodzaje odstępstw; `None` oznacza wszystkie."""
    min_priority: Priority = Priority.low
    """Minimalna waga pozycji kierowanych tą regułą."""


class AlertsConfig(Strict):
    """Rytm i routing powiadomień."""

    evening: ScheduledBrief = ScheduledBrief(at=time(19, 0))
    morning: ScheduledBrief = ScheduledBrief(at=time(6, 30))
    instant: InstantConfig = InstantConfig()
    failure: FailureConfig = FailureConfig()
    quiet_hours: TimeWindow | None = TimeWindow(start=time(21, 30), end=time(6, 15))
    """W tych godzinach nic nie wychodzi; pilne rzeczy trafiają do porannego powiadomienia."""
    routes: list[Route] = [Route(tags=["all"])]
    """Reguły routingu; domyślnie wszystko do tagu `all`."""
    priorities: dict[Kind, Priority] = Field(default_factory=dict)
    """Nadpisania domyślnych wag (`DEFAULT_PRIORITIES`)."""

    def priority_of(self, kind: Kind) -> Priority:
        """Zwraca wagę danego rodzaju odstępstwa (z nadpisaniem z konfiguracji).

        Args:
            kind: Rodzaj odstępstwa.

        Returns:
            Waga z `priorities` albo domyślna z `DEFAULT_PRIORITIES`.
        """
        return self.priorities.get(kind, DEFAULT_PRIORITIES[kind])


DEFAULT_PRIORITIES: dict[Kind, Priority] = {
    Kind.early_start: Priority.high,
    Kind.late_start: Priority.normal,
    Kind.early_end: Priority.high,
    Kind.late_end: Priority.high,
    Kind.day_off: Priority.high,
    Kind.cancel: Priority.high,
    Kind.substitution: Priority.low,
    Kind.bring: Priority.high,
    Kind.consent: Priority.high,
    Kind.payment: Priority.normal,
    Kind.trip: Priority.high,
    Kind.test: Priority.normal,
    Kind.homework: Priority.low,
    Kind.change: Priority.high,
    Kind.other: Priority.low,
}
"""Domyślne wagi: wysokie dla tego, co zmienia poranek albo wymaga działania przed wyjściem."""


class Config(Strict):
    """Cała konfiguracja programu."""

    timezone: str = "Europe/Warsaw"
    """Strefa czasowa szkoły; od niej zależą daty „dziś” i „jutro”."""
    data_dir: Path = Path("/data")
    """Katalog na bazę SQLite i blokadę instancji (wolumen kontenera)."""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    retention_days: Annotated[int, Field(ge=30, le=3650)] = 400
    """Starsze dane są usuwane z bazy (minimalizacja danych)."""
    children: Annotated[list[Child], Field(min_length=1, max_length=8)]
    fetch: FetchConfig = FetchConfig()
    llm: LLMConfig = LLMConfig()
    alerts: AlertsConfig = AlertsConfig()

    @model_validator(mode="after")
    def _consistent(self) -> Config:
        """Sprawdza unikalność id dzieci i poprawność odwołań w routingu.

        Returns:
            Ta sama konfiguracja, gdy jest spójna.

        Raises:
            ValueError: Przy zdublowanym id dziecka albo nieznanym id w `routes`.
        """
        ids = [c.id for c in self.children]
        if len(ids) != len(set(ids)):
            raise ValueError("children[].id muszą być unikalne")
        known = set(ids)
        for r in self.alerts.routes:
            if r.children and not set(r.children) <= known:
                raise ValueError(f"route.children zawiera nieznane id: {set(r.children) - known}")
        return self

    def child(self, child_id: str) -> Child:
        """Zwraca dziecko po id.

        Args:
            child_id: Id z konfiguracji.

        Returns:
            Obiekt dziecka.

        Raises:
            KeyError: Gdy nie ma takiego dziecka.
        """
        for c in self.children:
            if c.id == child_id:
                return c
        raise KeyError(child_id)


def _reject_secret_keys(node: object, path: str = "") -> None:
    """Rekurencyjnie odrzuca klucze, które wyglądają na sekrety.

    Klucze z `_ALLOWED_KEYS` to pola konfiguracji, które tylko mówią o
    sekrecie (np. `api_key_required`), więc są przepuszczane.

    Args:
        node: Fragment wczytanego YAML-a.
        path: Ścieżka do fragmentu (do komunikatu błędu).

    Raises:
        ValueError: Gdy któryś klucz pasuje do wzorca sekretu.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k not in _ALLOWED_KEYS and _FORBIDDEN_KEY.search(k):
                raise ValueError(
                    f"klucz '{path}{k}' wygląda na sekret; sekrety podawaj przez *_FILE/env, "
                    "nie w pliku konfiguracyjnym"
                )
            _reject_secret_keys(v, f"{path}{k}.")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reject_secret_keys(v, f"{path}{i}.")


def load_config(path: Path) -> Config:
    """Wczytuje i waliduje plik konfiguracyjny.

    Args:
        path: Ścieżka do pliku YAML.

    Returns:
        Zwalidowana konfiguracja.

    Raises:
        ValueError: Przy kluczu wyglądającym na sekret.
        pydantic.ValidationError: Przy nieznanych polach lub złych wartościach.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _reject_secret_keys(raw)
    return Config.model_validate(raw)


class Credentials(BaseModel):
    """Dane logowania do Librusa (wartości zamaskowane w repr)."""

    login: SecretStr
    password: SecretStr


def _required(name: str) -> SecretStr:
    """Wczytuje wymagany sekret.

    Args:
        name: Nazwa sekretu.

    Returns:
        Wartość sekretu.

    Raises:
        MissingSecretError: Gdy sekretu nie ma.
    """
    value = get_secret(name, required=True)
    if value is None:  # pragma: no cover - get_secret już zgłosiło błąd
        raise MissingSecretError(name)
    return value


def child_credentials(child: Child) -> Credentials:
    """Wczytuje login i hasło dziecka z sekretów `LIBRUS_<ID>_LOGIN/PASSWORD`.

    Args:
        child: Dziecko z konfiguracji.

    Returns:
        Dane logowania.

    Raises:
        MissingSecretError: Gdy brakuje któregoś z sekretów.
    """
    return Credentials(
        login=_required(f"{child.env_prefix}_LOGIN"),
        password=_required(f"{child.env_prefix}_PASSWORD"),
    )


def llm_api_key(cfg: LLMConfig) -> SecretStr | None:
    """Wczytuje klucz API modelu z sekretu `LLM_API_KEY`.

    Args:
        cfg: Konfiguracja modelu (decyduje, czy klucz jest wymagany).

    Returns:
        Klucz albo `None`, gdy nie jest wymagany i go nie podano.
    """
    return get_secret("LLM_API_KEY", required=cfg.api_key_required)


def apprise_config() -> SecretStr:
    """Wczytuje konfigurację kanałów Apprise z sekretu `APPRISE_CONFIG`.

    Treść ma format tekstowy Apprise: w każdej linii `tag1,tag2=URL`.
    URL-e zawierają tokeny (np. bota Telegrama), dlatego to sekret.

    Returns:
        Treść konfiguracji Apprise.
    """
    return _required("APPRISE_CONFIG")
