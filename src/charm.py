#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
import logging
import traceback
from pathlib import Path
from subprocess import check_call

from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler as KRH
from charmed_kubeflow_chisme.lightkube.batch import apply_many, delete_many
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.core_v1 import ServicePort
from lightkube.resources.admissionregistration_v1 import MutatingWebhookConfiguration
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus
from ops.pebble import ChangeError, Layer

log = logging.getLogger()


class SparkCharm(CharmBase):
    """A charm for creating Spark Applications via the Spark on k8s Operator."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(**self.gen_certs())

        port = ServicePort(int(self.model.config["webhook-port"]), name=f"{self.app.name}")
        self.service_patcher = KubernetesServicePatch(self, [port])

        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")

        self.resource_handler = KRH(
            template_files=self._template_files,
            context=self._context,
            field_manager=self.model.app.name,
        )

        self._mutating_webhook_name = f"{self.model.app.name}-webhook-config"
        self._container_name = "spark"
        self.container = self.unit.get_container(self._container_name)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.spark_pebble_ready, self._on_spark_pebble_ready)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.remove, self._on_remove)

    @property
    def _template_files(self):
        src_dir = Path("src")
        manifests = [file for file in glob.glob(f"{src_dir}/*.yaml")]
        return manifests

    @property
    def _context(self):
        context = {
            "app_name": self.model.app.name,
            "model_name": self.model.name,
        }
        return context

    @property
    def _spark_operator_layer(self) -> Layer:
        pebble_layer = {
            "summary": "spark layer",
            "description": "pebble config layer for spark-k8s",
            "services": {
                self._container_name: {
                    "override": "replace",
                    "summary": "Spark Operator layer",
                    "startup": "enabled",
                    "command": (
                        f"/usr/bin/tini -s -- /usr/bin/spark-operator -v=2 "
                        "-logtostderr "
                        f"-namespace={self.model.name} "
                        "-enable-ui-service=true "
                        "-controller-threads=10 "
                        "-resync-interval=30 "
                        "-enable-batch-scheduler=false "
                        "-enable-metrics=true "
                        "-metrics-labels=app_type "
                        f"-metrics-port={self.model.config['metrics-port']} "
                        "-metrics-endpoint=/metrics "
                        "-enable-resource-quota-enforcement=false "
                        "-enable-webhook=true "
                        f"-webhook-svc-namespace={self.model.name} "
                        f"-webhook-port={self.model.config['webhook-port']} "
                        f"-webhook-svc-name={self.model.app.name} "
                        f"-webhook-config-name={self._mutating_webhook_name} "
                        f"-webhook-namespace-selector=model.juju.is/name={self.model.name} "
                        "-webhook-fail-on-error=true"
                    ),
                }
            },
        }
        return Layer(pebble_layer)

    def _update_layer(self) -> None:
        """Updates the Pebble configuration layer if changed."""

        current_layer = self.container.get_plan()
        new_layer = self._spark_operator_layer

        if current_layer.services != new_layer.services:
            self.container.add_layer(self._container_name, new_layer, combine=True)
            try:
                log.info("Pebble plan updated with new configuration, replanning")
                self.container.replan()
            except ChangeError as e:
                log.error(traceback.format_exc())
                self.unit.status = BlockedStatus("Failed to replan")
                raise e
                return

    def _update_webhook_certs(self) -> None:
        self.container.push("/etc/webhook-certs/ca-cert.pem", self._stored.ca, make_dirs=True)
        self.container.push(
            "/etc/webhook-certs/server-cert.pem", self._stored.cert, make_dirs=True
        )
        self.container.push("/etc/webhook-certs/server-key.pem", self._stored.key, make_dirs=True)
        log.info("Pushed webhook keys and certs to spark container")

    def _on_install(self, _):
        """Event Handler for install event."""
        self.resource_handler.apply()

        if self.container.can_connect():
            self._update_webhook_certs()

    def _on_spark_pebble_ready(self, event):
        """Event Handler for spark pebble ready event."""
        if not self.container.can_connect():
            self.unit.status = MaintenanceStatus("Waiting to connect to spark container")
            event.defer()
            return

        self.unit.status = MaintenanceStatus("Configuring Spark Charm")
        self._update_webhook_certs()
        self._update_layer()

        self.unit.status = ActiveStatus()

    def _on_config_changed(self, event):
        """Event Handler for config changed event."""
        if not self.container.can_connect():
            self.unit.status = MaintenanceStatus("Waiting to connect to spark container")
            event.defer()
            return

        self.unit.status = MaintenanceStatus("Updating Spark Charm's Configurations")
        self.service_patcher._patch(self.service_patcher)
        self._update_webhook_certs()
        self._update_layer()

        self.unit.status = ActiveStatus()

    def _on_remove(self, _):
        """Event Handler for remove event."""
        manifests = self.resource_handler.render_manifests(force_recompute=False)
        resources = apply_many(self.lightkube_client, manifests)
        try:
            delete_many(self.lightkube_client, resources)
            self.lightkube_client.delete(MutatingWebhookConfiguration, self._mutating_webhook_name)
        except ApiError as e:
            log.warning(str(e))

    def gen_certs(self):
        """Generate webhook keys and certs."""
        model = self.model.name
        app = self.model.app.name
        Path("/run/ssl.conf").write_text(
            f"""[ req ]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn
[ dn ]
C = GB
ST = Canonical
L = Canonical
O = Canonical
OU = Canonical
CN = 127.0.0.1
[ req_ext ]
subjectAltName = @alt_names
[ alt_names ]
DNS.1 = {app}
DNS.2 = {app}.{model}
DNS.3 = {app}.{model}.svc
DNS.4 = {app}.{model}.svc.cluster
DNS.5 = {app}.{model}.svc.cluster.local
IP.1 = 127.0.0.1
[ v3_ext ]
authorityKeyIdentifier=keyid,issuer:always
basicConstraints=CA:FALSE
keyUsage=keyEncipherment,dataEncipherment,digitalSignature
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=@alt_names"""
        )

        check_call(["openssl", "genrsa", "-out", "/run/ca.key", "2048"])
        check_call(["openssl", "genrsa", "-out", "/run/server.key", "2048"])
        check_call(
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-nodes",
                "-days",
                "3650",
                "-key",
                "/run/ca.key",
                "-subj",
                "/CN=127.0.0.1",
                "-out",
                "/run/ca.crt",
            ]
        )
        check_call(
            [
                "openssl",
                "req",
                "-new",
                "-sha256",
                "-key",
                "/run/server.key",
                "-out",
                "/run/server.csr",
                "-subj",
                f"/CN={app}.{model}.svc",
                "-config",
                "/run/ssl.conf",
            ]
        )
        check_call(
            [
                "openssl",
                "x509",
                "-req",
                "-sha256",
                "-in",
                "/run/server.csr",
                "-CA",
                "/run/ca.crt",
                "-CAkey",
                "/run/ca.key",
                "-CAcreateserial",
                "-out",
                "/run/cert.pem",
                "-days",
                "365",
                "-extensions",
                "v3_ext",
                "-extfile",
                "/run/ssl.conf",
            ]
        )

        return {
            "cert": Path("/run/cert.pem").read_text(),
            "key": Path("/run/server.key").read_text(),
            "ca": Path("/run/ca.crt").read_text(),
        }


if __name__ == "__main__":
    main(SparkCharm)
