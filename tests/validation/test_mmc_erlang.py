"""AC-EXP-01: M/M/c output matches Erlang C within the 95% CI over >= 30 replications.

Seeds are fixed, so this test is deterministic. Each replication runs 2,000 s of
observation after a 200 s warm-up (rho = 0.8). The analytical value must lie in
the replication-based Student-t interval for mean wait and utilization.
"""

from __future__ import annotations

from domains.toy_queue.analytic import erlang_c

from simcore.api import default_engine
from simcore.experiment.stats import mean_ci
from tests.conftest import MMC

REPLICATIONS = 30
MASTER_SEED = 20240601


def test_AC_EXP_01_mmc_matches_erlang_c() -> None:
    expected = erlang_c(servers=3, arrival_rate=2.4, service_rate=1.0)
    engine = default_engine()
    engine.load_config(MMC).build_world()
    waits, utils, lq = [], [], []
    for rep in range(REPLICATIONS):
        k = engine.run_replication(seed=MASTER_SEED, replication_id=rep).kpis
        waits.append(k["wait_time_mean"])
        utils.append(k["utilization"])
        lq.append(k["queue_length_mean"])
    wait_ci = mean_ci(waits)
    util_ci = mean_ci(utils)
    lq_ci = mean_ci(lq)
    assert wait_ci.low <= expected.mean_wait <= wait_ci.high, (wait_ci, expected)
    assert util_ci.low <= expected.utilization <= util_ci.high, (util_ci, expected)
    assert lq_ci.low <= expected.mean_queue_length <= lq_ci.high, (lq_ci, expected)
    assert wait_ci.relative_half_width < 0.15  # the interval is informative, not vacuous
