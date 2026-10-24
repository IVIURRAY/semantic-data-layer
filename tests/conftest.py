import os
from pathlib import Path

import pytest

from semlayer import DataLayer
from semlayer.seed import seed

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("OMS_DSN", "postgresql://semlayer:semlayer@localhost:5432/oms")


@pytest.fixture(scope="session")
def dl(tmp_path_factory) -> DataLayer:
    """Seed all four stores (deterministic) and build a DataLayer over them.

    File stores go to a temp dir; the OMS is (re)seeded in the Postgres at
    OMS_DSN. Seeding is deterministic, so this is safe against the compose DB.
    """
    import psycopg

    try:
        psycopg.connect(DSN, connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.skip(f"Postgres not reachable at {DSN}; run `make test` to use docker compose")
    data_dir = tmp_path_factory.mktemp("data")
    seed(DSN, data_dir)
    os.environ["OMS_DSN"], os.environ["DATA_DIR"] = DSN, str(data_dir)
    return DataLayer.from_dir(ROOT / "model")


def rows(result, key):
    """Index a result DataFrame by one column, for readable assertions."""
    return result.df.set_index(key).to_dict("index")
