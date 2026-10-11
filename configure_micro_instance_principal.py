#!/usr/bin/env python3
"""Idempotently grant one existing E2 Micro an OCI instance-principal identity.

Runs ONLY from a trusted GitHub Actions workflow with pre-existing OCI secrets.
Does not change any running instance or output resource IDs/secrets to public logs.
"""
import os
import sys
import oci

INSTANCE_NAME = "vennila-oracle-micro"
GROUP_NAME = "VennilaA1MicroHunter"
POLICY_NAME = "VennilaA1MicroHunterPolicy"
REGION = "ap-hyderabad-1"


def main():
    key_path = "/tmp/oci_key.pem"
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(os.environ["OCI_PRIVATE_KEY"])
    os.chmod(key_path, 0o600)
    config = {
        "user": os.environ["OCI_USER"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "tenancy": os.environ["OCI_TENANCY"],
        "region": REGION,
        "key_file": key_path,
    }
    oci.config.validate_config(config)
    tenancy = config["tenancy"]
    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)

    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment_id=tenancy
    ).data
    matches = [
        x for x in instances
        if x.display_name == INSTANCE_NAME
        and x.shape == "VM.Standard.E2.1.Micro"
        and x.lifecycle_state in ("RUNNING", "STARTING", "STOPPED")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one existing Always Free micro; refusing IAM changes."
        )

    micro = matches[0]
    rule = f"instance.id = '{micro.id}'"
    existing_groups = oci.pagination.list_call_get_all_results(
        identity.list_dynamic_groups, compartment_id=tenancy
    ).data
    matching_groups = [x for x in existing_groups if x.name == GROUP_NAME]

    if matching_groups:
        if matching_groups[0].matching_rule != rule:
            raise RuntimeError(
                "Existing dynamic group has a different instance matching rule; "
                "refusing to broaden access."
            )
        print("INSTANCE_PRINCIPAL_GROUP_PRESENT", flush=True)
    else:
        identity.create_dynamic_group(
            oci.identity.models.CreateDynamicGroupDetails(
                compartment_id=tenancy,
                name=GROUP_NAME,
                description="Exact-instance principal for free A1 Micro hunter",
                matching_rule=rule,
            )
        )
        print("INSTANCE_PRINCIPAL_GROUP_CREATED", flush=True)

    dg = GROUP_NAME
    policy_statements = [
        f"Allow dynamic-group {dg} to inspect tenancies in tenancy",
        f"Allow dynamic-group {dg} to inspect compartments in tenancy",
        f"Allow dynamic-group {dg} to read instances in tenancy",
        f"Allow dynamic-group {dg} to read instance-images in tenancy",
        f"Allow dynamic-group {dg} to read app-catalog-listing in tenancy",
        f"Allow dynamic-group {dg} to read volume-family in tenancy",
        f"Allow dynamic-group {dg} to use volume-family in tenancy",
        f"Allow dynamic-group {dg} to read virtual-network-family in tenancy",
        f"Allow dynamic-group {dg} to use subnets in tenancy",
        f"Allow dynamic-group {dg} to use vnics in tenancy",
        f"Allow dynamic-group {dg} to use network-security-groups in tenancy",
        f"Allow dynamic-group {dg} to manage compute-capacity-reports in tenancy",
        f"Allow dynamic-group {dg} to manage instances in tenancy "
        "where request.permission = 'INSTANCE_CREATE'",
    ]
    existing_policies = oci.pagination.list_call_get_all_results(
        identity.list_policies, compartment_id=tenancy
    ).data
    matching_policies = [x for x in existing_policies if x.name == POLICY_NAME]

    if matching_policies:
        if set(matching_policies[0].statements) != set(policy_statements):
            raise RuntimeError(
                "Existing IAM policy differs; refusing to overwrite it."
            )
        print("INSTANCE_PRINCIPAL_POLICY_PRESENT", flush=True)
    else:
        identity.create_policy(
            oci.identity.models.CreatePolicyDetails(
                compartment_id=tenancy,
                name=POLICY_NAME,
                description="Only the existing Oracle Micro may launch A1; "
                            "all read access is for free-tier verification",
                statements=policy_statements,
            )
        )
        print("INSTANCE_PRINCIPAL_POLICY_CREATED", flush=True)

    print(
        "IAM_READY: no changes to the Micro's runtime, boot volume, "
        "IP address, SSH authorization, or shape were made.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # OCIDs, user identifiers and service messages can be sensitive in
        # public GitHub logs. Print type only; inspect privately if needed.
        print(f"IAM_BOOTSTRAP_ERROR: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
