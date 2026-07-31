from app.worker.tasks import ping


def test_ping_task_runs_eagerly_and_echoes(celery_eager) -> None:
    result = ping.apply(args=["hello"]).get()
    assert result["echo"] == "hello"
    assert result["worker"] == "lexloom"
