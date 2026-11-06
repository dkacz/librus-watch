"""Moduły JSON-owego API Synergii (ADR-012) i ich pobieranie.

Każdy moduł jest pobierany osobno. Błąd jednego modułu (niedostępny, zmiana
kształtu, przejściowy problem) nie zatrzymuje pozostałych: trafia do
`GatewayData.failed` albo `GatewayData.unavailable`. Tylko `AuthError` przerywa
całe pobieranie, bo dotyczy sesji, a nie modułu.

Gateway sprawdza wyłącznie kształt odpowiedzi (klucz główny, listy słowników).
Pola rekordów interpretuje adapter, jedyne miejsce znające ich znaczenie.
Kształty opisuje `docs/librus-apix-notes.md`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, Protocol

from librus_watch.errors import AuthError, ModuleUnavailable, SchemaError, TransientError

log = logging.getLogger(__name__)

Record = dict[str, Any]
Shape = Literal["list", "object", "timetable"]


class JsonSource(Protocol):
    """To, czego gateway potrzebuje od sesji (`LibrusSession.get_json`)."""

    def get_json(self, path: str) -> Any:
        """Pobiera ścieżkę API i zwraca zdekodowany JSON."""
        ...


def _monday(d: date) -> date:
    """Poniedziałek tygodnia, w którym leży data."""
    return d - timedelta(days=d.weekday())


def _single(path: str) -> Callable[[date, int], list[str]]:
    """Moduł bez parametrów: zawsze jedna ścieżka."""
    return lambda _today, _weeks: [path]


def _timetable_weeks(today: date, weeks: int) -> list[str]:
    """Tygodnie planu lekcji od bieżącego."""
    start = _monday(today)
    return [f"Timetables?weekStart={start + timedelta(weeks=w)}" for w in range(weeks)]


def _other_activities(today: date, weeks: int) -> list[str]:
    """Zajęcia dodatkowe od początku tygodnia na tyle tygodni co plan."""
    start = _monday(today)
    end = start + timedelta(weeks=weeks) - timedelta(days=1)
    return [
        f"Timetables/OtherActivitiesRegister?dateFrom={start}&dateTo={end}"
        "&hideOutdatedEntries=false"
    ]


@dataclass(frozen=True)
class Module:
    """Moduł API.

    Attributes:
        name: Nazwa używana w programie i w logach.
        root: Klucz główny odpowiedzi z danymi.
        shape: `list` (lista rekordów), `object` (jeden rekord),
            `timetable` (data -> sloty -> lekcje).
        paths: Ścieżki do pobrania dla danego dnia i liczby tygodni planu.
    """

    name: str
    root: str
    shape: Shape
    paths: Callable[[date, int], list[str]]


def _m(name: str, root: str, shape: Shape = "list", path: str | None = None) -> Module:
    """Skrót do modułów z jedną stałą ścieżką."""
    return Module(name, root, shape, _single(path or name))


MODULES: tuple[Module, ...] = (
    _m("Me", "Me", "object"),
    _m("Classes", "Class", "object"),
    Module("Timetables", "Timetable", "timetable", _timetable_weeks),
    _m("TimetableEntries", "TimetableEntries"),
    _m("Substitutions", "Substitutions", path="Calendars/Substitutions"),
    _m("ClassFreeDays", "ClassFreeDays"),
    _m("SchoolFreeDays", "SchoolFreeDays"),
    _m("HomeWorks", "HomeWorks"),
    _m("HomeWorkCategories", "Categories", path="HomeWorks/Categories"),
    _m("HomeWorkAssignments", "HomeWorkAssignments"),
    _m("SchoolNotices", "SchoolNotices"),
    _m("Notes", "Notes"),
    _m("NoteCategories", "Categories", path="Notes/Categories"),
    _m("Grades", "Grades"),
    _m("GradeComments", "Comments", path="Grades/Comments"),
    _m("Realizations", "Realizations"),
    _m("ParentTeacherConferences", "ParentTeacherConferences"),
    _m("SchoolTrips", "Data"),
    Module("OtherActivities", "data", "list", _other_activities),
    _m("Subjects", "Subjects"),
    _m("Users", "Users"),
)
"""Moduły pobierane w każdym cyklu, w kolejności: najpierw tożsamość i plan."""


@dataclass
class GatewayData:
    """Wynik pobrania modułów dla jednego dziecka.

    Attributes:
        records: Rekordy pobranych modułów. Dla `timetable` każda lekcja ma
            dodane pola `_day` (data) i `_slot` (indeks slotu w dniu).
        unavailable: Moduły niedostępne dla konta (403/404).
        failed: Moduły z błędem w tym cyklu: nazwa -> klasa błędu.
    """

    records: dict[str, list[Record]] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Czy wszystkie moduły pobrały się bez błędu (niedostępne nie są błędem)."""
        return not self.failed


def ref_id(value: Any) -> str | None:
    """Normalizuje identyfikator albo odwołanie `{"Id": ...}` do tekstu.

    W API typy `Id` są niespójne (liczby, napisy, napisy z prefiksem), więc
    porównujemy je zawsze jako tekst.

    Args:
        value: `Id` albo obiekt z polem `Id`.

    Returns:
        Identyfikator jako tekst albo `None`, gdy go brak.
    """
    if isinstance(value, dict):
        value = value.get("Id")
    if value is None or value == "":
        return None
    return str(value)


def _records(module: Module, path: str, payload: Any) -> list[Record]:
    """Wyciąga rekordy z odpowiedzi, sprawdzając jej kształt.

    Args:
        module: Moduł.
        path: Pobrana ścieżka (do komunikatu błędu).
        payload: Odpowiedź JSON.

    Returns:
        Lista rekordów.

    Raises:
        SchemaError: Gdy brakuje klucza głównego albo typy się nie zgadzają.
    """
    if not isinstance(payload, dict) or module.root not in payload:
        raise SchemaError(f"{module.name}: brak klucza {module.root}")
    data = payload[module.root]
    if module.shape == "object":
        if not isinstance(data, dict):
            raise SchemaError(f"{module.name}: {module.root} nie jest obiektem")
        return [data]
    if module.shape == "list":
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise SchemaError(f"{module.name}: {module.root} nie jest listą obiektów")
        pagination = payload.get("pagination")
        if isinstance(pagination, dict) and int(pagination.get("total") or 0) > len(data):
            log.warning("%s: niepełna lista (strona 1 z wielu)", module.name)
        return data
    if not isinstance(data, dict):
        raise SchemaError(f"{module.name}: {module.root} nie jest słownikiem dat")
    lessons: list[Record] = []
    for day, slots in data.items():
        if not isinstance(slots, list):
            raise SchemaError(f"{module.name}: dzień bez listy slotów")
        for i, slot in enumerate(slots):
            if not isinstance(slot, list) or not all(isinstance(ls, dict) for ls in slot):
                raise SchemaError(f"{module.name}: slot nie jest listą lekcji")
            lessons.extend({**ls, "_day": day, "_slot": i} for ls in slot)
    return lessons


def fetch_all(
    source: JsonSource,
    today: date,
    *,
    weeks: int = 2,
    modules: Sequence[Module] = MODULES,
    skip: frozenset[str] = frozenset(),
) -> GatewayData:
    """Pobiera moduły API, każdy niezależnie.

    Args:
        source: Sesja (`LibrusSession`) albo jej zamiennik w testach.
        today: Bieżąca data (od niej zależą tygodnie planu).
        weeks: Ile tygodni planu lekcji pobrać.
        modules: Moduły do pobrania.
        skip: Nazwy modułów wyłączonych w konfiguracji.

    Returns:
        Rekordy i lista modułów z błędami.

    Raises:
        AuthError: Gdy sesja przestała działać; dalsze moduły nie mają sensu.
    """
    out = GatewayData()
    for module in modules:
        if module.name in skip:
            continue
        try:
            records: list[Record] = []
            for path in module.paths(today, weeks):
                records.extend(_records(module, path, source.get_json(path)))
        except AuthError:
            raise
        except ModuleUnavailable:
            out.unavailable.append(module.name)
            log.warning("moduł %s niedostępny dla konta", module.name)
            continue
        except (TransientError, SchemaError) as e:
            out.failed[module.name] = type(e).__name__
            level = logging.ERROR if isinstance(e, SchemaError) else logging.WARNING
            log.log(level, "moduł %s: %s", module.name, e)
            continue
        out.records[module.name] = records
        log.debug("moduł %s: %d rekordów", module.name, len(records))
    return out
