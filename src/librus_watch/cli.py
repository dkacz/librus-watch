"""Polecenia programu (`librus-watch ...`).

Na razie `fetch`: jeden cykl pobrania dla wszystkich dzieci i raport liczb.
Pełny zestaw poleceń (usługa, podsumowania, test powiadomień) powstaje w E7.

Kody wyjścia: 0 sukces, 1 błąd pobierania (moduł albo dziecko), 2 błąd
konfiguracji lub sekretów, 3 inna instancja używa katalogu danych.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from librus_watch.config import ConfigError, child_credentials, load_config
from librus_watch.errors import AuthError, TransientError
from librus_watch.lock import AlreadyRunning, InstanceLock
from librus_watch.secret_env import MissingSecretError, setup_logging
from librus_watch.store import Store
from librus_watch.sync import LoginBlocked, SyncReport, login_child, sync_child

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FETCH, EXIT_CONFIG, EXIT_RUNNING = 0, 1, 2, 3
MAX_FINDINGS = 25
"""Ile odstępstw wypisać w raporcie (reszta jako liczba)."""


def _parser() -> argparse.ArgumentParser:
    """Parser argumentów wiersza poleceń."""
    parser = argparse.ArgumentParser(prog="librus-watch")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("LIBRUS_WATCH_CONFIG", "config.yaml")),
        help="plik konfiguracji (domyślnie $LIBRUS_WATCH_CONFIG albo config.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="jeden cykl pobrania danych i raport liczb")
    fetch.add_argument("--dry-run", action="store_true", help="bez zapisu w bazie")
    fetch.add_argument("--child", help="tylko to dziecko (id z konfiguracji)")
    fetch.add_argument(
        "--force",
        action="store_true",
        help="loguj mimo blokady po błędzie logowania i dziennego limitu (ostrożnie)",
    )
    return parser


def format_report(r: SyncReport) -> str:
    """Raport cyklu w kilku liniach, bez treści z Librusa.

    Args:
        r: Raport dziecka.

    Returns:
        Tekst raportu.
    """
    lines = [f"{r.child}{' (dry-run)' if r.dry_run else ''}:"]
    lines.append("  rekordy: " + ", ".join(f"{k}={v}" for k, v in sorted(r.records.items())))
    if r.not_fetched:
        lines.append("  niepobrane (cache lub ignorowane): " + ", ".join(r.not_fetched))
    if r.unavailable:
        lines.append("  niedostępne: " + ", ".join(r.unavailable))
    if r.failed:
        lines.append("  błędy: " + ", ".join(f"{k} ({v})" for k, v in sorted(r.failed.items())))
    if r.bad_records:
        lines.append(
            "  pominięte rekordy: "
            + ", ".join(f"{k}={v}" for k, v in sorted(r.bad_records.items()))
        )
    for kind, counter in sorted(r.changes.items()):
        parts = ", ".join(f"{k}={counter[k]}" for k in ("new", "changed", "same") if counter[k])
        lines.append(f"  {kind}: {parts}")
    if r.bodies:
        lines.append(f"  pobrane treści wiadomości: {r.bodies}")
    if r.removed:
        lines.append(f"  usunięte wpisy: {r.removed}")
    label = "odstępstwa" if r.dry_run else "nowe odstępstwa"
    lines.append(
        f"  {label}: {len(r.findings)}" + (f", wycofane: {r.retracted}" if r.retracted else "")
    )
    for f in sorted(r.findings, key=lambda f: (f.day, f.kind.value))[:MAX_FINDINGS]:
        at = f" {f.at:%H:%M}" if f.at else ""
        lines.append(f"    {f.day:%Y-%m-%d}{at} {f.kind.value} ({f.origin.value})")
    if len(r.findings) > MAX_FINDINGS:
        lines.append(f"    … i jeszcze {len(r.findings) - MAX_FINDINGS}")
    return "\n".join(lines)


def cmd_fetch(args: argparse.Namespace) -> int:
    """Polecenie `fetch`.

    Args:
        args: Argumenty z parsera.

    Returns:
        Kod wyjścia.
    """
    try:
        cfg = load_config(args.config)
    except (ConfigError, ValidationError, ValueError) as e:
        print(f"błąd konfiguracji: {e}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(cfg.log_level)
    children = [c for c in cfg.children if args.child in (None, c.id)]
    if not children:
        print(f"nieznane dziecko: {args.child}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        creds = {c.id: child_credentials(c) for c in children}
    except MissingSecretError as e:
        print(f"błąd sekretów: {e}", file=sys.stderr)
        return EXIT_CONFIG

    code = EXIT_OK
    try:
        with InstanceLock(cfg.data_dir):
            store = Store(cfg.data_dir)
            try:
                for child in children:
                    now = datetime.now(ZoneInfo(cfg.timezone)).replace(tzinfo=None)
                    try:
                        session = login_child(
                            cfg, store, child.id, creds[child.id], now, force=args.force
                        )
                        report = sync_child(
                            cfg, child.id, session, store, now=now, dry_run=args.dry_run
                        )
                    except LoginBlocked as e:
                        log.warning("dziecko %s: pominięte, %s", child.id, e)
                        print(f"{child.id}: pominięte ({e})")
                        code = EXIT_FETCH
                        continue
                    except (AuthError, TransientError) as e:
                        log.error("dziecko %s: %s (%s)", child.id, e, type(e).__name__)
                        print(f"{child.id}: błąd ({type(e).__name__})")
                        code = EXIT_FETCH
                        continue
                    print(format_report(report))
                    if not report.ok:
                        code = EXIT_FETCH
            finally:
                store.close()
    except AlreadyRunning as e:
        print(str(e), file=sys.stderr)
        return EXIT_RUNNING
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Punkt wejścia `librus-watch`.

    Args:
        argv: Argumenty (domyślnie z `sys.argv`).

    Returns:
        Kod wyjścia.
    """
    args = _parser().parse_args(argv)
    if args.command == "fetch":
        return cmd_fetch(args)
    return EXIT_CONFIG  # pragma: no cover - argparse wymusza znane polecenie


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
