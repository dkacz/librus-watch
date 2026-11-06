"""Klasy błędów pobierania z Librusa (ADR-011).

Od klasy zależy reakcja programu: `AuthError` wstrzymuje próby do następnego
dnia, `TransientError` jest ponawiany z rosnącym odstępem, `SchemaError`
pomija moduł i trafia do raportu o awarii, `ModuleUnavailable` to tylko
ostrzeżenie. Komunikaty zawierają nazwę modułu i typ przyczyny, nigdy treść
odpowiedzi (mogłaby zawierać dane dzieci albo sesję).
"""

from __future__ import annotations


class LibrusError(Exception):
    """Wspólna baza błędów komunikacji z Librusem."""


class AuthError(LibrusError):
    """Zły login lub hasło, odrzucone logowanie albo wygasła sesja."""


class TransientError(LibrusError):
    """Przejściowy problem: limit czasu, sieć, 5xx, prace serwisowe."""


class SchemaError(LibrusError):
    """Odpowiedź o nieoczekiwanym kształcie (zmiana API lub strony)."""


class ModuleUnavailable(LibrusError):
    """Moduł niedostępny dla konta (403 albo 404), np. szkoła go nie używa."""
