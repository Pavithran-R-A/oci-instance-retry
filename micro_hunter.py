#!/usr/bin/env python3
"""Low-resource OCI A1 claimer, running *on* an existing Always Free micro.

Authentication: OCI instance principal, not an API signing key on disk.
One capacity report and at most one launch per execution; systemd controls cadence.
"""
import datetime as dt
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.request
import uuid

import oci

SHAPE = "VM.Standard.A1.Flex"
OCPUS, RAM_GB, BOOT_GB = 2, 12, 50
MAX_A1_OCPUS, MAX_A1_RAM_GB, MAX_BLOCK_GB = 2, 12, 200
TARGET_NAME = "vennila-oracle-vm"
CLAIMED = Path("/var/lib/oci-hunter/claimed")
IMDS_URL = "http://169.254.169.254/opc/v2/instance/"
REDACT = re.compile(r"ocid1\.[A-Za-z0-9._:-]+|\b(?:\d{1,3}\.){3}\d{1,3}\b")


def log(message):
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {REDACT.sub('[redacted]', str(message))}", flush=True)


def pages(operation, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(operation, *args, **kwargs).data


def instance_metadata():
    request = urllib.request.Request(
        IMDS_URL, headers={"Authorization": "Bearer Oracle"}
    )
    with urllib.request.urlopen(request, timeout=6) as response:
        data = json.load(response)
    if not all(data.get(field) for field in ("id", "tenantId", "compartmentId")):
        raise RuntimeError("Required instance identity metadata is missing.")
    return data


def clients():
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    config = {"region": signer.region}
    def client(cls):
        return cls(
            config=config,
            signer=signer,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        )
    return (
        client(oci.identity.IdentityClient),
        client(oci.core.ComputeClient),
        client(oci.core.BlockstorageClient),
        client(oci.core.VirtualNetworkClient),
    )


def home_region(identity, tenancy, current_region):
    regions = identity.list_region_subscriptions(tenancy).data
    home = next(
        (region.region_name for region in regions if region.is_home_region),
        None,
    )
    if not home or home != current_region:
        raise RuntimeError(
            "Current Micro is not in the home region; refusing chargeable deployment."
        )


def compartment_ids(identity, tenancy):
    # Inventory every active compartment. A permission failure must stop launch.
    ids = [tenancy]
    children = pages(
        identity.list_compartments,
        tenancy,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    ids += [
        child.id
        for child in children
        if getattr(child, "lifecycle_state", None) == "ACTIVE"
    ]
    return list(dict.fromkeys(ids))


def current_usage(identity, compute, block, tenancy, ads):
    ocpus = memory = 0.0
    block_gb = 0
    target = None

    for compartment in compartment_ids(identity, tenancy):
        instances = pages(compute.list_instances, compartment_id=compartment)
        for instance in instances:
            state = getattr(instance, "lifecycle_state", None)
            if state == "TERMINATED":
                continue
            if instance.display_name == TARGET_NAME:
                target = instance
            if instance.shape == SHAPE:
                cfg = getattr(instance, "shape_config", None)
                if cfg is None:
                    cfg = compute.get_instance(instance.id).data.shape_config
                if cfg is None:
                    raise RuntimeError("A1 shape usage cannot be determined.")
                ocpus += float(cfg.ocpus or 0)
                memory += float(cfg.memory_in_gbs or 0)

        volumes = pages(block.list_volumes, compartment_id=compartment)
        block_gb += sum(
            int(v.size_in_gbs or 0)
            for v in volumes
            if v.lifecycle_state != "TERMINATED"
        )

        for ad in ads:
            boot_volumes = pages(
                block.list_boot_volumes,
                compartment_id=compartment,
                availability_domain=ad,
            )
            block_gb += sum(
                int(v.size_in_gbs or 0)
                for v in boot_volumes
                if v.lifecycle_state != "TERMINATED"
            )

    return target, ocpus, memory, block_gb


def assert_free_headroom(ocpus, memory, block_gb):
    if ocpus + OCPUS > MAX_A1_OCPUS:
        raise RuntimeError("A1 CPU allowance insufficient. No VM launched.")
    if memory + RAM_GB > MAX_A1_RAM_GB:
        raise RuntimeError("A1 RAM allowance insufficient. No VM launched.")
    if block_gb + BOOT_GB > MAX_BLOCK_GB:
        raise RuntimeError("Always Free block storage allowance insufficient.")


def own_subnet(compute, network, own_id, own_compartment):
    attached = pages(
        compute.list_vnic_attachments,
        compartment_id=own_compartment,
        instance_id=own_id,
    )
    if not attached:
        raise RuntimeError("Micro instance VNIC unavailable; cannot select subnet.")
    vnic = network.get_vnic(attached[0].vnic_id).data
    subnet = network.get_subnet(vnic.subnet_id).data
    if getattr(subnet, "prohibit_public_ip_on_vnic", False):
        raise RuntimeError("Current Micro subnet forbids public IP assignment.")
    if subnet.lifecycle_state != "AVAILABLE":
        raise RuntimeError("Current Micro subnet is not AVAILABLE.")
    return subnet


def report(compute, tenancy, ad):
    details = oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=tenancy,
        availability_domain=ad,
        shape_availabilities=[
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE,
                instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                    ocpus=OCPUS, memory_in_gbs=RAM_GB
                ),
            ),
        ],
    )
    response = compute.create_compute_capacity_report(
        details,
        opc_retry_token=str(uuid.uuid4()),
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )
    for item in response.data.shape_availabilities or []:
        if (
            item.availability_status == "AVAILABLE"
            and (item.available_count is None or item.available_count > 0)
        ):
            return True
    return False


def ubuntu_arm_image(compute, tenancy):
    images = compute.list_images(
        tenancy,
        shape=SHAPE,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise RuntimeError("No supported Ubuntu ARM image available.")
    return images[0].id


def ssh_pub_key(compute, own_id):
    current = compute.get_instance(own_id).data
    key = (getattr(current, "metadata", None) or {}).get("ssh_authorized_keys")
    if not key or "ssh-" not in key:
        raise RuntimeError("Micro has no discoverable SSH public key; refusing launch.")
    return key


def claim(compute, tenancy, ad, subnet_id, image_id, pub_key):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=tenancy,
        display_name=TARGET_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=RAM_GB
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=BOOT_GB
        ),
        metadata={"ssh_authorized_keys": pub_key},
        freeform_tags={"managed-by": "oci-micro-hunter", "tier": "always-free"},
    )
    # One attempt, with an idempotency token. Never loop on launch errors.
    return compute.launch_instance(
        details,
        opc_retry_token=str(uuid.uuid4()),
        retry_strategy=oci.retry.NoneRetryStrategy(),
    ).data


def mark_claimed():
    CLAIMED.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    CLAIMED.write_text(
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + "\n",
        encoding="utf-8",
    )


def main():
    if CLAIMED.exists():
        log("ALREADY_CLAIMED: service will not submit more requests.")
        return 0
    time.sleep(random.randint(0, 12))

    meta = instance_metadata()
    tenancy, own_id = meta["tenantId"], meta["id"]
    target_compartment = os.getenv("OCI_TARGET_COMPARTMENT_ID", tenancy)
    identity, compute, block, network = clients()
    current_region = meta.get("region")
    home_region(identity, tenancy, current_region)
    ads = [
        ad.name
        for ad in identity.list_availability_domains(tenancy).data
    ]
    if not ads:
        raise RuntimeError("No availability domains available.")

    target, ocpus, memory, storage = current_usage(
        identity, compute, block, tenancy, ads
    )
    if target:
        mark_claimed()
        log(f"ALREADY_CLAIMED: A1 target exists; state={target.lifecycle_state}")
        return 0

    assert_free_headroom(ocpus, memory, storage)
    subnet = own_subnet(compute, network, own_id, meta["compartmentId"])

    available_ads = [
        ad for ad in ads if report(compute, tenancy, ad)
        and (
            not getattr(subnet, "availability_domain", None)
            or subnet.availability_domain == ad
        )
    ]
    if not available_ads:
        log("NO_CAPACITY: A1 Flex 2 OCPU / 12 GB; check again next timer.")
        return 0

    log("CAPACITY_AVAILABLE: attempting one guarded A1 launch.")
    try:
        new = claim(
            compute,
            target_compartment,
            available_ads[0],
            subnet.id,
            ubuntu_arm_image(compute, target_compartment),
            ssh_pub_key(compute, own_id),
        )
    except oci.exceptions.ServiceError as exc:
        if "capacity" in (exc.message or "").lower():
            log("NO_CAPACITY: A1 slot disappeared during launch.")
            return 0
        if exc.status == 429:
            log("THROTTLED: wait for next scheduled timer.")
            return 0
        raise
    mark_claimed()
    log(f"CLAIMED: A1 instance created, lifecycle={new.lifecycle_state}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        log(f"ERROR: {type(err).__name__}: {err}")
        sys.exit(1)
