#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from subprocess import check_call

from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from jinja2 import Environment, FileSystemLoader
from lightkube import Client, codecs
from lightkube.core.exceptions import ApiError
from lightkube.models.core_v1 import ServicePort
from lightkube.resources.admissionregistration_v1 import MutatingWebhookConfiguration
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus

log = logging.getLogger()


class SparkCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(**self.gen_certs())
        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")
        port = ServicePort(int(self.model.config["webhook-port"]), name=f"{self.app.name}")
        self.service_patcher = KubernetesServicePatch(self, [port])

        self.env = Environment(loader=FileSystemLoader("src"))
        self.env.globals["app_name"] = self.model.app.name
        self.env.globals["model_name"] = self.model.name

        self._mutating_webhook_name = f"{self.model.app.name}-webhook-config"

        self.framework.observe(self.on.spark_pebble_ready, self._on_spark_pebble_ready)
        self.framework.observe(self.on.remove, self._on_remove)

    def _on_spark_pebble_ready(self, event):
        container = event.workload

        # TODO: put paths in config
        container.push("/etc/webhook-certs/ca-cert.pem", self._stored.ca, make_dirs=True)
        container.push("/etc/webhook-certs/server-cert.pem", self._stored.cert, make_dirs=True)
        container.push("/etc/webhook-certs/server-key.pem", self._stored.key, make_dirs=True)

        pebble_layer = {
            "summary": "spark layer",
            "description": "pebble config layer for spark-k8s",
            "services": {
                "spark": {
                    "override": "replace",
                    "summary": "apply image?",
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

        with open(Path("src/crds.yaml")) as f:
            for obj in codecs.load_all_yaml(f, context={}, create_resources_for_crds=True):
                self.lightkube_client.apply(obj)

        with open(Path("src/rbac.yaml")) as f:
            for obj in codecs.load_all_yaml(f, context={}, template_env=self.env):
                self.lightkube_client.apply(obj)

        container.add_layer("spark", pebble_layer, combine=True)
        container.autostart()

        self.unit.status = ActiveStatus()

    def _on_remove(self, event):
        try:
            self.lightkube_client.delete(MutatingWebhookConfiguration, self._mutating_webhook_name)
        except ApiError as e:
            log.warn(str(e))

    def gen_certs(self):
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
