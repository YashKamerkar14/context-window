import pytest

from context_window.cli import main


def test_worker_once_exits_cleanly() -> None:
    main(["worker", "--once"])


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit):
        main(["nope"])


def test_run_rejects_unknown_beats() -> None:
    with pytest.raises(SystemExit):
        main(["run", "--beat", "sports"])


@pytest.mark.parametrize("trigger", ["schedule", "manual"])
def test_a_scheduled_run_ends_quietly_when_today_is_already_published(
    monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    from context_window import runner

    async def published(*args: object, **kwargs: object) -> None:
        raise runner.AlreadyPublishedError("the briefing for 2026-10-11 is already published")

    monkeypatch.setattr(runner, "run_newsroom", published)
    if trigger == "schedule":
        main(["run", "--trigger", trigger])  # the backup schedule: not a failure
    else:
        with pytest.raises(SystemExit):
            main(["run", "--trigger", trigger])
