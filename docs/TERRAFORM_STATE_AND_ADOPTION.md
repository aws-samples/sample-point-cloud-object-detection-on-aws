# Terraform State and Adopting Existing Resources

Covers two linked things: moving state into S3, and adopting resources that already
exist in an account but are absent from state.

They are linked because the second is caused by the first. There was no backend
configuration, so state was a local `terraform.tfstate` — one machine, gitignored, no
lock. Anyone applying from a different machine started from an empty state and planned
to **create** resources that already existed. That fails on globally-unique names (S3
buckets, IAM roles) and half-creates the rest.

## One-time: create the state bucket

The bucket holding state cannot be managed by the state it holds, so it is created out
of band. This is the only resource deliberately outside Terraform.

```bash
cd infrastructure
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
./scripts/bootstrap-state.sh "pointcloud-detection-tfstate-${ACCOUNT}-${REGION}-an" "${REGION}"
```

A name ending in `-<account>-<region>-an` is created in the account's regional
namespace, which S3 lets no other account create, before or after the bucket is
deleted. The script creates any other name in the shared global namespace and says so.

Idempotent, so re-running is safe. It enables versioning (which is what makes a
corrupted state file recoverable), SSE-KMS default encryption, a full public-access
block, and a TLS-only bucket policy.

The default key is the AWS managed `aws/s3`, which needs no key policy: the state
bucket exists before any key the stack creates, and a key whose policy lives in the
state it protects is the same chicken-and-egg problem one level down. Pass a customer
managed key as the third argument to control who may decrypt.

Then:

```bash
export TF_STATE_BUCKET=<bucket-name>
export TF_STATE_REGION=<region>   # defaults to us-east-1
```

`deploy.sh` and `destroy.sh` both require `TF_STATE_BUCKET` and fail with an explanatory
message if it is unset. That check exists because a bare `terraform init` against a
partial backend config **prompts for the bucket on stdin** — in a script that hangs
rather than fails.

## Migrating an existing local state

If you have a working `terraform.tfstate` locally, move it rather than re-adopting
everything:

```bash
cd infrastructure
. scripts/lib/state-backend.sh   # the backend arguments deploy.sh uses, KMS key included
state_backend_args "${TF_STATE_BUCKET}" "pointcloud-detection/dev/terraform.tfstate" \
  "${TF_STATE_REGION}"
terraform init -migrate-state "${BACKEND_ARGS[@]}"
```

Terraform shows what it will copy and asks first. Afterwards, plan with the access mode
and hostname the stack is deployed with. These are the values `deploy.sh` passes from
`ACCESS_MODE` and `INGRESS_HOST`; public mode is the default and refuses to plan
without the hostname, so a plain `-var-file` plan fails validation.

```bash
terraform plan -var-file=environments/dev/terraform.tfvars \
  -var "access_mode=${ACCESS_MODE:-public}" -var "app_host=${INGRESS_HOST:-}"
```

A clean plan means the migration worked. **Keep the local `terraform.tfstate` until you
have seen that clean plan** — it is the only copy if the migration goes wrong.

## Adopting resources that exist but are not in state

This is the case where resources were applied from a machine whose state you do not
have, or were created by hand.

### Step 1 — see what Terraform thinks is missing

```bash
cd infrastructure
./scripts/plan-adoption.sh dev
```

Read only. It runs `terraform plan` and nothing else: no apply, no import, no state
write. It takes no state lock, since a read has no business blocking a concurrent
operator.

It plans with the access mode and hostname the stack was applied with, read from
state. With no state to read, it plans port-forward mode, which declares no Cognito
resources. If the stack was applied in public mode, export `ACCESS_MODE=public` and
`INGRESS_HOST` first, or the plan leaves the existing Cognito pool out and a later
apply fails on its domain prefix. The script prints the values it used; steps 3 and 4
pass the same ones.

Output goes to `infrastructure/adoption/` (gitignored):

- `imports.generated.tf` — one `import` block per resource the plan would create
- `plan.txt` — human-readable plan output, with sensitive values shown as `(sensitive value)`

No JSON copy of the plan is kept: `terraform show -json` prints sensitive values in
clear, the Cognito app client's secret among them in public mode, so the script streams
it straight into the import generator, and keeps the binary plan in a private temporary
directory that is removed however the script exits. A `plan.json` left by an earlier
version is deleted.

The summary also reports in-place updates, replacements and deletions. **Read the
warning if it appears.** Adoption only addresses creates; a planned replacement on a
stateful resource destroys data, and that needs separate attention.

### Step 2 — fill in the IDs

Every generated block has `id = "" # TODO`. That is deliberate. The plan knows the
resource *address*; it cannot know the ID of something it believes does not exist. A
guessed ID binds the wrong live resource to a config address, which is worse than not
importing — Terraform would then happily reconfigure the wrong thing.

Each block carries the ID format for its type and a CLI command that lists candidates:

```hcl
# module.iam.aws_iam_role.component["worker"]
#   id format: role name
#   find it:   aws iam list-roles --query 'Roles[].RoleName'
import {
  to = module.iam.aws_iam_role.component["worker"]
  id = "" # TODO
}
```

Import IDs are not uniform. A few that catch people:

| Resource | ID |
|---|---|
| `aws_s3_bucket` | bucket name |
| `aws_sqs_queue` | queue **URL**, not name or ARN |
| `aws_kms_key` | key **ID**, not the alias |
| `aws_kms_alias` | alias name including the `alias/` prefix |
| `aws_eks_node_group` | `cluster-name:nodegroup-name` |
| `aws_iam_role_policy` | `role-name:policy-name` |
| `aws_route_table_association` | `subnet-id/route-table-id` |
| `aws_eks_access_policy_association` | `cluster#principal-arn#policy-arn` |

**Delete the blocks for anything that genuinely does not exist.** Those should be
created, not imported.

### Step 3 — put the file in place and plan

```bash
mv adoption/imports.generated.tf .
terraform plan -var-file=environments/dev/terraform.tfvars \
  -var "access_mode=<as printed>" -var "app_host=<as printed>"
```

`import` blocks are used rather than `terraform import` CLI calls on purpose: they go
through plan, so you see what each one would attach **before** any state is written, and
they are reviewable in a merge request.

What to look for:

- `will be imported` with no change — correct.
- `will be imported` followed by an **in-place update** — the live resource differs from
  the config. Decide which is right before applying. Often the live resource was
  modified by hand and the config is the intended state, but not always.
- Still planning to **create** — the ID is wrong, or that resource really does not exist.

### Step 4 — apply, then remove the blocks

```bash
terraform apply -var-file=environments/dev/terraform.tfvars \
  -var "access_mode=<as printed>" -var "app_host=<as printed>"
rm imports.generated.tf
```

Import blocks are one-shot. Leaving them in place makes every future plan re-check
imports that already happened.

Do not skip step 3. An apply that still plans to create will fail partway, leaving some
resources created and some not — which is the state this procedure exists to get out of.

## If an import attaches the wrong resource

```bash
terraform state rm '<address>'
```

That removes it from state without touching the live resource, so you can retry with the
right ID. It is the one safe escape hatch here; `terraform destroy` at this point would
delete a resource Terraform has only just learned about.

## Why not just import everything automatically

The script deliberately stops short of running the imports. Discovering that a
resource named `pointcloud-detection-dev-input-111122223333-us-east-1-an` exists is not
the same as establishing that it is *the* bucket this config should manage — it could
belong to another environment, another stack, or another team. Getting that wrong means
Terraform starts managing, and potentially reconfiguring or destroying, something it
should not. The mapping needs a person who knows the account.

## Preventing recurrence

- Remote state with locking, which `backend.tf` now configures.
- One state key per environment: `pointcloud-detection/<env>/terraform.tfstate`.
- Do not create resources this config manages by hand. If you must, adopt them
  immediately rather than leaving the drift to be discovered later.

`deploy.sh` and `destroy.sh` no longer apply unseen changes: each saves a plan to a
private temporary directory, shows it, and asks before applying exactly that plan.
`AUTO_APPROVE=1` skips the question for unattended runs; use it only where something
else reviews the plan.
