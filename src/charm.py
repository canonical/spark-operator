#!/usr/bin/env python3

import logging
from pathlib import Path
from subprocess import check_call

import yaml
from ops.framework import StoredState
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus

from oci_image import OCIImageResource, OCIImageResourceError

log = logging.getLogger()


class SparkCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        if not self.model.unit.is_leader():
            log.info("Not a leader, skipping set_pod_spec")
            self.model.unit.status = ActiveStatus()
            return

        self._stored.set_default(**self.gen_certs())
        self.image = OCIImageResource(self, "oci-image")
        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(self.on.config_changed, self.set_pod_spec)

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
                            "--namespace",
                            self.model.name,
                            f"--enable-metrics={metrics_enabled}",
                            "--metrics-port",
                            self.model.config["metrics-port"],
                            "--metrics-labels=app_type",
                            "--enable-webhook=true",
                            "--webhook-svc-namespace",
                            self.model.name,
                            "--webhook-port",
                            self.model.config["webhook-port"],
                            "--webhook-svc-name",
                            self.model.app.name,
                            "--webhook-config-name",
                            f"{self.model.app.name}-config",
                            "--webhook-namespace-selector",
                            f"juju-model={self.model.name}",
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


if __name__ == "__main__":
    main(SparkCharm)
