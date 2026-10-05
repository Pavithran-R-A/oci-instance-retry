import datetime as dt
import os
import random
import re
import sys
import time
import uuid

import oci
import requests

USER = os.environ["OCI_USER"]
FINGERPRINT = os.environ["OCI_FINGERPRINT"]
TENANCY = os.environ["OCI_TENANCY"]
PRIVATE_KEY = os.environ["OCI_PRIVATE_KEY"]
SSH_PUB_KEY = os.environ["OCI_SSH_PUB_KEY"]
SUBNET_ID = os.getenv("OCI_SUBNET_ID", "").strip()
REGION = os.getenv("OCI_REGION", "ap-hyderabad-1").strip()
COMPARTMENT_ID = os.getenv("OCI_COMPARTMENT_ID", "").strip() or TENANCY
PRIMARY_NAME = os.getenv("OCI_DISPLAY_NAME", "vennila-oracle-vm").strip()
FALLBACK_NAME = os.getenv("OCI_FALLBACK_DISPLAY_NAME", "vennila-oracle-micro").strip()

SHAPE = "VM.Standard.E2.1.Micro"
BOOT_GB = 50
MAX_FREE_MICROS = 2
MAX_FREE_BLOCK_GB = 200
START_JITTER_SECONDS = int(os.getenv("START_JITTER_SECONDS", "15"))

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

OCID_RE = re.compile(r"ocid1\.[A-Za-z0-9._:-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def sanitize(value):
    text = str(value)
    text = OCID_RE.sub("<ocid-redacted>", text)
    return IPV4_RE.sub("<ip-redacted>", text)


def log(message):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {sanitize(message)}", flush=True)


def output(name, value):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
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


def all_results(callable_, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(callable_, *args, **kwargs).data


def build_config():
    key_path = "/tmp/oci_key.pem"
    with open(key_path, "w", encoding="utf-8") as handle:
        handle.write(PRIVATE_KEY)
    os.chmod(key_path, 0o600)
    config = {
        "user": USER,
        "fingerprint": FINGERPRINT,
        "tenancy": TENANCY,
        "region": REGION,
        "key_file": key_path,
    }
    oci.config.validate_config(config)
    return config


def get_home_region(identity):
    subscriptions = identity.list_region_subscriptions(TENANCY).data
    home = next(
        (x.region_name for x in subscriptions if getattr(x, "is_home_region", False)),
        None,
    )
    if not home:
        raise RuntimeError("Could not determine tenancy home region.")
    if REGION != home:
        raise RuntimeError(
            f"{REGION} is not home region {home}; Always Free Compute must stay in the home region."
        )
    return home


def get_ads(identity):
    ads = [x.name for x in identity.list_availability_domains(TENANCY).data]
    if not ads:
        raise RuntimeError("No availability domains found.")
    return ads


def get_compartments(identity):
    ids = [TENANCY]
    children = all_results(
        identity.list_compartments,
        TENANCY,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    ids.extend(
        x.id for x in children if getattr(x, "lifecycle_state", None) == "ACTIVE"
    )
    return list(dict.fromkeys(ids))


def inventory(compute, block, identity, ads):
    instances = []
    block_gb = 0
    for cid in get_compartments(identity):
        instances.extend(
            x
            for x in all_results(compute.list_instances, compartment_id=cid)
            if getattr(x, "lifecycle_state", None) != "TERMINATED"
        )
        block_gb += sum(
            int(x.size_in_gbs or 0)
            for x in all_results(block.list_volumes, compartment_id=cid)
            if getattr(x, "lifecycle_state", None) != "TERMINATED"
        )
        for ad in ads:
            block_gb += sum(
                int(x.size_in_gbs or 0)
                for x in all_results(
                    block.list_boot_volumes,
                    availability_domain=ad,
                    compartment_id=cid,
                )
                if getattr(x, "lifecycle_state", None) != "TERMINATED"
            )

    primary = next(
        (x for x in instances if getattr(x, "display_name", None) == PRIMARY_NAME),
        None,
    )
    fallback = next(
        (x for x in instances if getattr(x, "display_name", None) == FALLBACK_NAME),
        None,
    )
    micro_count = sum(1 for x in instances if getattr(x, "shape", None) == SHAPE)
    return primary, fallback, micro_count, block_gb


def capacity_available(compute, ad):
    details = oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=TENANCY,
        availability_domain=ad,
        shape_availabilities=[
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE
            )
        ],
    )
    response = compute.create_compute_capacity_report(
        details,
        opc_retry_token=str(uuid.uuid4()),
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )
    available = False
    for entry in response.data.shape_availabilities or []:
        status = getattr(entry, "availability_status", "UNKNOWN")
        count = getattr(entry, "available_count", None)
        log(f"Micro capacity: AD={ad}, status={status}, available={count}")
        if status == "AVAILABLE" and (count is None or int(count) > 0):
            available = True
    return available


def resolve_subnet(network):
    if SUBNET_ID:
        subnet = network.get_subnet(
            SUBNET_ID, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
        ).data
        if getattr(subnet, "lifecycle_state", None) != "AVAILABLE":
            raise RuntimeError("Configured subnet is not AVAILABLE.")
        if getattr(subnet, "prohibit_public_ip_on_vnic", False):
            raise RuntimeError("Configured subnet prohibits public IP assignment.")
        return subnet.id

    subnets = all_results(
        network.list_subnets,
        compartment_id=COMPARTMENT_ID,
        lifecycle_state="AVAILABLE",
    )
    public = [
        x for x in subnets if not getattr(x, "prohibit_public_ip_on_vnic", False)
    ]
    if len(public) == 1:
        return public[0].id
    defaults = [
        x for x in public
        if "default" in (getattr(x, "display_name", "") or "").lower()
    ]
    if len(defaults) == 1:
        return defaults[0].id
    if not public:
        raise RuntimeError("No public-IP-capable subnet found.")
    raise RuntimeError("Multiple eligible subnets exist; set OCI_SUBNET_ID explicitly.")


def latest_image(compute):
    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    ).data
    if not images:
        raise RuntimeError("No compatible Ubuntu 22.04 image found for E2.1.Micro.")
    return images[0].id


def launch(compute, ad, subnet_id, image_id):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=COMPARTMENT_ID,
        shape=SHAPE,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=BOOT_GB,
        ),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
        display_name=FALLBACK_NAME,
        freeform_tags={
            "managed-by": "oci-instance-retry",
            "tier": "always-free-fallback",
        },
    )
    return compute.launch_instance(
        details,
        opc_retry_token=str(uuid.uuid4()),
        retry_strategy=oci.retry.NoneRetryStrategy(),
    ).data


def get_public_ip(compute, network, instance_id):
    for _ in range(24):
        attachments = compute.list_vnic_attachments(
            compartment_id=COMPARTMENT_ID,
            instance_id=instance_id,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        ).data
        if attachments:
            vnic = network.get_vnic(
                attachments[0].vnic_id,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
            if vnic.public_ip:
                return vnic.public_ip
        time.sleep(5)
    return None


def is_capacity_error(exc):
    msg = (exc.message or "").lower()
    return "out of host capacity" in msg or "capacity" in msg


def main():
    output("fallback_created", "false")
    output("already_have_primary", "false")
    output("already_have_fallback", "false")

    if START_JITTER_SECONDS > 0:
        time.sleep(random.randint(0, START_JITTER_SECONDS))

    config = build_config()
    identity = oci.identity.IdentityClient(
        config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
    )
    compute = oci.core.ComputeClient(
        config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
    )
    block = oci.core.BlockstorageClient(
        config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
    )
    network = oci.core.VirtualNetworkClient(
        config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
    )

    try:
        home = get_home_region(identity)
        ads = get_ads(identity)
        log(f"Free-tier home region: {home}; ADs={len(ads)}")

        primary, fallback, micro_count, block_gb = inventory(
            compute, block, identity, ads
        )
        if primary is not None:
            log("A1 primary already exists; fallback is unnecessary.")
            output("already_have_primary", "true")
            return 0
        if fallback is not None:
            log("Fallback E2.1.Micro already exists; no duplicate will be created.")
            output("already_have_fallback", "true")
            return 0
        if micro_count >= MAX_FREE_MICROS:
            raise RuntimeError("E2.1.Micro Always Free instance limit is already in use.")
        if block_gb + BOOT_GB > MAX_FREE_BLOCK_GB:
            raise RuntimeError(
                "Fallback boot disk would exceed the 200 GB Always Free block-storage pool."
            )

        for ad in ads:
            try:
                available = capacity_available(compute, ad)
            except oci.exceptions.ServiceError as exc:
                if exc.status in (401, 403):
                    raise
                log(
                    f"Micro capacity report unavailable ({exc.code}); using one guarded direct probe."
                )
                available = True

            if not available:
                continue

            try:
                instance = launch(
                    compute,
                    ad,
                    resolve_subnet(network),
                    latest_image(compute),
                )
            except oci.exceptions.ServiceError as exc:
                if is_capacity_error(exc):
                    log("NO_CAPACITY: E2.1.Micro capacity disappeared before launch.")
                    continue
                if exc.status == 429:
                    log("THROTTLED: waiting for next five-minute schedule.")
                    return 0
                raise

            ip = get_public_ip(compute, network, instance.id)
            msg = (
                "✅ Oracle Always Free fallback VPS claimed!\n"
                f"Name: {FALLBACK_NAME}\n"
                "Shape: VM.Standard.E2.1.Micro (1 GB RAM)\n"
                "The stronger A1 2 OCPU / 12 GB hunt will continue.\n"
            )
            if ip:
                msg += f"IP: {ip}\nSSH: ubuntu@{ip}"
            else:
                msg += "Public IP is not ready yet; check OCI Console."
            notify(msg)
            log(
                "FALLBACK_SUCCESS: Always Free E2.1.Micro VPS created; A1 hunt continues."
            )
            output("fallback_created", "true")
            return 0

        log("NO_CAPACITY: E2.1.Micro is unavailable right now.")
        return 0

    except oci.exceptions.ServiceError as exc:
        if is_capacity_error(exc):
            log("NO_CAPACITY: temporary host-capacity exhaustion.")
            return 0
        safe = sanitize(exc.message or "")
        log(f"OCI_ERROR: status={exc.status} code={exc.code} message={safe}")
        notify(
            "⚠️ OCI fallback claimer needs attention.\n"
            f"Status: {exc.status}\nCode: {exc.code}\nMessage: {safe}"
        )
        return 1
    except Exception as exc:
        safe = sanitize(exc)
        log(f"GUARD_OR_CONFIG_ERROR: {safe}")
        notify(f"⚠️ OCI fallback claimer stopped safely: {safe}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
