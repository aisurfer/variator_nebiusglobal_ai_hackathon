import base64

import pytest
from pydantic import ValidationError

from variator.cloud import (
    EndpointSpec,
    EndpointState,
    InjectedFile,
    JobSpec,
    JobState,
    RegistryCredentials,
    SecretRef,
    VolumeMount,
)
from variator.cloud.specs import GIB


def job(**kw) -> JobSpec:
    return JobSpec(**{"name": "deep-sim-001", "image": "cr.eu-north1.nebius.cloud/e00reg/sim:1", **kw})


def test_job_request_matches_documented_rest_body():
    spec = job(
        args="--flag",
        env={"MODE": "deep"},
        env_secrets={"NEBIUS_API_KEY": "mbsec-e00a@mbsecver-e00b"},
        subnet_id="vpcsubnet-e00s",
        volumes=[VolumeMount(source="storagebucket-e00b", container_path="/data")],
        injected_files=[InjectedFile(container_path="/etc/variator/in.json", content=b'{"a":1}')],
        timeout_s=2 * 3600,
        labels={"app": "variator"},
    )
    body = spec.to_request("project-e00p")
    assert body["metadata"] == {
        "parentId": "project-e00p",
        "name": "deep-sim-001",
        "labels": {"app": "variator"},
    }
    s = body["spec"]
    assert s["image"] == "cr.eu-north1.nebius.cloud/e00reg/sim:1"
    assert (s["platform"], s["preset"]) == ("gpu-l40s-a", "1gpu-8vcpu-32gb")
    assert s["timeout"] == "7200s"
    assert s["args"] == "--flag"
    assert "containerCommand" not in s  # the image ENTRYPOINT stays in charge
    assert s["subnetId"] == "vpcsubnet-e00s"
    assert s["disk"] == {"type": "NETWORK_SSD", "sizeBytes": str(100 * GIB)}  # int64 as string
    assert s["environmentVariables"] == [
        {"name": "MODE", "value": "deep"},
        {
            "name": "NEBIUS_API_KEY",
            "mysteryboxSecret": {"secretId": "mbsec-e00a", "versionId": "mbsecver-e00b"},
        },
    ]
    assert s["volumes"] == [{"source": "storagebucket-e00b", "containerPath": "/data", "mode": "READ_WRITE"}]
    assert base64.b64decode(s["injectedFiles"][0]["content"]) == b'{"a":1}'
    assert "preemptible" not in s and "pricingModel" not in s and "restartAttempts" not in s
    assert "dryRun" not in body
    assert spec.to_request("p", dry_run=True)["dryRun"] is True


def test_preemptible_and_restart_policy():
    s = job(preemptible=True, restart_on_failure=True).to_api()
    assert s["preemptible"] is True
    assert s["pricingModel"] == {"followsSpotPrice": {}}
    assert s["restartAttempts"] == "-1"
    capped = job(preemptible=True, spot_pricing_policy_id="pricingpolicy-e00x").to_api()
    assert capped["pricingModel"] == {"spotPricingPolicy": {"id": "pricingpolicy-e00x"}}


def test_cpu_defaults_and_disk():
    spec = job(platform="cpu-d3", preset="2vcpu-8gb")
    assert not spec.is_gpu
    assert spec.effective_disk_gib == 30
    assert job(disk_size_gib=250).to_api()["disk"]["sizeBytes"] == str(250 * GIB)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_s": 1800}, "greater than or equal to 3600"),
        ({"timeout_s": 169 * 3600}, "less than or equal to"),
        ({"name": "Deep_Sim"}, "lowercase"),
        ({"env": {"A": "1"}, "env_secrets": {"A": "mbsec-1"}}, "both env and env_secrets"),
        ({"env": {"1BAD": "x"}}, "invalid environment variable"),
        ({"platform": "cpu-d3", "preset": "2vcpu-8gb", "preemptible": True}, "only GPU platforms"),
        ({"disk_size_gib": 30}, ">= 50"),
        ({"platform": "cpu-d3", "disk_size_gib": 10}, ">= 20"),
        ({"spot_pricing_policy_id": "pp-1"}, "requires preemptible"),
        ({"working_dir": "relative/dir"}, "clean absolute path"),
        ({"extra_field": 1}, "Extra inputs"),
    ],
)
def test_job_spec_validation(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        job(**kwargs)


def test_injected_file_and_volume_validation():
    with pytest.raises(ValidationError):
        InjectedFile(container_path="/etc/in.json", content=b"x" * (64 * 1024 + 1))
    with pytest.raises(ValidationError, match="clean absolute path"):
        VolumeMount(source="b", container_path="/data/../etc")
    with pytest.raises(ValidationError, match="unique"):
        job(
            volumes=[VolumeMount(source="b", container_path="/data")],
            injected_files=[InjectedFile(container_path="/data", content=b"x")],
        )
    assert VolumeMount(source="b", container_path="/in", mode="ro").to_api()["mode"] == "READ_ONLY"


def test_secret_ref_parsing():
    assert SecretRef.parse("mbsec-1@mbsecver-2") == SecretRef(secret_id="mbsec-1", version_id="mbsecver-2")
    ref = SecretRef.parse("mbsec-1")
    assert ref.version_id is None and ref.to_api() == {"secretId": "mbsec-1"}
    assert ref.selector == "mbsec-1"


def test_registry_credentials():
    inline = RegistryCredentials(username="u", password="p").to_api()
    assert inline == {"username": "u", "password": "p"}
    assert RegistryCredentials(secret_version_id="mbsecver-1").to_api() == {
        "mysteryboxSecretVersion": "mbsecver-1"
    }
    with pytest.raises(ValidationError):
        RegistryCredentials(username="u")
    with pytest.raises(ValidationError):
        RegistryCredentials()


def test_endpoint_request_with_port_and_token():
    spec = EndpointSpec(
        name="variator-app",
        image="img:1",
        platform="cpu-d3",
        preset="2vcpu-8gb",
        container_ports=[8000],
        auth_token="s3cret",
    )
    s = spec.to_api()
    assert s["ports"] == [{"containerPort": 8000, "protocol": "HTTP"}]
    assert s["authToken"] == "s3cret"
    assert "timeout" not in s
    assert "s3cret" not in repr(spec)  # SecretStr keeps the token out of logs
    via_secret = EndpointSpec(name="a", image="i", auth_token_secret="mbsec-9").to_api()
    assert via_secret["authTokenMysteryboxSecret"] == {"secretId": "mbsec-9"}
    assert "authToken" not in EndpointSpec(name="a", image="i").to_api()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"auth_token": "t", "auth_token_secret": "mbsec-1"}, "mutually exclusive"),
        ({"auth_token": "t", "container_ports": [8000, 9000]}, "exactly one HTTP port"),
        ({"container_ports": [70000]}, "1..65535"),
        ({"container_ports": [8000, 8000]}, "unique"),
    ],
)
def test_endpoint_validation(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        EndpointSpec(name="a", image="i", **kwargs)


def test_cli_rendering_matches_documented_flags():
    args = job(
        env={"A": "1", "B": "2"},
        env_secrets={"K": "mbsec-1@mbsecver-2"},
        volumes=[VolumeMount(source="storagebucket-e00b", container_path="/data")],
        subnet_id="vpcsubnet-e00s",
        preemptible=True,
    ).to_cli_args()
    assert args[:4] == ["nebius", "ai", "job", "create"]
    joined = " ".join(args)
    for fragment in (
        "--platform gpu-l40s-a",
        "--preset 1gpu-8vcpu-32gb",
        "--timeout 3600s",
        "--env A=1,B=2",
        "--env-secret K=mbsec-1@mbsecver-2",
        "--volume storagebucket-e00b:/data:rw",
        "--subnet-id vpcsubnet-e00s",
        "--preemptible",
        "--follows-spot-price",
        "--disk-size 100Gi",
    ):
        assert fragment in joined
    endpoint = EndpointSpec(name="a", image="i", auth_token="secret-value", public_ip=True).to_cli_args()
    assert "--container-port" in endpoint and "--public" in endpoint
    assert "secret-value" not in " ".join(endpoint)


def test_states_and_terminality():
    assert {s for s in JobState if s.is_terminal} == {
        JobState.COMPLETED,
        JobState.CANCELLED,
        JobState.FAILED,
        JobState.ERROR,
    }
    assert JobState.IMAGE_PULLING.value == "IMAGE_PULLING"
    assert "FAILED" not in EndpointState.__members__  # endpoints report ERROR instead


def test_with_defaults_fills_subnet_only_when_missing():
    assert job().with_defaults(subnet_id="s1").subnet_id == "s1"
    assert job(subnet_id="mine").with_defaults(subnet_id="s1").subnet_id == "mine"
