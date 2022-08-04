# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time

import pytest
from lightkube.resources.core_v1 import Pod
from tenacity import before_log, retry, stop_after_delay, wait_exponential

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test, helpers):
    spark_operator_charm = await ops_test.build_charm(".")

    spark_resources = {"oci-image": helpers.oci_image("./metadata.yaml", "oci-image")}
    spark_app_name = "spark-k8s"
    await ops_test.model.deploy(
        spark_operator_charm,
        resources=spark_resources,
        application_name=spark_app_name,
        trust=True,
    )
    await ops_test.model.wait_for_idle(
        [spark_app_name], status="active", raise_on_blocked=True, timeout=300
    )

    # juju bug: reports app as active before pods are actually ready
    # https://bugs.launchpad.net/juju/+bug/1981833
    time.sleep(60)

    assert ops_test.model.applications[spark_app_name].units[0].workload_status == "active"


@retry(
    wait=wait_exponential(multiplier=3, min=1, max=10),
    stop=stop_after_delay(60),
    reraise=True,
    before=before_log(log, logging.INFO),
)
def test_spark_application_creation(ops_test, spark_application, lightkube_client):
    app_name = spark_application.metadata.name
    driver_pod_name = f"{app_name}-driver"
    driver_pod = lightkube_client.get(Pod, name=driver_pod_name, namespace=ops_test.model_name)
    assert driver_pod.status.phase == "Succeeded"
