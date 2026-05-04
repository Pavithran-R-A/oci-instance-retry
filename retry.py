import oci, time, datetime, sys, requests, os

USER        = os.environ["OCI_USER"]
FINGERPRINT = os.environ["OCI_FINGERPRINT"]
TENANCY     = os.environ["OCI_TENANCY"]
REGION      = "ap-hyderabad-1"
SUBNET_ID   = "ocid1.subnet.oc1.ap-hyderabad-1.aaaaaaaa2v6sjtwjg7ok4sgzkuk44kqyougskm72eysfeftuoyezowkdrrba"
AD          = "Oqdb:AP-HYDERABAD-1-AD-1"
SHAPE       = "VM.Standard.A1.Flex"
OCPUS       = 4
MEMORY_GB   = 24
BOOT_GB     = 200
SSH_PUB_KEY = os.environ["OCI_SSH_PUB_KEY"]
BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
CHAT_ID     = os.environ["TG_CHAT_ID"]
RETRY_INTERVAL = 20
MAX_ATTEMPTS   = 70  # GitHub Actions max ~25 min per run

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def notify(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

def write_key():
    key_content = os.environ["OCI_PRIVATE_KEY"]
    with open("/tmp/oci_key.pem", "w") as f:
        f.write(key_content)
    os.chmod("/tmp/oci_key.pem", 0o600)
    return "/tmp/oci_key.pem"

def get_image(compute_client, compartment_id):
    images = compute_client.list_images(
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC"
    ).data
    if images:
        log(f"Image: {images[0].display_name}")
        return images[0].id
    return None

def create_instance(compute_client, compartment_id, image_id):
    return compute_client.launch_instance(oci.core.models.LaunchInstanceDetails(
        availability_domain=AD,
        compartment_id=compartment_id,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=SUBNET_ID, assign_public_ip=True),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=BOOT_GB),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
        display_name="vennila-oracle-vm"
    )).data

def main():
    key_file = write_key()
    config = {"user": USER, "fingerprint": FINGERPRINT, "tenancy": TENANCY,
              "region": REGION, "key_file": key_file}
    oci.config.validate_config(config)
    compute_client = oci.core.ComputeClient(config)
    log("Finding Ubuntu 22.04 ARM image...")
    image_id = get_image(compute_client, TENANCY)
    if not image_id:
        log("ERROR: No image found"); sys.exit(1)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"Attempt #{attempt}...")
        try:
            instance = create_instance(compute_client, TENANCY, image_id)
            log(f"SUCCESS! ID: {instance.id}")
            time.sleep(60)
            vnic_attachments = compute_client.list_vnic_attachments(
                compartment_id=TENANCY, instance_id=instance.id).data
            if vnic_attachments:
                vnic = oci.core.VirtualNetworkClient(config).get_vnic(vnic_attachments[0].vnic_id).data
                log(f"PUBLIC IP: {vnic.public_ip}")
                notify(f"✅ Oracle VM Created!\nIP: {vnic.public_ip}\nSSH: ubuntu@{vnic.public_ip}")
            sys.exit(0)
        except oci.exceptions.ServiceError as e:
            if "capacity" in str(e.message).lower():
                log(f"No capacity. Retry in {RETRY_INTERVAL}s...")
            elif "TooManyRequests" in str(e.code):
                log("Rate limited. Waiting 90s..."); time.sleep(90)
            else:
                log(f"Error: {e.code} - {e.message}")
        except Exception as e:
            log(f"Error: {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_INTERVAL)

    log("Max attempts reached. Will retry next scheduled run.")
    sys.exit(0)

if __name__ == "__main__":
    main()
