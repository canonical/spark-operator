# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from pathlib import Path
from urllib.parse import urlencode

import pytest
import yaml
from lightkube import Client
from lightkube.generic_resource import create_namespaced_resource

log = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def helpers():
    return Helpers()


@pytest.fixture(scope="module")
def lightkube_client():
    return Client()


@pytest.fixture(scope="module")
def spark_application(ops_test, lightkube_client):
    """Creates a SparkApplication resource in test namespace"""
    # This allows lightkube to use the sparkoperator api and returns a class for the custom resource
    spark_app = create_namespaced_resource(
        group="sparkoperator.k8s.io",
        version="v1beta2",
        kind="SparkApplication",
        plural="sparkapplications",
    )

    # Attempted to use lightkube's load_all_yaml function, but it did not recognize the resource
    # https://github.com/gtsystem/lightkube/issues/18
    # Instead, create a spark application using the class returned above
    yaml_file_path = "./examples/spark-pi.yaml"
    with open(yaml_file_path, "r") as stream:
        parsed_yaml = yaml.safe_load(stream)
        app = spark_app(parsed_yaml)
        lightkube_client.create(app, namespace=ops_test.model_name)
        return app


class Helpers:
    @staticmethod
    def oci_image(metadata_file: str, image_name: str) -> str:
        """Find upstream source for a container image.
        Args:
            metadata_file: string path of metadata YAML file relative
                to top level charm directory
            image_name: OCI container image string name as defined in
                metadata.yaml file
        Returns:
            upstream image source
        Raises:
            FileNotFoundError: if metadata_file path is invalid
            ValueError: if upstream source for image name can not be found
        """
        metadata = yaml.safe_load(Path(metadata_file).read_text())

        resources = metadata.get("resources", {})
        if not resources:
            raise ValueError("No resources found")

        image = resources.get(image_name, {})
        if not image:
            raise ValueError(f"{image_name} image not found")

        upstream_source = image.get("upstream-source", "")
        if not upstream_source:
            raise ValueError("Upstream source not found")

        return upstream_source

    async def juju_run(self, unit, cmd):
        """Run a command on a unit and return the output."""
        result = await unit.run(cmd)
        code = result.results["Code"]
        stdout = result.results.get("Stdout")
        stderr = result.results.get("Stderr")
        assert code == "0", f"{cmd} failed ({code}): {stderr or stdout}"
        return stdout

    async def query_prometheus(self, ops_test, query):
        prometheus_unit = ops_test.model.applications["prometheus-k8s"].units[0]
        assert prometheus_unit.workload_status == "active"

        # Query pods.
        query = {"query": query}
        qs = urlencode(query)
        url = f"http://localhost:9090/api/v1/query?{qs}"
        output = await self.juju_run(prometheus_unit, f"curl '{url}'")
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            log.error(f"Failed to parse query results: {output}")
            raise
