import json

import requests
from tenacity import retry, stop_after_attempt, wait_fixed

import hvac


@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def store_keys(server, token, deployment, pubkey, privkey):
    """Store the keys in the vault server."""
    client = hvac.Client(url=server, token=token)

    with open(pubkey, "r") as key:
        pubkey_data = key.read()
    with open(privkey, "r") as key:
        privkey_data = key.read()

    client.write("secret/{}/ssh/public_key".format(deployment), key=pubkey_data)
    client.write("secret/{}/ssh/private_key".format(deployment), key=privkey_data)


@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def check_keys(server, token, deployment):
    """True/False check if keys exist for deployment."""
    client = hvac.Client(url=server, token=token)

    pub = client.read("secret/{}/ssh/public_key".format(deployment))
    priv = client.read("secret/{}/ssh/private_key".format(deployment))

    if pub is not None and priv is not None:
        return True
    return False


@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def update_service_token_role(server, token):
    """
    Check if role exists for token signing, if not, create it.
    """
    client = hvac.Client(url=server, token=token)
    role = client.read("pki/roles/token-signing")

    params = {
        "max_ttl": "0s",
        "allow_localhost": False,
        "allow_any_name": True,
        "enforce_hostnames": False,
        "allow_ip_sans": False,
        "server_flag": False,
        "client_flag": False,
        "code_signing_flag": True,
        "require_cn": False,
    }

    if role is None:
        client.write("pki/roles/token-signing", **params)
    return None


@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def check_service_token_cert(server, token, deployment):
    """True/False check if token signing certs exist for deployment."""
    client = hvac.Client(url=server, token=token)
    signing_cert = client.read("secret/{}/certs/token-signing".format(deployment))

    if signing_cert is not None:
        return True
    return False


@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def store_service_token_cert(server, token, deployment, cert, key):
    """Store a certificate and key in vault."""
    client = hvac.Client(url=server, token=token)
    client.write("secret/{}/certs/token-signing".format(deployment), key=key, cert=cert)
    return None


@retry(wait=wait_fixed(2), stop=stop_after_attempt(5))
def generate_service_token_cert(server, token, deployment):
    """
    Generate a certificate from vault.

    Can't use the HVAC client since it has no PKI interface.
    """
    update_service_token_role(server, token)

    endpoint = "v1/pki/issue/token-signing"

    request_data = {"common_name": "{}_token-signing"}

    request_headers = {"X-Vault-Token": token}

    r = requests.post(
        "{}/{}".format(server, endpoint),
        data=json.dumps(request_data),
        headers=request_headers,
        timeout=60,
    )

    r.raise_for_status()

    store_service_token_cert(
        server,
        token,
        deployment,
        r.json()["data"]["certificate"],
        r.json()["data"]["private_key"],
    )
    return None
