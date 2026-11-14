# --- Stage 1: config/openclaw.json5 -> plain JSON ---------------------
# The Gateway reads /home/node/.openclaw/openclaw.json as plain JSON (see
# ../README.md's "First boot / config seeding"); git keeps the commented
# JSON5 as the source of truth. Converting at build time means the
# image always carries exactly the committed config - no hand-stripped
# copy that can drift from it.
FROM python:3.12-slim AS config
RUN pip install --no-cache-dir json5==0.9.25
COPY config/openclaw.json5 /src/openclaw.json5
RUN python -c "import json, json5; json.dump(json5.load(open('/src/openclaw.json5')), open('/src/openclaw.json', 'w'), indent=2)"

# --- Stage 2: the real image ------------------------------------------
# Pinned to the exact tag confirmed real and pullable on 2026-10-09
# (digest sha256:7f10d5cc975a90b65192eaa099454fe33ce8ce2390806c61c65cd868e9ef730d) -
# never `latest`/`main`, which move weekly (OPENCLAW_DEPLOYMENT_PLAN.md
# Section 1). Bump this by hand when upgrading, same explicit-version
# pattern this project already uses for its own backend image
# (infra/variables.tf's container_image_tag).
FROM ghcr.io/openclaw/openclaw:2026.9.9

# /home/node/.openclaw is a persistent EFS mount in production
# (infra/modules/openclaw), which shadows anything baked into that path -
# so the config is baked *beside* it and copied in on every start, before
# handing off to the image's own unchanged entrypoint. Overwriting on
# every start is deliberate: git stays the source of truth for config,
# while everything else on the volume (pairing allowlist, sqlite state,
# sessions) is left alone. Changed 2026-11-14 from the earlier
# "no ENTRYPOINT override" note: that held only while config seeding was
# planned as a manual ECS Exec step, which this replaces.
COPY --from=config --chown=node:node /src/openclaw.json /opt/openclaw-seed/openclaw.json

# Same entrypoint + CMD as the base image (confirmed via docker inspect,
# 2026-11-14: ["tini","-s","--","node","/app/docker-entrypoint.mjs"] +
# ["node","openclaw.mjs","gateway"]), with the seed copy in front.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /home/node/.openclaw && cp /opt/openclaw-seed/openclaw.json /home/node/.openclaw/openclaw.json && exec tini -s -- node /app/docker-entrypoint.mjs \"$@\"", "--"]
CMD ["node", "openclaw.mjs", "gateway"]
