import datetime as dt
import os
import random
import re
import sys
import time
import uuid

import oci
import requests


# ---- Public/safe configuration ------------------------------------------------
# Keep deployment identifiers and credentials in GitHub Actions secrets.
USER = os.environ["OCI_USER"]
FINGERPRINT = os.environ["OCI_FINGERPRINT"]
TENANCY = os.environ["OCI_TENANCY"]
PRIVATE_KEY = os.environ["OCI_PRIVATE_KEY"]
SSH_PUB_KEY = os.environ["OCI_SSH_PUB_KEY"]
SUBNET_ID = os.environ["OCI_SUBNET_ID"]

REGION = os.getenv("OCI_REGION", "ap-hyderabad-1").strip()
TARGET_COMPARTMENT = os.getenv("OCI_COMPARTMENT_ID", "").strip() or TENANCY
AD_OVERRIDE = os.getenv("OCI_AVAILABILITY_DOMAIN", "").strip()

DISPLAY_NAME = os.getenv("OCI_DISPLAY_NAME", "vennila-oracle-vm").strip()
SHAPE = "VM.Standard.A1.Flex"

# Current OCI Always Free envelope for A1 Free Tier tenancies (2026).
OCPUS = 2
MEMORY_GB = 12
BOOT_GB = 50
MAX_FREE_A1_OCPUS = 2
MAX_FREE_A1_MEMORY_GB = 12
MAX_FREE_BLOCK_GB = 200

# Optional Telegram notification. Empty/missing values simply disable Telegram.
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

# GitHub's minimum cron interval is five minutes. A small jitter prevents all
# claimers from hitting OCI on the same second without materially slowing us.
START_JITTER_SECONDS = int(os.getenv("START_JITTER_SECONDS", "15"))

OCID_RE = re.compile(r"ocid1\.[A-Za-z0-9._:-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def sanitize(value):
    text = str(value)
    text = OCID_RE.sub("<ocid-redacted>", text)
    text = IPV4_RE.sub("<ip-redacted>", text)
    return text


def log(message):
    stamp = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {sanitize(message)}", flush=True)


def set_output(name, value):
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def notify(message):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        log(f"Telegram notification failed: {type(exc).__name__}")


def write_private_key():
    path = "/tmp/oci_key.pem"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(PRIVATE_KEY)
    os.chmod(path, 0o600)
    return path


def all_results(callable_, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(
        callable_, *args, **kwargs
    ).data


def build_config():
    config = {
        "user": USER,
        "fingerprint": FINGERPRINT,
        "tenancy": TENANCY,
        "region": REGION,
        "key_file": write_private_key(),
    }
    oci.config.validate_config(config)
    return config


def get_home_region(identity_client):
    subscriptions = identity_client.list_region_subscriptions(TENANCY).data
    home = next(
        (
            subscription.region_name
            for subscription in subscriptions
            if getattr(subscription, "is_home_region", False)
        ),
        None,
    )
    if not home:
        raise RuntimeError("Could not determine the tenancy home region.")
    if REGION != home:
        raise RuntimeError(
            f"Configured region {REGION} is not the tenancy home region {home}. "
            "Always Free Compute must be created in the home region."
        )
    return home


def get_availability_domains(identity_client):
    domains = identity_client.list_availability_domains(TENANCY).data
    names = [domain.name for domain in domains]
    if not names:
        raise RuntimeError("No availability domains were returned for the home region.")

    if AD_OVERRIDE:
        if AD_OVERRIDE not in names:
            raise RuntimeError("Configured availability domain is not valid for the tenancy.")
        return [AD_OVERRIDE]

    return names


def accessible_compartments(identity_client):
    compartment_ids = [TENANCY]
    try:
        compartments = all_results(
            identity_client.list_compartments,
            TENANCY,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        )
        compartment_ids.extend(
            compartment.id
            for compartment in compartments
            if getattr(compartment, "lifecycle_state", None) == "ACTIVE"
        )
    except oci.exceptions.ServiceError as exc:
        # Free-tier safety depends on seeing the tenancy-wide resource usage.
        # If the principal cannot inventory compartments, stop instead of risking
        # a launch that could exceed the free allowance.
        raise RuntimeError(
            f"Cannot safely inventory tenancy compartments ({exc.code})."
        ) from exc

    return list(dict.fromkeys(compartment_ids))


def find_existing_target(compute_client, compartment_ids):
    for compartment_id in compartment_ids:
        try:
            instances = all_results(
                compute_client.list_instances,
                compartment_id=compartment_id,
                display_name=DISPLAY_NAME,
            )
        except oci.exceptions.ServiceError:
            continue

        for instance in instances:
            if getattr(instance, "lifecycle_state", None) != "TERMINATED":
                return instance
    return None


def get_instance_shape_config(compute_client, instance):
    shape_config = getattr(instance, "shape_config", None)
    if shape_config is not None:
        return shape_config
    return compute_client.get_instance(instance.id).data.shape_config


def inspect_free_tier_usage(
    compute_client,
    block_client,
    identity_client,
    availability_domains,
):
    compartment_ids = accessible_compartments(identity_client)

    existing_target = find_existing_target(compute_client, compartment_ids)
    if existing_target:
        return {
            "existing_target": existing_target,
            "a1_ocpus": None,
            "a1_memory": None,
            "block_gb": None,
        }

    a1_ocpus = 0.0
    a1_memory = 0.0
    block_gb = 0

    for compartment_id in compartment_ids:
        try:
            instances = all_results(
                compute_client.list_instances,
                compartment_id=compartment_id,
            )
        except oci.exceptions.ServiceError:
            continue

        for instance in instances:
            if getattr(instance, "lifecycle_state", None) == "TERMINATED":
                continue
            if getattr(instance, "shape", None) == SHAPE:
                shape_config = get_instance_shape_config(compute_client, instance)
                if shape_config is None:
                    raise RuntimeError(
                        "Could not determine resources for an existing A1 instance."
                    )
                a1_ocpus += float(shape_config.ocpus or 0)
                a1_memory += float(shape_config.memory_in_gbs or 0)

        try:
            volumes = all_results(
                block_client.list_volumes,
                compartment_id=compartment_id,
            )
            block_gb += sum(
                int(volume.size_in_gbs or 0)
                for volume in volumes
                if getattr(volume, "lifecycle_state", None) != "TERMINATED"
            )
        except oci.exceptions.ServiceError:
            pass

        for availability_domain in availability_domains:
            try:
                boot_volumes = all_results(
                    block_client.list_boot_volumes,
                    availability_domain=availability_domain,
                    compartment_id=compartment_id,
                )
                block_gb += sum(
                    int(volume.size_in_gbs or 0)
                    for volume in boot_volumes
                    if getattr(volume, "lifecycle_state", None) != "TERMINATED"
                )
            except oci.exceptions.ServiceError:
                pass

    return {
        "existing_target": None,
        "a1_ocpus": a1_ocpus,
        "a1_memory": a1_memory,
        "block_gb": block_gb,
    }


def assert_free_tier_headroom(usage):
    next_ocpus = usage["a1_ocpus"] + OCPUS
    next_memory = usage["a1_memory"] + MEMORY_GB
    next_block = usage["block_gb"] + BOOT_GB

    if next_ocpus > MAX_FREE_A1_OCPUS:
        raise RuntimeError(
            f"Launching would bring A1 usage to {next_ocpus} OCPUs, above "
            f"the Always Free cap of {MAX_FREE_A1_OCPUS}."
        )
    if next_memory > MAX_FREE_A1_MEMORY_GB:
        raise RuntimeError(
            f"Launching would bring A1 memory to {next_memory} GB, above "
            f"the Always Free cap of {MAX_FREE_A1_MEMORY_GB} GB."
        )
    if next_block > MAX_FREE_BLOCK_GB:
        raise RuntimeError(
            f"Launching a {BOOT_GB} GB boot volume would bring block storage "
            f"to {next_block} GB, above the Always Free cap of {MAX_FREE_BLOCK_GB} GB."
        )


def capacity_report(compute_client, availability_domain):
    details = oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=TENANCY,
        availability_domain=availability_domain,
        shape_availabilities=[
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE,
                instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                    ocpus=OCPUS,
                    memory_in_gbs=MEMORY_GB,
                ),
            )
        ],
    )

    response = compute_client.create_compute_capacity_report(
        details,
        opc_retry_token=str(uuid.uuid4()),
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )

    entries = response.data.shape_availabilities or []
    statuses = []
    for entry in entries:
        status = getattr(entry, "availability_status", "UNKNOWN")
        count = getattr(entry, "available_count", None)
        fault_domain = getattr(entry, "fault_domain", None) or "automatic"
        statuses.append((status, count, fault_domain))
        log(
            f"Capacity report: AD={availability_domain}, "
            f"fault-domain={fault_domain}, status={status}, available={count}"
        )

    available = any(
        status == "AVAILABLE" and (count is None or int(count) > 0)
        for status, count, _ in statuses
    )
    return available, statuses


def get_latest_ubuntu_image(compute_client):
    images = compute_client.list_images(
        TARGET_COMPARTMENT,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    ).data
    if not images:
        raise RuntimeError("No compatible Ubuntu 22.04 ARM image was found.")
    return images[0].id


def launch_instance(compute_client, image_id, availability_domain):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=availability_domain,
        compartment_id=TARGET_COMPARTMENT,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=BOOT_GB,
        ),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
        display_name=DISPLAY_NAME,
        freeform_tags={
            "managed-by": "oci-instance-retry",
            "tier": "always-free",
        },
    )

    # Exactly one real launch request per scheduled workflow run. We deliberately
    # disable automatic launch retries because OCI's generic 5xx retry handling
    # can otherwise retry an Out-of-Host-Capacity response in a tight burst.
    return compute_client.launch_instance(
        details,
        opc_retry_token=str(uuid.uuid4()),
        retry_strategy=oci.retry.NoneRetryStrategy(),
    ).data


def get_public_ip(compute_client, network_client, instance_id):
    for _ in range(24):
        attachments = compute_client.list_vnic_attachments(
            compartment_id=TARGET_COMPARTMENT,
            instance_id=instance_id,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        ).data
        if attachments:
            vnic = network_client.get_vnic(
                attachments[0].vnic_id,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
            if vnic.public_ip:
                return vnic.public_ip
        time.sleep(5)
    return None


def announce_success(compute_client, network_client, instance):
    public_ip = get_public_ip(
        compute_client,
        network_client,
        instance.id,
    )
    message = (
        "✅ Oracle Always Free A1 VM claimed!\n"
        f"Name: {DISPLAY_NAME}\n"
        f"Shape: {SHAPE} ({OCPUS} OCPU / {MEMORY_GB} GB RAM)\n"
    )
    if public_ip:
        message += f"IP: {public_ip}\nSSH: ubuntu@{public_ip}"
    else:
        message += "The public IP was not ready yet. Check the OCI Console."

    notify(message)

    # Do not print the OCID or public IP to a public GitHub Actions log.
    log("SUCCESS: Oracle VM was created. Sensitive instance details were kept out of logs.")
    set_output("claimed", "true")


def main():
    set_output("claimed", "false")
    set_output("already_exists", "false")

    if START_JITTER_SECONDS > 0:
        delay = random.randint(0, START_JITTER_SECONDS)
        log(f"Start jitter: {delay}s")
        time.sleep(delay)

    config = build_config()
    identity_client = oci.identity.IdentityClient(
        config,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )
    compute_client = oci.core.ComputeClient(config)
    block_client = oci.core.BlockstorageClient(
        config,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )
    network_client = oci.core.VirtualNetworkClient(
        config,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )

    try:
        home_region = get_home_region(identity_client)
        log(f"Home-region guard passed: {home_region}")

        availability_domains = get_availability_domains(identity_client)
        log(f"Checking {len(availability_domains)} availability domain(s).")

        usage = inspect_free_tier_usage(
            compute_client,
            block_client,
            identity_client,
            availability_domains,
        )

        if usage["existing_target"] is not None:
            lifecycle = getattr(
                usage["existing_target"],
                "lifecycle_state",
                "UNKNOWN",
            )
            log(
                f"{DISPLAY_NAME} already exists in state {lifecycle}; "
                "no additional instance will be launched."
            )
            set_output("already_exists", "true")
            notify(
                f"ℹ️ Oracle VM already exists: {DISPLAY_NAME}\nState: {lifecycle}\n"
                "The capacity claimer has stopped creating new instances."
            )
            return 0

        assert_free_tier_headroom(usage)
        log(
            "Always Free guard passed: "
            f"existing A1={usage['a1_ocpus']} OCPU/{usage['a1_memory']} GB, "
            f"block storage={usage['block_gb']} GB."
        )

        available_domains = []
        report_failed = False

        for availability_domain in availability_domains:
            try:
                is_available, _ = capacity_report(
                    compute_client,
                    availability_domain,
                )
                if is_available:
                    available_domains.append(availability_domain)
            except oci.exceptions.ServiceError as exc:
                if exc.status in (401, 403):
                    raise
                # Capacity reports are an optimization, not a prerequisite for
                # launching. If the report API itself is unavailable, perform
                # one guarded launch attempt rather than missing real capacity.
                report_failed = True
                log(
                    f"Capacity-report API unavailable ({exc.code}); "
                    "falling back to one guarded launch attempt."
                )
                break

        if not available_domains and not report_failed:
            log("NO_CAPACITY: A1 2 OCPU / 12 GB is currently out of host capacity.")
            set_output("capacity", "none")
            return 0

        # Re-check the target immediately before launch. This protects against a
        # manual creation or another actor succeeding between inventory and launch.
        compartment_ids = accessible_compartments(identity_client)
        existing_target = find_existing_target(
            compute_client,
            compartment_ids,
        )
        if existing_target is not None:
            log("Target appeared during this run; skipping duplicate launch.")
            set_output("already_exists", "true")
            return 0

        launch_ad = (
            available_domains[0]
            if available_domains
            else availability_domains[0]
        )

        image_id = get_latest_ubuntu_image(compute_client)
        log(
            f"Capacity candidate found in {launch_ad}; "
            "submitting one idempotent launch request without pinning a fault domain."
        )
        instance = launch_instance(
            compute_client,
            image_id,
            launch_ad,
        )
        announce_success(
            compute_client,
            network_client,
            instance,
        )
        return 0

    except oci.exceptions.ServiceError as exc:
        message = (exc.message or "").lower()
        code = str(exc.code or "")

        if "out of host capacity" in message or "capacity" in message:
            log("NO_CAPACITY: launch raced with capacity loss; next scheduled run will retry.")
            set_output("capacity", "none")
            return 0

        if exc.status == 429 or code.lower() in {"toomanyrequests", "throttled"}:
            log("THROTTLED: OCI asked us to slow down; no immediate retry will be made.")
            set_output("capacity", "throttled")
            return 0

        safe_message = sanitize(exc.message or "")
        log(f"OCI_ERROR: status={exc.status} code={code} message={safe_message}")
        notify(
            "⚠️ OCI capacity claimer needs attention.\n"
            f"Status: {exc.status}\nCode: {code}\nMessage: {safe_message}"
        )
        return 1

    except Exception as exc:
        safe_message = sanitize(exc)
        log(f"GUARD_OR_CONFIG_ERROR: {safe_message}")
        notify(f"⚠️ OCI capacity claimer stopped safely: {safe_message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
