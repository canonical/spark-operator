#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from subprocess import check_call

import yaml
from charmed_kubeflow_chisme.lightkube.batch import apply_many, delete_many
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from jinja2 import Environment, FileSystemLoader
from lightkube import Client, codecs
from lightkube.generic_resource import create_global_resource
from lightkube.models.core_v1 import ServicePort
from lightkube.resources.admissionregistration_v1 import MutatingWebhookConfiguration
from lightkube.resources.core_v1 import ConfigMap, Service
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus

log = logging.getLogger()


class SparkCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._check_leader()

        self._stored.set_default(**self.gen_certs())
        self.lightkube_client = Client(
            namespace=self.model.name, field_manager="lightkube"
        )
        port = ServicePort(int(self.model.config['webhook-port']), name=f"{self.app.name}")
        self.service_patcher = KubernetesServicePatch(self, [port])

        self.env = Environment(loader=FileSystemLoader("src"))

        self._mutating_webhook_name = f"{self.model.app.name}-webhook-config"

        self.framework.observe(self.on.spark_pebble_ready, self._on_spark_pebble_ready)
        self.framework.observe(self.on.remove, self._on_remove)
        # self.framework.observe(self.on.install, self.set_pod_spec)
        # self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        # self.framework.observe(self.on.config_changed, self.set_pod_spec)

    def _check_leader(self):
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            raise CheckFailedError("Waiting for leadership", WaitingStatus)

    def _on_spark_pebble_ready(self, event):
        container = event.workload

        # TODO: put paths in config
        container.push(
            "/etc/webhook-certs/ca-cert.pem", self._stored.ca, make_dirs=True
        )
        container.push(
            "/etc/webhook-certs/server-cert.pem", self._stored.cert, make_dirs=True
        )
        container.push(
            "/etc/webhook-certs/server-key.pem", self._stored.key, make_dirs=True
        )

        # create webhook svc
        # service_template = self.env.get_template("manifests.yaml.j2")
        # service_manifest = service_template.render(
        #     webhook_port=self.model.config["webhook-port"], app_name=self.app.name
        # )
        # for obj in codecs.load_all_yaml(service_manifest):
        #     log.debug(f"Deploying {obj.metadata.name} of kind {obj.kind}")
        #     self.lightkube_client.apply(obj, namespace=obj.metadata.namespace)

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
        container.add_layer("spark", pebble_layer, combine=True)
        container.autostart()

        self.unit.status = ActiveStatus()

    def _on_remove(self, event):
        self.lightkube_client.delete(
            MutatingWebhookConfiguration, self._mutating_webhook_name
        )
        # TODO
        for obj in self.lightkube_client.list(
            resource,
            labels={"app.juju.is/created-by": f"{self.app_name}"},
            namespace=self.model.name,
        ):
            self.lightkube_client.delete(
                obj,
                namespace=self.model.name,
            )

    def set_pod_spec(self, event):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            log.info(e)
            return

        metrics_enabled = str(bool(self.model.relations["prometheus"])).lower()
        self.model.unit.status = MaintenanceStatus("Setting pod spec")
        self.model.pod.set_spec(
            {
                "version": 3,
                "serviceAccount": {
                    "roles": [
                        {
                            "global": True,
                            "rules": [
                                {
                                    "apiGroups": [""],
                                    "resources": ["pods"],
                                    "verbs": ["*"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["configmaps"],
                                    "verbs": ["*"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["services", "secrets"],
                                    "verbs": ["create", "get", "delete"],
                                },
                                {
                                    "apiGroups": ["extensions"],
                                    "resources": ["ingresses"],
                                    "verbs": ["create", "get", "delete"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["nodes"],
                                    "verbs": ["get"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["resourcequotas"],
                                    "verbs": ["get", "list", "watch"],
                                },
                                {
                                    "apiGroups": [""],
                                    "resources": ["events"],
                                    "verbs": ["create", "update", "patch"],
                                },
                                {
                                    "apiGroups": ["apiextensions.k8s.io"],
                                    "resources": ["customresourcedefinitions"],
                                    "verbs": ["create", "get", "update", "delete"],
                                },
                                {
                                    "apiGroups": ["admissionregistration.k8s.io"],
                                    "resources": [
                                        "mutatingwebhookconfigurations",
                                        "validatingwebhookconfigurations",
                                    ],
                                    "verbs": ["create", "get", "update", "delete"],
                                },
                                {
                                    "apiGroups": ["sparkoperator.k8s.io"],
                                    "resources": [
                                        "sparkapplications",
                                        "scheduledsparkapplications",
                                        "sparkapplications/status",
                                        "scheduledsparkapplications/status",
                                    ],
                                    "verbs": ["*"],
                                },
                                {
                                    "apiGroups": ["scheduling.volcano.sh"],
                                    "resources": [
                                        "podgroups",
                                        "queues",
                                        "queues/status",
                                    ],
                                    "verbs": [
                                        "get",
                                        "list",
                                        "watch",
                                        "create",
                                        "delete",
                                        "update",
                                    ],
                                },
                            ],
                        }
                    ],
                },
                "containers": [
                    {
                        "name": "sparkoperator",
                        "args": [
                            "--v=4",
                            "--logtostderr",
                            f"--namespace={self.model.name}",
                            f"--enable-metrics={metrics_enabled}",
                            f"--metrics-port={self.model.config['metrics-port']}",
                            "--metrics-labels=app_type",
                            "--enable-webhook=true",
                            f"--webhook-svc-namespace={self.model.name}",
                            f"--webhook-port={self.model.config['webhook-port']}",
                            f"--webhook-svc-name={self.model.app.name}",
                            f"--webhook-config-name={self.model.app.name}-config",
                            f"--webhook-namespace-selector=model.juju.is/name={self.model.name}",
                            "--webhook-fail-on-error=true",
                        ],
                        "imageDetails": image_details,
                        "ports": [
                            {
                                "name": "metrics",
                                "containerPort": int(self.model.config["metrics-port"]),
                            },
                            {
                                "name": "webhook",
                                "containerPort": int(self.model.config["webhook-port"]),
                            },
                        ],
                        "volumeConfig": [
                            {
                                "name": "certs",
                                "mountPath": "/etc/webhook-certs",
                                "files": [
                                    {
                                        "path": "server-cert.pem",
                                        "content": self._stored.cert,
                                    },
                                    {
                                        "path": "server-key.pem",
                                        "content": self._stored.key,
                                    },
                                    {
                                        "path": "ca-cert.pem",
                                        "content": self._stored.ca,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
            k8s_resources={
                "kubernetesResources": {
                    "customResourceDefinitions": [
                        {"name": crd["metadata"]["name"], "spec": crd["spec"]}
                        for crd in yaml.safe_load_all(Path("src/crds.yaml").read_text())
                    ],
                    "pod": {
                        "annotations": {
                            "prometheus.io/scrape": "true",
                            "prometheus.io/port": self.model.config["metrics-port"],
                            "prometheus.io/path": "/metrics",
                        }
                    },
                }
            },
        )
        self.model.unit.status = ActiveStatus()

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


class CheckFailedError(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=None):
        super().__init__()

        self.msg = str(msg)
        self.status_type = status_type
        self.status = status_type(msg)


if __name__ == "__main__":
    main(SparkCharm)
