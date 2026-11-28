"""
Rendered-manifest tests for the console chart.

They run `helm template` (helm on PATH, or HELM) and assert on the objects it
renders: `pytest chart/tests -p no:cacheprovider --no-cov` from console/.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent
HELM = os.environ.get("HELM") or shutil.which("helm")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")

OIDC = {
    "database": {"existingSecret": "console-db"},
    "auth": {
        "secretKey": {"existingSecret": "console-session"},
        "publicUrl": "https://console.example.com",
        "oidc": {
            "issuer": "https://sso.example.com",
            "clientId": "console",
            "clientSecret": {"existingSecret": "console-oidc"},
        },
    },
    "stores": {
        "stores": [
            {
                "name": "prod",
                "region": "us-east-1",
                "bucket": "b",
                "prefix": "terraform/{deployment}",
            }
        ]
    },
}


def merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        if value and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def helm(values, *args, name="t"):
    # through a values file, as an install reads it: numbers arrive as floats
    return subprocess.run(
        [HELM, "template", name, str(CHART), "-f", "-", *args],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
    )


def render(**extra):
    result = helm(merge(OIDC, extra))
    assert result.returncode == 0, result.stderr
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def refused(values):
    result = helm(values)
    assert result.returncode != 0
    return result.stderr


def env(container):
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}


def web(objects):
    deployment = objects[("Deployment", "t-tfworker-console")]
    return deployment["spec"]["template"]["spec"]


class TestWeb:
    def test_defaults(self):
        objects = render()
        pod = web(objects)
        (container,) = pod["containers"]
        values = env(container)
        assert container["args"] == ["serve"]
        assert values["TFWORKER_CONSOLE_AUTH"] == "oidc"
        assert values["TFWORKER_CONSOLE_AUTO_MIGRATE"] == "false"
        assert values["TFWORKER_CONSOLE_DATABASE_URL"] == {
            "secretKeyRef": {"name": "console-db", "key": "database-url"}
        }
        assert values["TFWORKER_CONSOLE_SECRET_KEY"]["secretKeyRef"]["name"] == (
            "console-session"
        )
        assert values["TFWORKER_CONSOLE_OIDC_CLIENT_SECRET"]["secretKeyRef"] == {
            "name": "console-oidc",
            "key": "client-secret",
        }
        assert values["TFWORKER_CONSOLE_OIDC_ISSUER"] == "https://sso.example.com"
        assert values["TFWORKER_CONSOLE_PUBLIC_URL"] == "https://console.example.com"
        assert (
            values["TFWORKER_CONSOLE_STORES_FILE"] == "/config/stores.yaml/stores.yaml"
        )
        assert "TFWORKER_CONSOLE_GROUPS_FILE" not in values
        assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"

    def test_large_numbers(self):
        objects = render(
            auth={"sessionMaxAge": 2592000},
            lockStaleAfter=2592000,
            reconcile={"mode": "inPod", "interval": 1000000},
            metrics={"mode": "port", "port": 19090},
        )
        values = env(web(objects)["containers"][0])
        # never 2.592e+06, which the console can't read as a number of seconds
        assert values["TFWORKER_CONSOLE_SESSION_MAX_AGE"] == "2592000"
        assert float(values["TFWORKER_CONSOLE_LOCK_STALE_AFTER"]) == 2592000
        assert float(values["TFWORKER_CONSOLE_RECONCILE_INTERVAL"]) == 1000000
        assert values["TFWORKER_CONSOLE_METRICS_PORT"] == "19090"

    def test_startup_probe(self):
        pod = web(render())
        (container,) = pod["containers"]
        startup = container["startupProbe"]
        assert startup["httpGet"]["path"] == "/livez"
        assert startup["periodSeconds"] * startup["failureThreshold"] >= 300
        assert pod["automountServiceAccountToken"] is False
        assert pod["terminationGracePeriodSeconds"] == 45

    def test_extra_env_and_volumes(self):
        objects = render(
            extraEnv=[{"name": "PGSSLMODE", "value": "verify-full"}],
            extraVolumes=[{"name": "ca", "configMap": {"name": "corp-ca"}}],
            extraVolumeMounts=[
                {"name": "ca", "mountPath": "/etc/ca", "readOnly": True}
            ],
        )
        cron = objects[("CronJob", "t-tfworker-console-reconcile")]
        job = objects[("Job", "t-tfworker-console-migrate")]["spec"]["template"]["spec"]
        for pod in (
            web(objects),
            cron["spec"]["jobTemplate"]["spec"]["template"]["spec"],
            job,
        ):
            (container,) = pod["containers"]
            assert env(container)["PGSSLMODE"] == "verify-full"
            assert "/etc/ca" in {m["mountPath"] for m in container["volumeMounts"]}
            assert "ca" in {v["name"] for v in pod["volumes"]}

    def test_image(self):
        (container,) = web(render())["containers"]
        assert container["image"] == "tfworker-console:0.0.0-dev"
        (container,) = web(render(image={"tag": "0.3.0"}))["containers"]
        assert container["image"] == "tfworker-console:0.3.0"
        pod = web(render(imagePullSecrets=[{"name": "registry"}]))
        assert pod["imagePullSecrets"] == [{"name": "registry"}]

    def test_roles_config_map(self):
        objects = render(auth={"rolesConfigMap": "console-roles"})
        pod = web(objects)
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["roles"]["configMap"]["name"] == "console-roles"
        values = env(pod["containers"][0])
        assert values["TFWORKER_CONSOLE_ROLES_FILE"] == "/config/roles.yaml/roles.yaml"

    def test_hardened(self):
        pod = web(render())
        assert pod["securityContext"]["runAsNonRoot"] is True
        (container,) = pod["containers"]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        assert "/tmp" in mounts
        objects = render()
        cron = objects[("CronJob", "t-tfworker-console-reconcile")]
        job = objects[("Job", "t-tfworker-console-migrate")]["spec"]["template"]["spec"]
        for pod in (cron["spec"]["jobTemplate"]["spec"]["template"]["spec"], job):
            assert pod["securityContext"]["runAsNonRoot"] is True
            (container,) = pod["containers"]
            assert container["securityContext"]["readOnlyRootFilesystem"] is True

    def test_inline_config(self):
        objects = render(groups={"groups": [{"name": "Production"}]})
        data = objects[("ConfigMap", "t-tfworker-console-config")]["data"]
        assert yaml.safe_load(data["stores.yaml"])["stores"][0]["name"] == "prod"
        assert "groups.yaml" in data
        values = env(web(objects)["containers"][0])
        assert (
            values["TFWORKER_CONSOLE_GROUPS_FILE"] == "/config/groups.yaml/groups.yaml"
        )

    def test_existing_config_map(self):
        objects = render(stores={}, storesConfigMap="my-stores")
        assert ("ConfigMap", "t-tfworker-console-config") not in objects
        volumes = {v["name"]: v for v in web(objects)["volumes"]}
        assert volumes["stores"]["configMap"]["name"] == "my-stores"

    def test_config_changes_roll_the_pods(self):
        def checksum(objects):
            pod = objects[("Deployment", "t-tfworker-console")]["spec"]["template"]
            return pod["metadata"]["annotations"]["checksum/config"]

        other = {"stores": [{"name": "dev", "region": "us-east-1", "bucket": "d"}]}
        assert checksum(render()) != checksum(render(stores=other))

    def test_local_auth(self):
        objects = render(auth={"mode": "local"})
        values = env(web(objects)["containers"][0])
        assert values["TFWORKER_CONSOLE_AUTH"] == "local"
        assert not any(name.startswith("TFWORKER_CONSOLE_OIDC") for name in values)

    def test_ingress(self):
        objects = render(
            ingress={
                "enabled": True,
                "className": "nginx",
                "hosts": [{"host": "console.example.com"}],
            }
        )
        ingress = objects[("Ingress", "t-tfworker-console")]
        assert ingress["spec"]["ingressClassName"] == "nginx"
        (path,) = ingress["spec"]["rules"][0]["http"]["paths"]
        assert path["path"] == "/"
        assert path["backend"]["service"]["port"]["name"] == "http"

    def test_ingress_tls(self):
        tls = [{"hosts": ["console.example.com"], "secretName": "console-tls"}]
        objects = render(
            ingress={
                "enabled": True,
                "hosts": [{"host": "console.example.com"}],
                "tls": tls,
            }
        )
        assert objects[("Ingress", "t-tfworker-console")]["spec"]["tls"] == tls


class TestCredentials:
    def test_irsa(self):
        role = "arn:aws:iam::123456789012:role/console"
        objects = render(
            serviceAccount={"annotations": {"eks.amazonaws.com/role-arn": role}}
        )
        account = objects[("ServiceAccount", "t-tfworker-console")]
        assert account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == role
        pod = web(objects)
        assert pod["serviceAccountName"] == "t-tfworker-console"
        assert "AWS_CONFIG_FILE" not in env(pod["containers"][0])

    def test_profiles_secret(self):
        objects = render(awsProfiles={"existingSecret": "aws-profiles"})
        pod = web(objects)
        values = env(pod["containers"][0])
        assert values["AWS_CONFIG_FILE"] == "/aws/config"
        assert values["AWS_SHARED_CREDENTIALS_FILE"] == "/aws/credentials"
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["aws"]["secret"]["secretName"] == "aws-profiles"
        cron = objects[("CronJob", "t-tfworker-console-reconcile")]
        job = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert env(job["containers"][0])["AWS_CONFIG_FILE"] == "/aws/config"


class TestReconcile:
    def test_cron_job(self):
        objects = render()
        values = env(web(objects)["containers"][0])
        assert values["TFWORKER_CONSOLE_RECONCILE_INTERVAL"] == "0"
        cron = objects[("CronJob", "t-tfworker-console-reconcile")]
        assert cron["spec"]["concurrencyPolicy"] == "Forbid"
        assert cron["spec"]["startingDeadlineSeconds"] == 300
        assert cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 1800
        job = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        (container,) = job["containers"]
        assert container["args"] == ["reconcile"]
        assert "TFWORKER_CONSOLE_STORES_FILE" in env(container)

    def test_long_release_names(self):
        result = helm(OIDC, name="a" * 53)
        assert result.returncode == 0, result.stderr
        for doc in yaml.safe_load_all(result.stdout):
            if doc and doc["kind"] == "CronJob":
                # Kubernetes limits CronJob names to 52 characters
                assert len(doc["metadata"]["name"]) <= 52

    def test_in_pod(self):
        objects = render(reconcile={"mode": "inPod", "interval": 120})
        assert not any(kind == "CronJob" for kind, _ in objects)
        values = env(web(objects)["containers"][0])
        assert values["TFWORKER_CONSOLE_RECONCILE_INTERVAL"] == "120"


class TestMigrations:
    def test_hook(self):
        job = render()[("Job", "t-tfworker-console-migrate")]
        hooks = job["metadata"]["annotations"]["helm.sh/hook"]
        assert hooks == "pre-install,pre-upgrade"
        spec = job["spec"]["template"]["spec"]
        (container,) = spec["containers"]
        assert container["args"] == ["db", "upgrade"]
        # the chart's service account isn't there yet on a first install
        assert "serviceAccountName" not in spec
        assert list(env(container)) == ["TFWORKER_CONSOLE_DATABASE_URL"]
        assert spec["automountServiceAccountToken"] is False

    def test_existing_service_account(self):
        objects = render(serviceAccount={"create": False, "name": "console"})
        assert ("ServiceAccount", "t-tfworker-console") not in objects
        job = objects[("Job", "t-tfworker-console-migrate")]["spec"]["template"]["spec"]
        assert job["serviceAccountName"] == "console"
        assert web(objects)["serviceAccountName"] == "console"

    def test_off(self):
        objects = render(migrations={"enabled": False})
        assert ("Job", "t-tfworker-console-migrate") not in objects


class TestEvents:
    def test_events(self):
        queue = "https://sqs.us-east-1.amazonaws.com/123456789012/runs"
        objects = render(
            events={"enabled": True, "queueUrl": queue, "region": "us-east-1"}
        )
        values = env(web(objects)["containers"][0])
        assert values["TFWORKER_CONSOLE_EVENTS_QUEUE_URL"] == queue
        assert values["TFWORKER_CONSOLE_EVENTS_REGION"] == "us-east-1"
        assert "TFWORKER_CONSOLE_EVENTS_ROLE_ARN" not in values


class TestMetrics:
    def test_off(self):
        objects = render()
        values = env(web(objects)["containers"][0])
        assert values["TFWORKER_CONSOLE_METRICS"] == "off"
        assert not any(kind == "ServiceMonitor" for kind, _ in objects)

    def test_unquoted_off(self):
        objects = render(metrics={"mode": False})
        assert env(web(objects)["containers"][0])["TFWORKER_CONSOLE_METRICS"] == "off"

    def test_port(self):
        objects = render(metrics={"mode": "port"}, service={"type": "LoadBalancer"})
        pod = objects[("Deployment", "t-tfworker-console")]["spec"]["template"]
        (container,) = pod["spec"]["containers"]
        assert {"name": "metrics", "containerPort": 9090} in container["ports"]
        assert pod["metadata"]["annotations"]["prometheus.io/port"] == "9090"
        # the unauthenticated port stays off the console's own Service
        ports = objects[("Service", "t-tfworker-console")]["spec"]["ports"]
        assert [p["name"] for p in ports] == ["http"]
        service = objects[("Service", "t-tfworker-console-metrics")]["spec"]
        assert service["type"] == "ClusterIP"
        assert [p["port"] for p in service["ports"]] == [9090]

    def test_port_with_a_service_monitor(self):
        objects = render(metrics={"mode": "port", "serviceMonitor": {"enabled": True}})
        pod = objects[("Deployment", "t-tfworker-console")]["spec"]["template"]
        # the ServiceMonitor scrapes; annotations would make some setups scrape twice
        assert "prometheus.io/port" not in pod["metadata"]["annotations"]
        monitor = objects[("ServiceMonitor", "t-tfworker-console")]
        selector = monitor["spec"]["selector"]["matchLabels"]
        assert selector["app.kubernetes.io/component"] == "metrics"
        metrics = objects[("Service", "t-tfworker-console-metrics")]["metadata"]
        assert metrics["labels"]["app.kubernetes.io/component"] == "metrics"
        (endpoint,) = monitor["spec"]["endpoints"]
        assert endpoint["port"] == "metrics"
        assert "authorization" not in endpoint

    def test_token(self):
        objects = render(
            metrics={
                "mode": "token",
                "serviceMonitor": {
                    "enabled": True,
                    "tokenSecret": {"name": "console-metrics"},
                },
            }
        )
        ports = objects[("Service", "t-tfworker-console")]["spec"]["ports"]
        assert [p["name"] for p in ports] == ["http"]
        assert ("Service", "t-tfworker-console-metrics") not in objects
        monitor = objects[("ServiceMonitor", "t-tfworker-console")]
        selector = monitor["spec"]["selector"]["matchLabels"]
        assert selector["app.kubernetes.io/component"] == "web"
        (endpoint,) = objects[("ServiceMonitor", "t-tfworker-console")]["spec"][
            "endpoints"
        ]
        assert endpoint["port"] == "http"
        assert endpoint["authorization"]["credentials"] == {
            "name": "console-metrics",
            "key": "token",
        }


class TestRefused:
    @pytest.mark.parametrize(
        "values, message",
        [
            ({"database": {"existingSecret": ""}}, "database.existingSecret"),
            ({"auth": {"mode": "none"}}, "auth.allowNone"),
            ({"auth": {"mode": "magic"}}, "auth.mode must be"),
            ({"auth": {"secretKey": {"existingSecret": ""}}}, "secretKey"),
            ({"auth": {"publicUrl": ""}}, "auth.publicUrl"),
            ({"auth": {"oidc": {"issuer": ""}}}, "auth.oidc.issuer"),
            ({"reconcile": {"mode": "sometimes"}}, "reconcile.mode"),
            ({"metrics": {"mode": "always"}}, "metrics.mode"),
            ({"events": {"enabled": True}}, "events.queueUrl"),
            (
                {"events": {"enabled": True, "queueUrl": "q"}, "stores": {}},
                "events need the stores",
            ),
            ({"storesConfigMap": "mine"}, "set stores inline or name its ConfigMap"),
            ({"ingress": {"enabled": True}}, "ingress.hosts"),
            (
                {"metrics": {"serviceMonitor": {"enabled": True}}},
                "metrics.mode token or port",
            ),
            (
                {"metrics": {"mode": "token", "serviceMonitor": {"enabled": True}}},
                "tokenSecret.name",
            ),
        ],
    )
    def test_refused(self, values, message):
        assert message in refused(merge(OIDC, values))

    def test_auth_none_for_development(self):
        objects = render(auth={"mode": "none", "allowNone": True})
        values = env(web(objects)["containers"][0])
        assert values["TFWORKER_CONSOLE_AUTH"] == "none"
        assert values["TFWORKER_CONSOLE_AUTH_ALLOW_REMOTE_NONE"] == "true"
        assert "TFWORKER_CONSOLE_SECRET_KEY" not in values


def test_lint():
    result = subprocess.run(
        [HELM, "lint", str(CHART), "-f", "-"],
        input=yaml.safe_dump(OIDC),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
