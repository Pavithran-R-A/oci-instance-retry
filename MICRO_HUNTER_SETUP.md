# Run the A1 hunter on the already-obtained Oracle Micro

## Status and hard constraints

The exact-instance OCI dynamic group `VennilaA1MicroHunter` and policy
`VennilaA1MicroHunterPolicy` are provisioned by the separate bootstrap workflow.
The group matches **only** `vennila-oracle-micro`'s OCI instance OCID. The
instance-principal runtime does not store the user's OCI signing key or Telegram
bot key. IAM propagation may take up to an hour.

**Do not terminate, reboot, reshape, reimage, or replace the Micro.**
The Micro and the A1 are distinct Always Free resources. Both can coexist.

Oracle's standard *Compute Instance Run Command* does **not** officially support
the Canonical Ubuntu image used on this Micro, so it is not a safe deployment route.
An existing authorized SSH client is required to install the user-space service.
The installer does not change OCI networking, public IP, SSH authorized keys, boot
volume, or shape.

## Codex deployment command

**Run as an authorized operator with a matching SSH private key available locally**.
Do not paste the private key into ChatGPT, GitHub issues, or source control.

```bash
ssh -i /secure/path/to/micro_private_key ubuntu@129.225.93.220
```

Inside the Micro (or as the remote command over SSH), run:

```bash
git clone --depth 1 https://github.com/Pavithran-R-A/oci-instance-retry.git
cd oci-instance-retry
sudo bash install_micro_hunter.sh
```

The install creates an unprivileged `oci-hunter` system user and runs a
single-request Python service under a systemd timer approximately every five
minutes. It uses OCI instance principal authentication directly against OCI
metadata (no OCI API private key on disk). It prioritizes the A1 at
2 OCPUs / 12 GB RAM and a 50 GB boot volume and fails closed when it cannot
inventory tenancy-wide Always Free usage. On an A1 success, it writes
`/var/lib/oci-hunter/claimed`; the timer will stop invoking the workload.

The installer limits the process to a 450 MB memory cgroup and low CPU weight.
It does not install Docker, Chromium, n8n or any unrelated services. It is
idempotent, supports updates from `main`, and leaves other services alone.

## Verify without revealing secrets

```bash
sudo systemctl is-enabled oci-a1-hunter.timer
sudo systemctl list-timers --all oci-a1-hunter.timer
sudo journalctl -u oci-a1-hunter.service -n 40 --no-pager
sudo systemctl start oci-a1-hunter.service
sudo journalctl -u oci-a1-hunter.service -n 80 --no-pager
```

Expected log outcomes include `NO_CAPACITY`, `CAPACITY_AVAILABLE`,
`ALREADY_CLAIMED`, `CLAIMED`, or a bounded `ERROR`. A successful timer
installation is **not** proof that the credentials/IAM and OCI probe work; verify
the first live systemd invocation before reporting success.

For self-hosted successful claims, Telegram notifications are **not configured**
by this script: it intentionally does not read GitHub encrypted bot secrets.
Verify the new A1 instance directly in OCI. The existing GitHub/ChatGPT watcher
can separately detect the new instance on its next real OCI check.

## Keep the Micro safe

- Leave the existing 1 GB E2 Micro running; the installer does not terminate it.
- Do not generate artificial CPU load to evade Oracle's idle reclaim criteria.
- This is a legitimate but low-utilization task, so Oracle may still reclaim an
  otherwise idle Always Free VM under its published policy.
- Keep the code and configuration versioned in GitHub and re-installable. Snapshot
  or back up important persistent data within the actual free storage allowances.
- Review OCI Free Tier usage and billing regularly; instance principals grant
  specific OCI API permissions but cannot promise zero cost outside the free
  usage envelope.
- Wait for policy propagation after IAM creation; do not retry unauthorized
  requests aggressively.
- Only after confirming the A1 booted and SSH works should the Micro be
  repurposed or a backup/deletion decision be made.

## Stop or uninstall

```bash
sudo systemctl disable --now oci-a1-hunter.timer
```

The timer can be stopped without deleting the existing Micro. The A1 Micro IAM
dynamic group and policy are separate resources and should be removed when no
longer necessary.
