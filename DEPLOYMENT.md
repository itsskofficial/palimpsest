# Deployment

Three ways to run this, in increasing order of commitment. All three use the same image
and the same schema; only credentials change.

> **Before anything else.** A deployment of palimpsest can read and edit your private
> notes. Every path below starts in propose-only mode (`PALIMPSEST_APPLY=0`) and the app
> refuses to bind to a public interface without an API key. Keep both.

---

## 1. Local, on SQLite (30 seconds)

```bash
pip install -e ".[anthropic,serve]"

export NOTION_TOKEN=ntn_...
export ANTHROPIC_API_KEY=sk-ant-...

palimpsest sync                 # pull your workspace into palimpsest.db
palimpsest sweep duplicates     # works without the model key
palimpsest serve                # http://127.0.0.1:8100
```

The whole mirror is one file. Delete `palimpsest.db` and you have deleted everything
palimpsest knows; your Notion is untouched.

**Getting a Notion token.** Create an internal integration at
[notion.so/my-integrations](https://www.notion.so/my-integrations), copy the secret,
then **share at least one page with it** (page → `⋯` → Connections → your integration).
An integration sees nothing until you do, and the symptom is an empty sync rather than
an error message.

---

## 2. Local, on real Supabase (the one to develop against)

```bash
python scripts/dev.py up        # starts Supabase, migrates, creates the archive bucket
python scripts/dev.py serve
```

Ports are in palimpsest's own `545xx` range so this runs alongside other local Supabase
projects: API `54521`, Postgres `54522`, Studio `54523`.

**Why bother when SQLite works.** Local Supabase is the same Postgres, the same
pgBouncer, the same Storage API, the same RLS and the same migrations as the cloud
project you will deploy against — so the only thing that changes on the way to AWS is
credentials. "It worked locally" then means something. A plain `postgres:16` container
would be a different thing wearing the same name, and every Supabase-specific problem
(pooler semantics, RLS, Storage auth) would stay hidden until deploy day.

Or with containers:

```bash
docker compose -f deploy/docker-compose.yml up --build
docker compose -f deploy/docker-compose.yml --profile tools run --rm sync
```

---

## 3. AWS: ECS Fargate + ALB + S3 + Secrets Manager

```bash
cd deploy/terraform
terraform init
terraform apply \
  -var="notion_token=ntn_..." \
  -var="anthropic_api_key=sk-ant-..." \
  -var="api_key=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  -var="supabase_project_ref=<ref>" \
  -var="supabase_db_password=<password>" \
  -var="budget_alert_email=you@example.com"
```

### What it creates, and what it costs

| Resource | ~USD/month (ap-south-1, on-demand) |
|---|---|
| ALB | 18 — the only unavoidable fixed cost |
| Fargate app (0.5 vCPU, 1 GB) | 15 |
| ECR + S3 + CloudWatch logs | 2 |
| **Total** | **~35**, about 0.35% of the AWS credit per month |

Two things it deliberately does **not** create:

- **A database.** The default is Supabase — a credit you already have and one less thing
  to pay for. `-var="create_rds=true"` adds an RDS instance (~$15/month) if you would
  rather keep it all in AWS.
- **A NAT gateway.** It is ~$32/month and this workload does not need one: tasks run in
  public subnets with public IPs, locked down by a security group that only accepts
  traffic from the load balancer. That is the standard trade for a small service and it
  is worth knowing you are making it.

### After `terraform apply`

Terraform prints the exact commands. In short:

```bash
# 1. build and push
aws ecr get-login-password --region ap-south-1 \
  | docker login --username AWS --password-stdin <ecr_repository_url>
docker build -f deploy/Dockerfile -t <ecr_repository_url>:latest .
docker push <ecr_repository_url>:latest

# 2. migrate once
aws ecs run-task --cluster palimpsest-prod \
  --task-definition palimpsest-prod-migrate --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[...],securityGroups=[...],assignPublicIp=ENABLED}'

# 3. roll the app, then check it
aws ecs update-service --cluster palimpsest-prod --service palimpsest-prod-api --force-new-deployment
curl http://<alb-dns>/healthz
curl -H "Authorization: Bearer $PALIMPSEST_API_KEY" http://<alb-dns>/v1/status
```

### Scheduling the mirror sync

`sync` is a run-once task definition, not a service — it finishes. Point an EventBridge
rule at it:

```bash
aws events put-rule --name palimpsest-sync --schedule-expression "rate(6 hours)"
aws events put-targets --rule palimpsest-sync --targets '[{
  "Id": "sync",
  "Arn": "arn:aws:ecs:ap-south-1:<acct>:cluster/palimpsest-prod",
  "RoleArn": "<events-invoke-role-arn>",
  "EcsParameters": {
    "TaskDefinitionArn": "<sync task definition arn>",
    "LaunchType": "FARGATE",
    "NetworkConfiguration": {"awsvpcConfiguration": {
      "Subnets": ["subnet-...","subnet-..."],
      "SecurityGroups": ["sg-..."],
      "AssignPublicIp": "ENABLED"}}
  }}]'
```

Six hours is a reasonable default: the sync is incremental, so an unchanged workspace
costs one search call.

### CI/CD

`.github/workflows/deploy.yml` builds, pushes, migrates and rolls. It authenticates with
**OIDC**, not a stored access key, so there is no long-lived secret in the repository.
Set `AWS_DEPLOY_ROLE_ARN` to a role trusting your repo:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
      "StringLike": {"token.actions.githubusercontent.com:sub": "repo:<you>/palimpsest:*"}
    }
  }]
}
```

Deployment is manual (`workflow_dispatch`) or tag-triggered by default. A deployment
that happens because someone merged a README change is a deployment nobody is watching.

---

## Supabase specifics that will bite you

**Three connection strings, and they behave differently.**

| Dashboard name | Port | What it is |
|---|---|---|
| Direct connection | 5432 | a real Postgres session. **IPv6-only on the free tier** — a Fargate task in this stack cannot reach it |
| Session pooler | 5432 | pgBouncer in *session* mode. Behaves like Postgres. IPv4 |
| Transaction pooler | 6543 | pgBouncer in *transaction* mode. **Different semantics** |

Use the **transaction pooler** for the running service, and the session pooler or direct
URL for migrations. `palimpsest db migrate` refuses the transaction pooler and prints the
fix — the advisory lock that makes concurrent migrations safe needs a session, and in
transaction mode it would be taken and released per statement, protecting nothing.

`palimpsest supabase url --project-ref <ref> --password <pw> --purpose service` builds
the right one, URL-encoding the password (Supabase-generated passwords routinely contain
`@`, `/` and `?`, each of which breaks a naively concatenated URL with a confusing
authentication error).

**RLS is on and there are no policies.** Migration `0002_rls` enables row-level security
on every table and defines nothing, which denies `anon` and `authenticated` completely.
These tables hold a mirror of your private notes and Supabase exposes `public` through
PostgREST — a table here without RLS is readable with the anon key that ships in a
browser. palimpsest never uses the Data API; it connects over SQL as the owner.

---

## Operations

```bash
palimpsest status                    # config, mirror size, and what looks wrong
palimpsest db check                  # reachability, applied vs pending migrations
palimpsest history <page_id>         # every applied change to a page
palimpsest provenance <block_id>     # which source produced this text
palimpsest patches --status applied  # what has been written
palimpsest undo <patch_id>           # revert exactly
```

`palimpsest status` exits non-zero when it finds a problem, so it works as a health
check in a script. `/healthz` deliberately touches nothing external — a liveness probe
that fails when Notion blips gets a healthy process killed. `/readyz` checks the
database.

**Logs:** `aws logs tail /ecs/palimpsest-prod --follow`. Set `PALIMPSEST_LOG_JSON=1`
(the container default) so CloudWatch Logs Insights can query them.

**Metrics:** `/metrics` in Prometheus text format — request counts, latency histogram,
and mirror/ledger row counts.

---

## Cost control

- **The budget alarm is created by default** when you set `budget_alert_email`. AWS
  Activate credits do not stop services when they expire — they start billing the card
  on file. Set a calendar reminder 60 days before expiry too.
- **Model spend is per-source and visible.** `palimpsest ingest --out run.json` records
  token counts and an estimated cost. The classifier's cached prefix means the marginal
  cost of a claim is roughly its own tokens.
- **`--max-windows`** caps extraction for a cheap first look at a long source.
- **Re-ingesting is free** — sources are keyed by content hash.
- **Lower `PALIMPSEST_CLASSIFY_EFFORT`** to `medium` if the bill matters more than the
  last few points of precision. Measure before you do; the classifier is the product.

## Security

- The service-role key and the Notion token live in Secrets Manager and arrive as
  `secrets` entries, so they never appear in `describe-task-definition` or CloudTrail.
- The task role is scoped to the artifact bucket only.
- `/v1/status` redacts every credential-shaped value.
- Narrow `allowed_cidrs` to your own IP while developing. The API key is the real
  control, but this deployment can edit your notes and defence in depth is free.
- Put a certificate on it (`certificate_arn`) before pointing it at anything you care
  about; without one the ALB serves plain HTTP and your notes cross the internet in
  clear text.
