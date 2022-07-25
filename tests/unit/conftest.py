# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

import pytest
from ops.testing import Harness

from charm import SparkCharm


@pytest.fixture
def harness():
    harness = Harness(SparkCharm)
    harness.set_leader(True)
    return harness


@pytest.fixture()
def mocked_lightkube_client(mocker):
    mocked_client = mocker.patch("charm.Client")
    mocked_client.return_value = MagicMock()
    yield mocked_client


@pytest.fixture()
def mocked_cert(mocker):
    mocked_cert = mocker.patch("charm.SparkCharm.gen_certs")
    mocked_cert.return_value = {
        "cert": "fake-cert",
        "key": "fake-server-key",
        "ca": "fake-ca-cert",
    }
    yield mocked_cert


@pytest.fixture()
def mocked_kubernetes_service_patcher(mocker):
    mocked_patcher = mocker.patch("charm.KubernetesServicePatch")
    mocked_patcher.return_value = MagicMock()
    yield mocked_patcher


@pytest.fixture()
def mocked_resource_handler(mocker):
    mocked_resource_handler = mocker.patch("charm.KRH")
    mocked_resource_handler.return_value = MagicMock()
    yield mocked_resource_handler
