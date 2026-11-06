import stat
from pathlib import Path

import pytest

from librus_watch.lock import AlreadyRunning, InstanceLock


def test_second_instance_is_refused_until_first_exits(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with InstanceLock(data) as first:
        assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
        with pytest.raises(AlreadyRunning, match="w użyciu"), InstanceLock(data):
            pass
    with InstanceLock(data):  # po zwolnieniu można znowu
        pass


def test_lock_is_released_when_block_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError), InstanceLock(tmp_path):
        raise ValueError
    with InstanceLock(tmp_path):
        pass


def test_exit_without_enter_is_harmless(tmp_path: Path) -> None:
    InstanceLock(tmp_path).__exit__(None, None, None)
