# OCI Instance Retry

A small automation script for retrying an Oracle Cloud Infrastructure (OCI) compute-instance launch when the requested shape has no capacity.

I wrote this for a very specific operational problem: OCI Ampere capacity can be unavailable when a launch is attempted, so the script retries for a bounded period instead of requiring me to keep clicking the console manually. When an instance is created successfully, it can send the resulting public IP to Telegram.

## How it works

`retry.py`:

1. loads OCI credentials and the SSH public key from environment variables
2. finds a recent Ubuntu 22.04 image that supports the configured ARM shape
3. attempts to launch the configured instance
4. waits and retries when OCI reports a capacity error
5. backs off when the API is rate-limited
6. sends a Telegram notification after a successful launch

The retry loop is bounded so it can be used from a scheduled CI job without running forever.

## Configuration

The script expects credentials to be supplied through the environment. The current code uses variables such as:

```text
OCI_USER
OCI_FINGERPRINT
OCI_TENANCY
OCI_PRIVATE_KEY
OCI_SSH_PUB_KEY
TG_BOT_TOKEN
TG_CHAT_ID
```

Never commit private OCI keys, Telegram tokens or other secrets to the repository.

The target region, availability domain, subnet, shape, CPU/memory allocation and boot-volume size are currently configured in the script. If you reuse this code, replace those deployment-specific values with your own environment/configuration rather than copying mine.

## Run locally

Install the Python dependencies:

```bash
pip install oci requests
```

Set the required environment variables and run:

```bash
python retry.py
```

## Notes

This is personal infrastructure automation, not a general OCI provisioning library. It assumes a particular instance shape and network setup and should be reviewed before being used in another tenancy.

It also does not bypass OCI quotas or capacity rules; it simply repeats a normal launch request and handles the expected errors more conveniently.
