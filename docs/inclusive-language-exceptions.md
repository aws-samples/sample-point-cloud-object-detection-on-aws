# Inclusive language: known exceptions

This repository follows inclusive-language conventions: no `master`, `slave`,
`whitelist`, or `blacklist` in code, comments, configuration, or documentation.
Where a term was ours to choose, it has been changed — for example the detection
label filter is `excluded_labels`, not a denylist/blocklist synonym.

A small number of identifiers cannot be changed because they are fixed by an
external API. A naive substring scan will flag them. They are listed here so a
scan can be allow-listed against a reviewed set rather than silenced wholesale,
and so a reviewer can confirm nothing new has crept in.

## Exceptions

| Identifier | Where | Why it cannot be renamed |
|---|---|---|
| `kms_master_key_id` | `infrastructure/modules/s3/main.tf`, `infrastructure/modules/sqs/main.tf` | Required argument name of the Terraform AWS provider resources `aws_s3_bucket_server_side_encryption_configuration` and `aws_sqs_queue`. Renaming it makes the configuration invalid. |

Both uses set a customer-managed KMS key. For SQS, supplying
`kms_master_key_id` is also what selects a dedicated CMK over SQS-managed SSE —
the two are mutually exclusive, so the argument is load-bearing rather than
incidental.

## Adding to this list

An entry belongs here only if the term is imposed by an external API, standard,
or protocol. If the name is ours, change the name instead. Each entry should say
where it appears and what breaks if it is renamed, so the exception can be
re-checked when the dependency changes.

## Scanner configuration

No inclusive-language scanner runs in this repository's CI (see
`.github/workflows/ci.yml` — the gates are ruff, pyright, pytest, and
`terraform fmt`/`validate`). If one is added, or if a scan gates publication,
point its allow-list at this table. Keeping the exception list in the repository
rather than in a tool-specific config means it survives a change of tool and can
be reviewed alongside the code it excuses.
