from app import playground_worker


def test_tick_advances_playgrounds_even_when_a_model_task_was_processed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        playground_worker,
        "rehearse_tick",
        lambda db, environment: calls.append(("model", environment)) or True,
    )
    monkeypatch.setattr(
        playground_worker,
        "advance_running_playgrounds",
        lambda db: calls.append(("clock", None)) or True,
    )

    assert playground_worker.tick(object()) is True
    assert calls == [("model", "playground"), ("clock", None)]
