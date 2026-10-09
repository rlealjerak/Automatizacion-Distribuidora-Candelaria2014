# Pinned to the exact tag confirmed real and pullable on 2026-10-09
# (digest sha256:7f10d5cc975a90b65192eaa099454fe33ce8ce2390806c61c65cd868e9ef730d) -
# never `latest`/`main`, which move weekly (OPENCLAW_DEPLOYMENT_PLAN.md
# Section 1). Bump this by hand when upgrading, same explicit-version
# pattern this project already uses for its own backend image
# (infra/variables.tf's container_image_tag).
FROM ghcr.io/openclaw/openclaw:2026.9.9

# Deliberately no ENTRYPOINT/CMD override here - confirmed by actually
# running this image locally (2026-10-09, not assumed from docs): its
# own entrypoint (tini -s -- node /app/docker-entrypoint.mjs) already
# runs `openclaw doctor --fix --non-interactive` on every start, which
# safely no-ops against an already-valid config. A custom bootstrap
# wrapper would be solving a problem the image already solves.
#
# The real config does NOT get baked into this image at build time -
# /home/node/.openclaw is where the image stores both config and
# runtime state (pairing allowlist, session history), and in production
# that path is a persistent EFS mount (OPENCLAW_DEPLOYMENT_PLAN.md
# Section 2) that would shadow anything copied here on every container
# start. See ../README.md's "First boot / config seeding" section for
# how config/openclaw.json5 actually gets onto that volume.
