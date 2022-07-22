# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
from unittest.mock import patch, MagicMock

import pytest
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness

from charm import SparkCharm


@pytest.fixture
def harness():
    return Harness(SparkCharm)


@patch("charm.KubernetesServicePatch", lambda x, y: None)
@patch(
    "charm.SparkCharm.gen_certs",
    lambda _: {"cert": "fake-cert", "key": "fake-server-key", "ca": "fake-ca-cert"},
)
def test_pebble_ready_event(harness):
    harness.set_leader(True)
    harness.begin()

    harness.charm.resource_handler.apply = MagicMock()
    harness.charm.resource_handler.apply.return_value = None

    harness.set_can_connect("spark", True)
    initial_plan = harness.get_container_pebble_plan("spark")
    assert initial_plan.to_yaml() == "{}\n"

    harness.container_pebble_ready("spark")
    assert isinstance(harness.charm.unit.status, ActiveStatus)

    # After configuration run, plan should be populated
    plan = harness.get_container_pebble_plan("spark").to_dict()["services"]

    assert (
        "status_set",
        "maintenance",
        "Configuring Spark Charm",
        {"is_app": False},
    ) in harness._get_backend_calls()
    assert "spark" in plan
    assert "spark-operator" in plan["spark"]["command"]
