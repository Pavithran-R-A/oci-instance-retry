# OCI Always Free A1 Claimer

A small, bounded deployment utility for claiming an Oracle Cloud Infrastructure
**Always Free** Ampere A1 VM when the home region temporarily has no host capacity.

The current target is:

- Region: `ap-hyderabad-1`
- Shape: `VM.Standard.A1.Flex`
- Size: **2 OCPUs / 12 GB RAM**
- Boot volume: **50 GB**
- Image: Ubuntu 22.04 ARM
- Instance name: `vennila-oracle-vm`

The workflow is intentionally conservative: it checks the tenancy first, asks OCI
for a Compute Capacity Report, and only sends a real launch request when capacity
looks available (or when the report API itself is unavailable). It sends at most
**one real launch request per scheduled workflow run**.

## Why the schedule is every five minutes

GitHub Actions supports scheduled workflows as frequently as once every five
minutes. OCI documents Out-of-Host-Capacity as temporary and recommends waiting a
few minutes before trying again. The workflow therefore runs at `2/5 * * * *`:
minute 2, 7, 12, and so on.

The offset avoids the busiest start-of-hour GitHub scheduling window. A random
0-15 second jitter is also added before each OCI probe.

The script does **not** run a tight loop. If OCI throttles the API, it stops that
run and waits for the next scheduled execution.

## Capacity-aware flow

Each run:

1. validates the OCI API configuration
2. verifies that the configured region is the tenancy home region
3. looks for an already-created `vennila-oracle-vm`
4. checks existing A1 CPU/RAM and block-volume usage
5. refuses to launch if the requested VM would exceed the Always Free guardrails
6. requests a Compute Capacity Report for 2 OCPUs / 12 GB
7. if capacity is unavailable, exits normally
8. if capacity is available, submits one launch request without pinning a fault domain
9. sends the IP over Telegram after a successful launch
10. disables the GitHub workflow after success so further retries stop

The public GitHub log deliberately does not print OCI resource OCIDs, the VM public
IP, API credentials, SSH keys, or Telegram credentials.

## Required GitHub Actions secrets

Repository **Settings → Secrets and variables → Actions → Secrets** must contain:

```text
OCI_USER
OCI_FINGERPRINT
OCI_TENANCY
OCI_PRIVATE_KEY
OCI_SSH_PUB_KEY
TG_BOT_TOKEN
TG_CHAT_ID
```

The five OCI identity/key values must belong to the same tenancy. `TG_BOT_TOKEN`
and `TG_CHAT_ID` are optional; without them the claimer still works but Telegram
notification is disabled.

`OCI_SUBNET_ID` is optional. If it is absent, the script safely auto-selects the
only public-IP-capable subnet (or the unique default public subnet). If multiple
eligible subnets exist, it stops and asks for `OCI_SUBNET_ID` rather than guessing.

Do **not** commit any of these values to source control.

## Optional environment overrides

The defaults are suitable for this deployment:

```text
OCI_REGION=ap-hyderabad-1
OCI_DISPLAY_NAME=vennila-oracle-vm
START_JITTER_SECONDS=15
```

The script also supports optional `OCI_COMPARTMENT_ID` and
`OCI_AVAILABILITY_DOMAIN` values. If omitted, the root tenancy compartment and
available home-region ADs are discovered automatically.

## Safety model

This repository is designed to fail closed:

- A1 usage above the configured Always Free envelope stops the launch.
- Total block storage that would exceed 200 GB stops the launch.
- A target VM that already exists stops duplicate creation.
- The launch request does not specify a fault domain, allowing OCI to choose the
  best available one.
- API throttling does not trigger an immediate retry burst.
- Capacity errors are treated as expected and retried by the next scheduled run.
- Only a successful capacity check can normally progress to a launch.

This automation does not bypass Oracle quotas, capacity controls, or billing
rules. It repeatedly performs normal OCI API operations within the configured
free-tier guardrails.

## Local validation

```bash
python -m pip install -r requirements.txt
python -m py_compile retry.py
```

For a real local run, export the same environment variables listed above and run:

```bash
python retry.py
```

Keep the repository free of credentials and deployment secrets if it is public.
