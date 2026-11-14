#!/usr/bin/env bash
# Backend deploy, as code - CLAUDE.md's "Deploy checklist" (added after the
# 2026-10-10 discovery that main and the running container had drifted for
# ~1.5 months), turned into one script so no step can be skipped by memory.
# Added 2026-11-14.
#
#   scripts/deploy_backend.sh            # deploy current HEAD of main
#
# What it does, in checklist order:
#   1. Refuses a dirty tree, a non-main branch, or an unpushed HEAD.
#      Shows the diff between what's live (/health's commit) and HEAD,
#      grouped by the areas the checklist calls out.
#   2. Builds + pushes the image tagged with the git SHA (git-<sha12>) -
#      replaces the hand-bumped v1..v9 tags, so the tag itself says what
#      code is in it.
#   3. If any migration changed: manual RDS snapshot, then migrations as
#      their own one-off ECS task, then `alembic current` == `alembic heads`
#      checked before any service is touched.
#   4. Writes the tag into infra/variables.tf (so a later plain
#      `terraform apply` can't silently roll the image back), shows the
#      plan, and applies only after you type "yes".
#   5. Waits for the services, then fails loudly unless /health's commit
#      equals HEAD.
# Step 5 of the checklist (verify the specific change against the real
# endpoint) stays manual - it depends on what changed.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
REGION=us-east-1
CLUSTER=adc-prod-cluster
HEALTH_URL=https://api.distribuidoracandelaria2014ops.com/health

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. preconditions + diff ---
[[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || die "not on main"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "HEAD is not origin/main - push or pull first"

HEAD_SHA="$(git rev-parse HEAD)"
TAG="git-${HEAD_SHA:0:12}"
LIVE_SHA="$(curl -fsS "$HEALTH_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"])')"
say "live: $LIVE_SHA  ->  deploying: $HEAD_SHA ($TAG)"
[[ "$LIVE_SHA" != "$HEAD_SHA" ]] || die "already deployed"
git cat-file -e "$LIVE_SHA^{commit}" 2>/dev/null || die "live commit $LIVE_SHA not in this repo - investigate drift before deploying"

for area in "backend/alembic/versions:DB SCHEMA" "backend/src/adc_backend/worker.py:SQS WORKER" \
            "backend/src/adc_backend/reminder_job.py:REMINDER JOB" "backend/src/adc_backend/config.py:ENV VARS / SECRETS" \
            "infra:INFRA"; do
  path="${area%%:*}"; label="${area#*:}"
  changed="$(git diff --name-only "$LIVE_SHA" "$HEAD_SHA" -- "$path")"
  [[ -n "$changed" ]] && { echo "  [$label]"; echo "$changed" | sed 's/^/    /'; }
done
SCHEMA_CHANGED="$(git diff --name-only "$LIVE_SHA" "$HEAD_SHA" -- backend/alembic/versions)"

# --- 2. build + push ---
REGISTRY="$(aws ecr describe-repositories --region $REGION --repository-names adc-prod-backend --query 'repositories[0].repositoryUri' --output text)"
say "building $REGISTRY:$TAG"
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "${REGISTRY%%/*}" >/dev/null
docker build --platform linux/amd64 --build-arg GIT_SHA="$HEAD_SHA" -t "$REGISTRY:$TAG" backend
docker push "$REGISTRY:$TAG"

run_oneoff() {  # $1 = task def, $2 = JSON command array -> prints the task's log output, fails on non-zero exit
  local net task lg
  net="$(aws ecs describe-services --cluster $CLUSTER --services adc-prod-worker --query 'services[0].networkConfiguration' --output json)"
  task="$(aws ecs run-task --cluster $CLUSTER --task-definition "$1" --launch-type FARGATE --network-configuration "$net" \
    --overrides "{\"containerOverrides\":[{\"name\":\"migrate\",\"command\":$2}]}" --query 'tasks[0].taskArn' --output text)"
  aws ecs wait tasks-stopped --cluster $CLUSTER --tasks "$task"
  lg="$(aws ecs describe-task-definition --task-definition "$1" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-group"' --output text)"
  aws logs get-log-events --log-group-name "$lg" --log-stream-name "migrate/migrate/${task##*/}" --query 'events[].message' --output text | tr '\t' '\n'
  [[ "$(aws ecs describe-tasks --cluster $CLUSTER --tasks "$task" --query 'tasks[0].containers[0].exitCode' --output text)" == "0" ]]
}

# --- 3. migrations, as their own logged step ---
if [[ -n "$SCHEMA_CHANGED" ]]; then
  SNAP="adc-prod-db-pre-${TAG}"
  say "schema changed - snapshot $SNAP first"
  aws rds create-db-snapshot --db-instance-identifier adc-prod-db --db-snapshot-identifier "$SNAP" >/dev/null
  aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP"

  say "registering one-off migration task with $TAG"
  aws ecs describe-task-definition --task-definition adc-prod-backend --query taskDefinition | python3 -c '
import json, sys
td = json.load(sys.stdin); c = td["containerDefinitions"][0]
c.update(name="migrate", image=sys.argv[1], entryPoint=[], command=["alembic", "upgrade", "head"])
c.pop("portMappings", None); c.pop("healthCheck", None)
c["logConfiguration"]["options"]["awslogs-stream-prefix"] = "migrate"
out = {k: td[k] for k in ("executionRoleArn", "taskRoleArn", "networkMode", "requiresCompatibilities", "cpu", "memory")}
out.update(family="adc-prod-migrate", containerDefinitions=[c])
json.dump(out, open("/tmp/adc-migrate-td.json", "w"))' "$REGISTRY:$TAG"
  MIGRATE_TD="$(aws ecs register-task-definition --cli-input-json file:///tmp/adc-migrate-td.json --query 'taskDefinition.taskDefinitionArn' --output text)"

  say "running migrations"
  run_oneoff "$MIGRATE_TD" '["alembic","upgrade","head"]' || die "migration task failed - services untouched; snapshot $SNAP exists"
  say "checking alembic current == heads"
  out="$(run_oneoff "$MIGRATE_TD" '["sh","-c","alembic current; alembic heads"]' | grep -v INFO)"
  echo "$out"
  [[ "$(echo "$out" | sort -u | wc -l | tr -d ' ')" == "1" ]] || die "alembic current != heads - not touching services"
fi

# --- 4. terraform ---
say "pinning container_image_tag = $TAG in infra/variables.tf"
python3 - "$TAG" <<'PY'
import re, sys
p = "infra/variables.tf"; s = open(p).read()
s2 = re.sub(r'(variable "container_image_tag" \{.*?default\s*=\s*)"[^"]*"', r'\1"%s"' % sys.argv[1], s, count=1, flags=re.S)
assert s2 != s, "container_image_tag default not found/unchanged"
open(p, "w").write(s2)
PY
( cd infra && terraform plan -out=/tmp/adc-deploy.plan )
read -r -p "Apply this plan? Type 'yes': " answer
[[ "$answer" == "yes" ]] || { git checkout -- infra/variables.tf; die "aborted - image pushed, nothing applied"; }
( cd infra && terraform apply /tmp/adc-deploy.plan )

# --- 5. verify what's actually running ---
say "waiting for services"
aws ecs wait services-stable --cluster $CLUSTER --services adc-prod-backend adc-prod-worker
LIVE_AFTER="$(curl -fsS "$HEALTH_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"])')"
[[ "$LIVE_AFTER" == "$HEAD_SHA" ]] || die "/health reports $LIVE_AFTER, expected $HEAD_SHA - deploy NOT done"
say "/health commit matches HEAD ($HEAD_SHA)"
echo "Reminder schedule state: $(aws scheduler get-schedule --name adc-prod-reminder-job --query State --output text)"

git add infra/variables.tf
git commit -m "deploy: backend $TAG" -m "Deployed via scripts/deploy_backend.sh; /health commit verified = $HEAD_SHA."
echo
echo "Done. Push the deploy commit (git push), then verify the specific change against the real endpoint (checklist step 5)."
