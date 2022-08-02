# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
from ops.model import ActiveStatus, WaitingStatus


def test_pebble_ready_event(
    harness,
    mocked_lightkube_client,
    mocked_cert,
    mocked_kubernetes_service_patcher,
    mocked_resource_handler,
):
    harness.begin()

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


def test_config_changed_cannot_connect(
    harness,
    mocked_lightkube_client,
    mocked_cert,
    mocked_kubernetes_service_patcher,
    mocked_resource_handler,
):
    harness.begin()

    harness.set_can_connect("spark", False)
    harness.charm.on.config_changed.emit()

    assert isinstance(harness.charm.unit.status, WaitingStatus)


def test_config_changed_metrics_port(
    harness,
    mocked_lightkube_client,
    mocked_cert,
    mocked_kubernetes_service_patcher,
    mocked_resource_handler,
):
    harness.begin()

    harness.container_pebble_ready("spark")

    plan_1 = harness.get_container_pebble_plan("spark").to_dict()["services"]
    assert "-metrics-port=10254" in plan_1["spark"]["command"]

    harness.update_config({"metrics-port": "1234"})
    plan_2 = harness.get_container_pebble_plan("spark").to_dict()["services"]

    assert "-metrics-port=1234" in plan_2["spark"]["command"]
