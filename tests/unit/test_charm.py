# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import pytest
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness

from charm import SparkCharm


@pytest.fixture
def harness():
    return Harness(SparkCharm)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


def test_successful_install(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()

    assert harness.charm.model.unit.status == ActiveStatus()
