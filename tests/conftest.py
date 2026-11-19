from contextlib import closing

import pytest

from enterprise_ops.fixtures.seed import seed_baseline
from enterprise_ops.storage.sqlite import connect, initialize


@pytest.fixture
def baseline(tmp_path):
    with closing(connect(tmp_path / "baseline.sqlite")) as connection:
        initialize(connection)
        clock = seed_baseline(connection)
        yield connection, clock
