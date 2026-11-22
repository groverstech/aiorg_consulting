import pytest

from tests.acceptance import test_autonomous_organisation as org

pytestmark = pytest.mark.postgres


def test_pg_ceo_objective_end_to_end(pg, tenant):
    org.test_ceo_runs_an_objective_end_to_end_without_human_intervention(tenant)
