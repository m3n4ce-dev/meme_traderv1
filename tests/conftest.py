import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_files(monkeypatch, tmp_path):
    """Engines read data/model.json and write config/params.yaml relative to the repo. Point them at a
    temp dir so a model trained on this machine can't change test results and no test can touch
    the real settings file."""
    monkeypatch.setattr("meme_trader.sniper.engine.ROOT", tmp_path)
