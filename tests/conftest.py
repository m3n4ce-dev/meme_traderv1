import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_files(monkeypatch, tmp_path):
    """Engines read data/model.json and write config/params.yaml relative to the repo. Point them at a
    temp dir so a model trained on this machine can't change test results and no test can touch
    the real settings file."""
    monkeypatch.setattr("meme_trader.sniper.engine.ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _no_transaction_fetches(monkeypatch):
    """Fork reconciliation fetches the transaction to decode it; tests that don't supply one get a failed lookup
    (status-only evidence) instead of a network call."""
    def offline(*a, **k):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr("meme_trader.sniper.engine.Engine._tx_lookup", staticmethod(offline))
