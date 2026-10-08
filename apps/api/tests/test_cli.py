import pytest

from context_window.cli import main


def test_worker_once_exits_cleanly() -> None:
    main(["worker", "--once"])


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit):
        main(["nope"])
