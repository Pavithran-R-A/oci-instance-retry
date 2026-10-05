import datetime
import os
import sys
import time

import oci
import requests

USER = os.environ["OCI_USER"]
FINGERPRINT = os.environ["OCI_FINGERPRINT"]
TENANCY = os.environ["OCI_TENANCY"]
REGION = "ap-hyderabad-1"
SUBNET_ID = "ocid1.subnet.oc1.ap-hyderabad-1.aaaaaaaa2v6sjtwjg7ok4sgzkuk44kqyougskm72eysfeftuoyezowkdrrba"
AD = "Oqdb:AP-HYDERABAD-1-AD-1"
SHAPE = "VM.Standard.A1.Flex"
DISPLAY_NAME = "vennila-oracle-vm"

# Current OCI Always Free envelope (2026): 2 A1 OCPUs + 12 GB RAM.
# Keep the boot volume small so the launch stays within the 200 GB free
# block-volume allowance even if other small volumes already exist.
OCPUS = 2
MEMORY_GB = 12
BOOT_GB = 50
MAX_FREE_A1_OCPUS = 2
MAX_FREE_A1_MEMORY_GB = 12
MAX_FREE_BLOCK_GB = 200

SSH_PUB_KEY = os.environ["OCI_SSH_PUB_KEY"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def notify(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        ).raise_for_status()
    except Exception as exc:
        log(f"Telegram notification failed: {type(exc).__name__}")


def write_key():
    path = "/tmp/oci_key.pem"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(os.environ["OCI_PRIVATE_KEY"])
    os.chmod(path, 0o600)
    return path


def all_results(fn, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data


def get_compartment_ids(identity_client):
    ids = [TENANCY]
    compartments = all_results(
        identity_client.list_compartments,
        TENANCY,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    ids.extend(
        c.id
        for c in compartments
        if getattr(c, "lifecycle_state", None) == "ACTIVE"
    )
    return list(dict.fromkeys(ids))


def assert_home_region(identity_client):
    subscriptions = identity_client.list_region_subscriptions(TENANCY).data
    home = next(
        (s.region_name for s in subscriptions if getattr(s, "is_home_region", False)),
        None,
    )
    if home != REGION:
        raise RuntimeError(
            f"Fail-closed: configured region {REGION} is not the tenancy home region ({home})."
        )


def inspect_free_tier_usage(compute_client, block_client, identity_client):
    compartment_ids = get_compartment_ids(identity_client)
    a1_ocpus = 0.0
    a1_memory = 0.0
    block_gb = 0

    for compartment_id in compartment_ids:
        for instance in all_results(
            compute_client.list_instances,
            compartment_id=compartment_id,
        ):
            if getattr(instance, "lifecycle_state", None) == "TERMINATED":
                continue

            if getattr(instance, "display_name", None) == DISPLAY_NAME:
                return {
                    "already_exists": True,
                    "instance_id": instance.id,
                    "lifecycle_state": instance.lifecycle_state,
                    "a1_ocpus": a1_ocpus,
                    "a1_memory": a1_memory,
                    "block_gb": block_gb,
                }

            if getattr(instance, "shape", None) == SHAPE:
                shape_config = getattr(instance, "shape_config", None)
                if not shape_config:
                    raise RuntimeError(
                        "Fail-closed: could not determine resources for an existing A1 instance."
                    )
                a1_ocpus += float(shape_config.ocpus or 0)
                a1_memory += float(shape_config.memory_in_gbs or 0)

        for volume in all_results(
            block_client.list_boot_volumes,
            availability_domain=AD,
            compartment_id=compartment_id,
        ):
            if getattr(volume, "lifecycle_state", None) != "TERMINATED":
                block_gb += int(volume.size_in_gbs or 0)

        for volume in all_results(
            block_client.list_volumes,
            compartment_id=compartment_id,
        ):
            if getattr(volume, "lifecycle_state", None) != "TERMINATED":
                block_gb += int(volume.size_in_gbs or 0)

    return {
        "already_exists": False,
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
            f"Fail-closed: A1 OCPU total would be {next_ocpus}, above free limit {MAX_FREE_A1_OCPUS}."
        )
    if next_memory > MAX_FREE_A1_MEMORY_GB:
        raise RuntimeError(
            f"Fail-closed: A1 memory total would be {next_memory} GB, above free limit {MAX_FREE_A1_MEMORY_GB} GB."
        )
    if next_block > MAX_FREE_BLOCK_GB:
        raise RuntimeError(
            f"Fail-closed: block-volume total would be {next_block} GB, above free limit {MAX_FREE_BLOCK_GB} GB."
        )


def get_image(compute_client, compartment_id):
    images = compute_client.list_images(
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if images:
        log(f"Using image: {images[0].display_name}")
        return images[0].id
    return None


def create_instance(compute_client, compartment_id, image_id):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=AD,
        compartment_id=compartment_id,
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
    )
    return compute_client.launch_instance(details).data


def get_public_ip(compute_client, network_client, instance_id):
    for _ in range(24):
        attachments = compute_client.list_vnic_attachments(
            compartment_id=TENANCY,
            instance_id=instance_id,
        ).data
        if attachments:
            vnic = network_client.get_vnic(attachments[0].vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        time.sleep(5)
    return None


def main():
    config = {
        "user": USER,
        "fingerprint": FINGERPRINT,
        "tenancy": TENANCY,
        "region": REGION,
        "key_file": write_key(),
    }
    oci.config.validate_config(config)

    identity_client = oci.identity.IdentityClient(config)
    compute_client = oci.core.ComputeClient(config)
    block_client = oci.core.BlockstorageClient(config)
    network_client = oci.core.VirtualNetworkClient(config)

    try:
        assert_home_region(identity_client)
        usage = inspect_free_tier_usage(
            compute_client,
            block_client,
            identity_client,
        )

        if usage["already_exists"]:
            log(
                f"{DISPLAY_NAME} already exists in state {usage['lifecycle_state']}; "
                "no new instance will be launched."
            )
            notify(
                f"ℹ️ Oracle VM already exists: {DISPLAY_NAME}\n"
                f"State: {usage['lifecycle_state']}\n"
                f"ID: {usage['instance_id']}"
            )
            return 0

        assert_free_tier_headroom(usage)
        log(
            "Free-tier guard passed: "
            f"A1={usage['a1_ocpus']} OCPU/{usage['a1_memory']} GB RAM, "
            f"block storage={usage['block_gb']} GB before launch."
        )

        image_id = get_image(compute_client, TENANCY)
        if not image_id:
            raise RuntimeError("No compatible Ubuntu 22.04 ARM image found.")

        log("Checking A1 capacity with one guarded launch attempt...")
        instance = create_instance(compute_client, TENANCY, image_id)
        log(f"SUCCESS! Oracle VM created: {instance.id}")

        try:
            oci.wait_until(
                compute_client,
                compute_client.get_instance(instance.id),
                "lifecycle_state",
                "RUNNING",
                max_wait_seconds=300,
            )
        except Exception:
            log("Instance was created but did not reach RUNNING within 5 minutes.")

        public_ip = get_public_ip(compute_client, network_client, instance.id)
        if public_ip:
            log(f"PUBLIC IP: {public_ip}")
            notify(
                f"✅ Oracle VM Created!\n"
                f"Name: {DISPLAY_NAME}\n"
                f"Shape: {SHAPE} ({OCPUS} OCPU / {MEMORY_GB} GB)\n"
                f"IP: {public_ip}\n"
                f"SSH: ubuntu@{public_ip}"
            )
        else:
            notify(
                f"✅ Oracle VM Created!\n"
                f"Name: {DISPLAY_NAME}\n"
                f"ID: {instance.id}\n"
                "Public IP was not available yet; check OCI Console."
            )
        return 0

    except oci.exceptions.ServiceError as exc:
        message = (exc.message or "").lower()
        if "capacity" in message:
            log("NO_CAPACITY: Oracle A1 capacity is not currently available.")
            return 0

        log(f"OCI_ERROR: status={exc.status} code={exc.code} message={exc.message}")
        notify(
            f"⚠️ OCI retry needs attention.\n"
            f"Status: {exc.status}\nCode: {exc.code}\nMessage: {exc.message}"
        )
        return 1

    except Exception as exc:
        log(f"GUARD_OR_CONFIG_ERROR: {exc}")
        notify(f"⚠️ OCI retry stopped safely: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
