"""Turn a Terraform plan into `import` blocks for resources that already exist.

Reads `terraform show -json` output, finds everything the plan would CREATE, and writes
one `import` block per resource with the ID format for its type and an AWS CLI command
that lists candidates.

The plan is read from standard input when the path is `-`, which is how
plan-adoption.sh calls it. The JSON form of a plan holds sensitive values in plain text
(in public mode, the Cognito app client secret), so it is streamed here and never
written to disk. Only addresses, types and actions are read from it.

The plan knows the resource address. It cannot know the ID of something it believes does
not exist, so every block carries a TODO rather than a guess — an import block with a
wrong ID attaches the wrong live resource to a config address, which is worse than not
importing at all.

Read only. Writes one file, which holds addresses and no values, and touches nothing
else.
"""

from __future__ import annotations

import json
import pathlib
import sys

# How to identify each resource type on import, and how to find candidates.
#
# Import ID formats differ per resource and getting one wrong binds the wrong live
# resource to a config address. Types absent from this table still get a block, marked
# so the operator knows to look the format up rather than assume.
_SAME_BUCKET = "same name as the aws_s3_bucket it configures"

ID_FORMATS: dict[str, tuple[str, str]] = {
    "aws_s3_bucket": ("bucket name", "aws s3api list-buckets"),
    "aws_s3_bucket_versioning": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_server_side_encryption_configuration": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_public_access_block": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_policy": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_ownership_controls": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_lifecycle_configuration": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_logging": ("bucket name", _SAME_BUCKET),
    "aws_s3_bucket_cors_configuration": ("bucket name", _SAME_BUCKET),
    "aws_s3_account_public_access_block": (
        "AWS account ID",
        "aws sts get-caller-identity",
    ),
    "aws_sqs_queue": ("queue URL, not the name or ARN", "aws sqs list-queues"),
    "aws_sqs_queue_policy": ("queue URL", "aws sqs list-queues"),
    "aws_dynamodb_table": ("table name", "aws dynamodb list-tables"),
    "aws_iam_role": ("role name", "aws iam list-roles"),
    "aws_iam_role_policy": (
        "role-name:policy-name",
        "aws iam list-role-policies --role-name <role>",
    ),
    "aws_iam_role_policy_attachment": (
        "role-name/policy-arn",
        "aws iam list-attached-role-policies --role-name <role>",
    ),
    "aws_iam_policy": ("policy ARN", "aws iam list-policies --scope Local"),
    "aws_iam_instance_profile": ("profile name", "aws iam list-instance-profiles"),
    "aws_iam_openid_connect_provider": (
        "provider ARN",
        "aws iam list-open-id-connect-providers",
    ),
    "aws_kms_key": ("key ID, not the alias", "aws kms list-aliases"),
    "aws_kms_alias": ("alias name including the alias/ prefix", "aws kms list-aliases"),
    "aws_eks_cluster": ("cluster name", "aws eks list-clusters"),
    "aws_eks_node_group": (
        "cluster-name:nodegroup-name",
        "aws eks list-nodegroups --cluster-name <cluster>",
    ),
    "aws_eks_addon": (
        "cluster-name:addon-name",
        "aws eks list-addons --cluster-name <cluster>",
    ),
    "aws_eks_access_entry": (
        "cluster-name:principal-arn (the IAM role or user ARN)",
        "aws eks list-access-entries --cluster-name <cluster>",
    ),
    "aws_eks_access_policy_association": (
        "cluster-name#principal-arn#policy-arn",
        "aws eks list-associated-access-policies --cluster-name <cluster>"
        " --principal-arn <arn>",
    ),
    "aws_vpc": ("VPC ID", "aws ec2 describe-vpcs"),
    "aws_subnet": ("subnet ID", "aws ec2 describe-subnets"),
    "aws_route_table": ("route table ID", "aws ec2 describe-route-tables"),
    "aws_route_table_association": (
        "subnet-id/route-table-id",
        "aws ec2 describe-route-tables",
    ),
    "aws_internet_gateway": ("igw ID", "aws ec2 describe-internet-gateways"),
    "aws_nat_gateway": ("nat gateway ID", "aws ec2 describe-nat-gateways"),
    "aws_eip": ("allocation ID", "aws ec2 describe-addresses"),
    "aws_security_group": ("security group ID", "aws ec2 describe-security-groups"),
    "aws_vpc_security_group_ingress_rule": (
        "security group rule ID",
        "aws ec2 describe-security-group-rules",
    ),
    "aws_vpc_security_group_egress_rule": (
        "security group rule ID",
        "aws ec2 describe-security-group-rules",
    ),
    "aws_launch_template": ("launch template ID", "aws ec2 describe-launch-templates"),
    "aws_instance": ("instance ID", "aws ec2 describe-instances"),
    "aws_flow_log": ("flow log ID", "aws ec2 describe-flow-logs"),
    "aws_ecr_repository": ("repository name", "aws ecr describe-repositories"),
    "aws_ecr_lifecycle_policy": ("repository name", "aws ecr describe-repositories"),
    "aws_cloudwatch_log_group": ("log group name", "aws logs describe-log-groups"),
    "aws_cloudwatch_metric_alarm": ("alarm name", "aws cloudwatch describe-alarms"),
    "aws_cloudwatch_dashboard": ("dashboard name", "aws cloudwatch list-dashboards"),
    "aws_sns_topic": ("topic ARN", "aws sns list-topics"),
    "aws_sns_topic_subscription": ("subscription ARN", "aws sns list-subscriptions"),
}

# Extra comment lines for types whose import needs more than an ID.
#
# The account-wide S3 Block Public Access is declared only when its opt-in is on,
# as module.s3.aws_s3_account_public_access_block.account_opt_in[0]. It never fails
# on a name: creating it simply overwrites the account's current setting, which may
# predate this stack. Importing it instead records that Terraform now manages a
# setting that was already there.
NOTES: dict[str, tuple[str, ...]] = {
    "aws_s3_account_public_access_block": (
        "account-wide, one per account. Creating it overwrites the account's",
        "current setting; import it to take over the setting as it is.",
        "destroy.sh removes it from state and never deletes it.",
    ),
}

# Deleting this type switches Block Public Access off for the whole account
# (THREAT_MODEL.md T37).
ACCOUNT_BPA_TYPE = "aws_s3_account_public_access_block"

# Resources that genuinely should be created rather than adopted, because they hold no
# state of their own and are cheap to replace.
SKIP_TYPES: set[str] = set()


def load_plan(path: str) -> dict:
    """The `terraform show -json` document at `path`, or on stdin for `-`."""
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(pathlib.Path(path).read_text())


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: generate_imports.py <plan.json | -> <out.tf>", file=sys.stderr)
        return 2

    plan = load_plan(sys.argv[1])
    out_path = pathlib.Path(sys.argv[2])

    creates = []
    other = {"update": 0, "delete": 0, "replace": 0, "no-op": 0}
    bpa_deletes: list[str] = []

    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if rc.get("type") == ACCOUNT_BPA_TYPE and "delete" in actions:
            bpa_deletes.append(rc["address"])
        if actions == ["create"]:
            creates.append(rc)
        elif actions == ["update"]:
            other["update"] += 1
        elif actions == ["delete"]:
            other["delete"] += 1
        elif "create" in actions and "delete" in actions:
            other["replace"] += 1
        else:
            other["no-op"] += 1

    print(f"  planned creates : {len(creates)}")
    print(f"  in-place updates: {other['update']}")
    print(f"  replacements    : {other['replace']}")
    print(f"  deletions       : {other['delete']}")

    if other["delete"] or other["replace"]:
        print()
        print("  WARNING: the plan contains deletions or replacements. Adoption only")
        print("  addresses creates. Review those separately before applying — a")
        print("  replacement on a stateful resource destroys data.")

    for addr in bpa_deletes:
        print()
        print(f"  WARNING: the plan deletes {addr}, the account-wide S3 Block")
        print("  Public Access. Applying it switches that protection off for every")
        print("  bucket in the account. Do not apply it; take it out of state first:")
        print(f"    terraform state rm '{addr}'")

    if not creates:
        print()
        print("  Nothing to adopt: the plan creates no resources.")
        out_path.write_text(
            "# No planned creates — nothing to adopt.\n"
            "# Generated by scripts/plan-adoption.sh\n"
        )
        return 0

    by_type: dict[str, int] = {}
    for rc in creates:
        by_type[rc["type"]] = by_type.get(rc["type"], 0) + 1

    print()
    print("  by type:")
    for t, n in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"    {n:3}  {t}")

    lines = [
        "# Terraform import blocks — GENERATED, then edited by hand.",
        "#",
        "# One block per resource the plan would create and that likely already",
        "# exists in the account. Every `id` is a TODO: the plan knows the address,",
        "# not what exists in your account, and a wrong ID attaches the wrong live",
        "# resource to a config address. Fill them in from the lookup command on",
        "# each block.",
        "#",
        "# Delete the blocks for anything that genuinely does not exist yet — those",
        "# should be created, not imported.",
        "#",
        "# These are one-shot. Remove this file once the apply has succeeded, or every",
        "# future plan re-checks imports that already happened.",
        "#",
        f"# Generated from a plan with {len(creates)} planned creates.",
        "",
    ]

    unknown: list[str] = []
    for rc in sorted(creates, key=lambda r: r["address"]):
        addr = rc["address"]
        rtype = rc["type"]
        if rtype in SKIP_TYPES:
            continue
        fmt, how = ID_FORMATS.get(rtype, ("UNKNOWN — check the provider docs", ""))
        if rtype not in ID_FORMATS:
            unknown.append(rtype)

        lines.append(f"# {addr}")
        lines.append(f"#   id format: {fmt}")
        if how:
            lines.append(f"#   find it:   {how}")
        for note in NOTES.get(rtype, ()):
            lines.append(f"#   note:      {note}")
        lines.append("import {")
        lines.append(f"  to = {addr}")
        lines.append('  id = "" # TODO')
        lines.append("}")
        lines.append("")

    out_path.write_text("\n".join(lines))

    print()
    print(f"  wrote {out_path}")
    if unknown:
        print()
        print("  types with no known ID format (look these up):")
        for t in sorted(set(unknown)):
            print(f"    {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
