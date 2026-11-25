# silo

Helm chart for [Silo](https://siloserver.org) — a self-hosted media server with
Jellyfin-compatible (port 8096) and Audiobookshelf-compatible (port 13378)
endpoints — packaged for Kubernetes together with the in-cluster services it
needs: PostgreSQL (pgvector image), Redis, and optionally Meilisearch.

It is a direct conversion of the upstream multi-container layout
(`docker-compose.advanced.yml`), which runs each service in its own
container/pod instead of Silo's omnibus single container.

## Requirements

- Kubernetes **1.25+** (`kubeVersion: ">=1.25.0-0"`)
- Helm 3
- A default (or explicitly configured) storage provisioner with
  PersistentVolume support
- Optional: the [Gateway API](https://gateway-api.sigs.k8s.io) CRDs (standard
  channel, v1.0+) if you use `gateway.enabled` — they ship with neither
  Kubernetes nor this chart
- The `silo` pod must run on a node that can reach your media storage
  (see [Media](#media))

## Quick start

1. Create the at-rest credential encryption master key (Silo refuses to start
   without it):

   ```console
   openssl rand -base64 48
   kubectl create secret generic silo-secrets \
     --from-literal=SECRET_KEY='<paste the generated key>'
   ```

   Back this key up apart from your database dumps — without it, encrypted
   secrets cannot be recovered.

2. Install:

   ```console
   helm install silo ./silo \
     --set silo.existingSecret=silo-secrets
   ```

3. Reach the web UI at service port `8090`, the Jellyfin-compatible API at
   `8096` and the Audiobookshelf-compatible API at `13378`. With the default
   `ClusterIP` service, use port-forwarding or put an Ingress/Gateway in
   front (see [Gateway API](#gateway-api)); the Jellyfin/Audiobookshelf
   client apps generally need direct access to those ports, so on clusters
   without one set `service.type=LoadBalancer` (or `NodePort`).

## What the chart creates

| Kind | Name | Purpose |
| --- | --- | --- |
| StatefulSet + headless Service | `<release>-postgres` | pgvector/pgvector:pg18, `listen_addresses=*`, `pg_stat_statements` preloaded |
| StatefulSet + headless Service | `<release>-redis` | redis:8.4-alpine (pinned minor, default cache backend) |
| StatefulSet + headless Service | `<release>-meilisearch` | getmeili/meilisearch:v1.54.0 (pinned), only with `meilisearch.enabled=true` |
| StatefulSet + headless Service | `<release>-dragonfly` | DragonflyDB v2.0.2 (pinned), only with `dragonfly.enabled=true` — replaces the Redis one |
| Deployment | `<release>` | Silo itself |
| Service | `<release>` | one Service carrying web (8090→8080), jellyfin (8096), audiobookshelf (13378) |
| PVCs | `<release>-{media,plugins,artwork,compat,transcode,download-artifacts,catalog-seeds}` | silo's data directories |
| PVCs | via volumeClaimTemplates | postgres / redis / dragonfly / meilisearch data |
| Secret | `<release>-postgres-auth` | generated PostgreSQL password + assembled `DATABASE_URL` (skipped when you bring `postgres.auth.existingSecret`; with `existingSecretUrlKey` the URL comes from your secret via valueFrom) |
| Gateway (optional) | `<release>-gateway` | only with `gateway.gateway.create=true`; otherwise HTTPRoutes attach to your existing Gateway |
| HTTPRoute(s) (optional) | `<release>` (+ `<release>-jellyfin`, `<release>-audiobookshelf`) | only with `gateway.enabled=true` |
| NetworkPolicy(s) (optional) | `<release>-{postgres,redis,dragonfly,meilisearch}` | default-deny ingress+egress per datastore, only with `networkPolicy.enabled=true` |
| Job (helm test hook) | `<release>-test` | TCP smoke checks; run with `helm test <release>` |

## docker-compose → chart mapping

| compose | chart |
| --- | --- |
| `postgres` service | `postgres.enabled` (StatefulSet) |
| `shm_size: 8gb` | `postgres.shmSize` — tmpfs `emptyDir{medium: Memory}` mounted at `/dev/shm` |
| `command: [postgres, -c, ...]` | `postgres.args` |
| `redis` service | `redis.enabled` (StatefulSet) — or `dragonfly.enabled=true` to swap in Redis-wire-compatible DragonflyDB |
| `meilisearch` with `profiles: [search]` | `meilisearch.enabled=false` by default; enabling without a master key fails rendering, like the compose entrypoint check |
| `stop_grace_period: 2m` | `silo.terminationGracePeriodSeconds: 120` |
| `cap_add: [NET_ADMIN]` | `silo.capabilities: [NET_ADMIN]` (set `[]` to undo) |
| `depends_on: condition: service_healthy` | `silo.initContainers.enabled` — a small busybox init container waits for postgres/redis |
| `PUID` / `PGID` | `silo.puid` / `silo.pgid` → `PUID`/`PGID` env vars (the image's s6 init drops privileges to them; `fsGroup` = pgid) |
| `${MEDIA_ROOT}:...:ro` | `silo.persistence.media` (PVC or `existingClaim`, mounted read-only) |
| `SILO_DATA_ROOT` subdirectories | one PVC each under `silo.persistence.*` |
| `/proc/{meminfo,stat,loadavg}` host mounts | `silo.hostProc.enabled` (**off by default** — only useful when the Kubernetes nodes are themselves LXC containers; needs hostPath permissions) |
| `devices: [/dev/dri:/dev/dri]` | `silo.transcode.vaapi.enabled=true` |
| NVIDIA `deploy.resources.reservations.devices` | `silo.transcode.nvidia.enabled=true` (+ `runtimeClassName`, `count`) |
| `PORT` / `JF_PORT` / `ABS_PORT` host mappings | `service.ports.*` (container always listens on 8080/8096/13378) |
| `env_file: .env` | `silo.extraEnv` |
| `POSTGRES_TUNE` | `silo.postgresTune` (default `auto`) |

## Configuration

### Media

Point Silo at your media with a PVC:

```yaml
silo:
  persistence:
    media:
      size: 100Gi
      storageClass: fast-nvme   # or use the top-level storageClass
```

or at an existing claim (e.g. an NFS-backed one):

```yaml
silo:
  persistence:
    media:
      existingClaim: media-nfs
```

The media is mounted read-only (`readOnly: true`), like compose. The image's
s6 init drops privileges to `PUID:PGID` (default 1000:1000), which needs read
access to the media; the chart sets `fsGroup` to PGID so fresh PVC mounts are
group-accessible.

### Backups volume

Since 0.4.1 the chart ships a dedicated volume for Silo's own backups
(`silo.persistence.backups`, mounted at `/var/lib/silo/backups`). Silo's
pre-migration guard refuses to run database migrations unless `1.5x the
previous backup + 1GB` is free at the backup destination — a budget a small
data claim cannot honor once a few backups exist, and then upgrades stall
until you free space. Give the destination its own room:

1. In Silo, set the backup destination to `/var/lib/silo/backups`
   (Settings → Backups). The chart only provides the volume; the app stores
   the destination path in its own settings.
2. Grow it before it fills — PVCs never shrink:

   ```yaml
   silo:
     persistence:
       backups:
         size: 50Gi
   ```

   If you ever resized the claim manually with `kubectl patch`, mirror the
   live size in your values (`--set silo.persistence.backups.size=...`)
   before upgrading: a chart size below the live claim fails the upgrade
   with a forbidden-shrink error.
3. Or bring your own claim:

   ```yaml
   silo:
     persistence:
       backups:
         existingClaim: backup-nfs
   ```

Retention and pruning of old backups stay the app's job (Settings → Backups);
size the volume for a few backups plus the guard's margin. Like every data
claim, the backups PVC carries `helm.sh/resource-policy: keep` and survives
`helm uninstall`.

### Runtime user (root init, non-root escape hatch)

The Silo image uses an s6-overlay init that **must start as root** and then
drops privileges to `PUID:PGID` itself — exactly how the compose stack runs
it (compose sets no `user:`). The chart therefore does not set `runAsUser`;
forcing uid 1000 at start makes the init abort with *"Started as uid …, but
this image's init needs root"*.

If your cluster enforces the `restricted` pod-security profile (or you just
want a non-root container), enable the image's documented escape hatch:

```yaml
silo:
  nonRootMode: true          # runAsNonRoot as puid:pgid, command: ["silo"]
  databaseUrlOverride: postgres://silo:pass@my-cluster:5432/silo?sslmode=disable
  redisUrlOverride: redis://my-redis:6379
  postgres:
    enabled: false
```

Per the image's docs, non-root mode requires an external PostgreSQL, so the
chart fails rendering if `databaseUrlOverride` is missing in this mode.

### Database / cache

- PostgreSQL password: leave `postgres.auth.password` empty and the chart
  generates a random one on first install and keeps it stable across upgrades
  (stored in `<release>-postgres-auth`). Set `postgres.auth.password`
  explicitly, or bring your own `postgres.auth.existingSecret` (key:
  `postgres.auth.existingSecretPasswordKey`, default `postgres-password`).
- External managed services: disable the built-ins and point Silo elsewhere —
  the chart fails fast if you forget the URLs:

  ```yaml
  postgres:
    enabled: false
  redis:
    enabled: false
  silo:
    databaseUrlOverride: postgres://silo:pass@my-cluster:5432/silo?sslmode=disable
    redisUrlOverride: redis://my-redis:6379
  ```

  Note: with `databaseUrlOverride` (or `postgres.auth.existingSecret` without
  `existingSecretUrlKey`) the `DATABASE_URL` is rendered into the Deployment
  spec as plain text, so restrict `kubectl get deployment` access accordingly.
  With `postgres.auth.existingSecret` + `postgres.auth.existingSecretUrlKey`
  the URL is pulled from your secret via `valueFrom` and never appears in the
  pod spec.

### Cache backend: Redis or DragonflyDB

The cache backend (Silo's `REDIS_URL`) is Redis by default. DragonflyDB is a
drop-in alternative that speaks the Redis wire protocol on the same port
6379 — Silo's URL simply changes to `<release>-dragonfly:6379`, and the
init-container wait follows automatically:

```yaml
redis:
  enabled: false
dragonfly:
  enabled: true
```

(The two are mutually exclusive — enabling both fails rendering, as does
disabling both without `silo.redisUrlOverride`.)

An honest read before you switch, per Dragonfly's own docs and v2.0 sources:

- **Compatibility**: Dragonfly implements ~190 Redis commands, Lua 5.4.4
  scripting, pub/sub and streams — the surface a cache/queue client uses.
  Edge-case differences exist (e.g. 256 MB string limit, expiry clamping
  beyond ~8.5 years; see its "Differences with Redis" doc). Silo's exact
  command mix can't be verified from a chart, so smoke-test against your
  data before switching a loaded release over.
- **Memory in Kubernetes**: Dragonfly sizes nothing from cgroup limits. Set
  a container memory limit *and* `--maxmemory` below it, or the pod gets
  OOM-killed under pressure — same as stock Redis without `maxmemory`, but
  Dragonfly's baseline footprint is larger. `--cache_mode=true` enables
  eviction near `--maxmemory`; do **not** use it if queue/job state lives in
  this backend, eviction would silently drop it.
- **Threads**: `--proactor_threads=1` is the chart default because Dragonfly
  sizes its thread pool from the node's core count, not the container's CPU
  limit. Raise it (and the CPU limit) deliberately.
- **THP**: the transparent-huge-pages requirement from the 1.x era is gone —
  the v2.x source contains no THP check, so any node THP setting works.
- **Version**: pinned `v2.0.2`, the last BSL 1.1 release; with v2.1
  (2026-11) Dragonfly moved to a source-available license. Bump deliberately.
- **Expectations**: for a single-instance Silo the throughput win is mostly
  theoretical — pick Dragonfly for ops preferences (one modern binary,
  efficient snapshots), not measured speed.

### Search (Meilisearch)

```yaml
meilisearch:
  enabled: true
  masterKey: "<openssl rand -base64 32>"
```

`masterKey` is required (mirroring the compose entrypoint's check) unless you
set `meilisearch.existingSecret`. The image is pinned (`v1.54.0`) because
Meilisearch will not open data written by another version; bump the tag
deliberately, together with `meilisearch.upgradeDb=true`.

The compose file wires Silo to the search index through its `.env` server
settings; this chart does not guess the variable name. Point Silo at the
service explicitly with `silo.extraEnv`, using the variable documented in
Silo's `.env.example`:

```yaml
silo:
  extraEnv:
    - name: SILO_MEILI_URL        # ← check Silo's .env.example for the real name
      value: http://silo-meilisearch:7700
```

(adjust the host to your release name). Meilisearch's `/health` endpoint is
used for its probes.

### Hardware transcoding

```yaml
silo:
  transcode:
    vaapi:
      enabled: true        # Intel/AMD Quick Sync: /dev/dri hostPath
    nvidia:
      enabled: true        # needs NVIDIA container runtime + device plugin
      count: 1
```

VA-API mounts the node's `/dev/dri` directory (customize with
`silo.transcode.vaapi.hostPath`). NVIDIA sets
`runtimeClassName: nvidia` and injects an `nvidia.com/gpu` request/limit —
unless you provide your own `silo.resources` map, which then wins (include the
GPU claim in it yourself). Note that pods then schedule onto GPU nodes; pin
the media node with `silo.nodeSelector`/`tolerations` if needed.

### Probes, shutdown, networking

- Silo's probes are `tcpSocket` against the always-open 8080 listener — the
  compose file defines no HTTP healthcheck for Silo, so none was invented.
  Tune or disable under `silo.probes`.
- `silo.terminationGracePeriodSeconds: 120` mirrors `stop_grace_period: 2m`;
  during shutdown Silo fails readiness and keeps serving in-flight requests.
- `silo.capabilities: [NET_ADMIN]` reproduces compose's BBR congestion-control
  setup; set `[]` to remove the capability, or turn parts off with
  `SILO_NET_TCP_CONGESTION=off` / `SILO_NET_TCP_SEND_BUFFER=off` via
  `silo.extraEnv`.
- Updates use strategy `Recreate` because all claims are ReadWriteOnce.

### Gateway API

The chart speaks Gateway API (`gateway.networking.k8s.io/v1`, standard
channel) instead of the legacy Ingress. With the Gateway API CRDs installed,
the minimal setup attaches the HTTPRoutes to an existing Gateway:

```yaml
gateway:
  enabled: true
  hostnames:
    - silo.example.com
  parentRefs:
    - name: my-gateway
      namespace: gateway-system
      sectionName: https     # optional: pick one listener
```

This creates an HTTPRoute for the web UI pointing at the release Service on
port 8090. Alternatively, let the chart create its own Gateway (requires a
`gatewayClassName` installed in the cluster, e.g. `envoy`, `cilium`, `nginx`):

```yaml
gateway:
  enabled: true
  hostnames:
    - silo.example.com
  gateway:
    create: true
    gatewayClassName: envoy
```

With no `listeners` override this Gateway listens on plain HTTP port 80 and
accepts routes from its own namespace; override `gateway.gateway.listeners`
to terminate TLS, e.g.:

```yaml
gateway:
  gateway:
    listeners:
      - name: https
        port: 443
        protocol: HTTPS
        tls:
          mode: Terminate
          certificateRefs:
            - kind: Secret
              name: silo-tls
        allowedRoutes:
          namespaces:
            from: Same
```

The Jellyfin- and Audiobookshelf-compatible ports are plain client APIs —
client apps expect a dedicated hostname, not a path prefix, so each gets its
own optional HTTPRoute with its own hostnames:

```yaml
gateway:
  jellyfin:
    enabled: true
    hostnames:
      - jellyfin.example.com
  audiobookshelf:
    enabled: true
    hostnames:
      - audiobooks.example.com
```

(they attach to the same parentRefs as the web route). Misconfigurations fail
at render time: enabling the gateway without hostnames, without a parent
(`parentRefs` or `gateway.create`), or with `create=true` but no
`gatewayClassName`.

### Network isolation (optional)

`networkPolicy.enabled=true` creates one NetworkPolicy per in-cluster
datastore (postgres, redis/dragonfly, meilisearch) with **default-deny
ingress and egress**: ingress only from this release's Silo pods in the same
namespace (plus the `helm test` job, so `helm test` still passes), and no
egress at all — a datastore never initiates connections. Requires a CNI that
enforces NetworkPolicy (Calico/Cilium/kube-router do; flannel does not).
Widen the posture with `networkPolicy.extraIngress` / `networkPolicy.extraEgress`
(raw NetworkPolicy `ingress`/`egress` items, appended to every datastore
policy) — e.g. an admin pod, a monitoring scraper, or DNS — or disable the
policies if you run cross-namespace database tooling.

### Capability hardening (Drop ALL)

By default every workload in this chart **drops all Linux capabilities**
(`capDrop: [ALL]`) and re-adds only the minimal set the image actually
needs — removing the risky Docker defaults (`NET_RAW`, `SYS_CHROOT`,
`MKNOD`, `AUDIT_WRITE`, `SETFCAP`, ...) that compose grants silently:

| workload | re-added | why |
| --- | --- | --- |
| silo | `CHOWN, FOWNER, DAC_OVERRIDE, SETGID, SETUID` + your `silo.capabilities` (`NET_ADMIN`) | root s6 init chowns data dirs and drops to `PUID:PGID`; BBR / send-buffer helper |
| postgres, redis | `CHOWN, FOWNER, SETGID, SETUID` | entrypoint chowns the data dir, gosu privilege drop |
| dragonfly, meilisearch | — | direct exec / non-root image, nothing needed |

If an image update ever needs a capability, add it to that component's
`capabilities` list; set `dropAllCapabilities: false` on any component to
restore the full default set (compose parity).

### Upgrade notes (0.5.0)

- **Resource-name length guard:** `fullname` is truncated to 44 characters so
  suffixed names (`-download-artifacts`, ...) stay inside Kubernetes' 63-char
  DNS label limit. Only matters if your release name is long: if truncation
  changes your resource names, PVCs would be recreated (data loss) — set
  `fullnameOverride` to your existing names (or stay on 0.4.x) before upgrading.
- `silo.replicaCount` may now be `0` (maintenance/migration) or `1`.
- `gateway.gateway.create=true` and `gateway.parentRefs` are mutually
  exclusive; rendering fails if both are set (previously `parentRefs`
  silently won).
- The `wait-for-datastores` init container only renders when an in-cluster
  datastore (postgres/redis/dragonfly) is enabled — external-only installs
  no longer start a sidecar that waits on services that don't exist.
- New: secret-backed `silo.databaseUrlExistingSecret` /
  `silo.redisUrlExistingSecret` (credentials leave the pod spec), per-volume
  `enabled: false` and `type: emptyDir`, checksum annotations that roll pods
  when explicit secrets change, and hardened init/test pods (non-root, drop
  ALL capabilities).

## Migrating from docker compose

There is no in-place upgrade: Kubernetes reads different volumes. A manual
migration is:

1. Stop the compose stack.
2. Dump PostgreSQL from the old container
   (`docker exec <pg> pg_dump -U silo silo > silo.sql`).
3. Restore media access: either give the cluster a PVC backed by the same
   storage (`existingClaim`), or copy the tree into the new
   `<release>-media` volume (e.g. with a helper pod and `kubectl cp`).
4. `helm install` the chart, then restore the dump into the new database
   (`psql -U silo silo < silo.sql`).
5. Copy the contents of `plugins`, `artwork`, `compat`, `transcode`,
   `download-artifacts`, `catalog-seeds`, `redis`, `meilisearch` from your
   `SILO_DATA_ROOT` into the matching PVCs the same way, with the release
   scaled down (`kubectl scale deployment silo --replicas=0`).

## Values reference

| Key | Default | Notes |
| --- | --- | --- |
| `storageClass` | `""` | cluster default; per-claim `storageClass` overrides |
| `imagePullSecrets` | `[]` | applied to every component |
| `postgres.enabled` | `true` | disable + `silo.databaseUrlOverride` for external DB |
| `postgres.shmSize` | `8Gi` | tmpfs `/dev/shm` (compose `shm_size`) |
| `postgres.persistence.size` | `20Gi` | |
| `redis.enabled` | `true` | disable + `silo.redisUrlOverride` |
| `redis.persistence.size` | `8Gi` | |
| `dragonfly.enabled` | `false` | Redis wire-compatible replacement for the Redis StatefulSet |
| `dragonfly.image.tag` | `v2.0.2` | pinned (last BSL 1.1 release) |
| `dragonfly.args` | `--alsologtostderr --proactor_threads=1` | add `--maxmemory`/`--cache_mode` deliberately |
| `dragonfly.persistence.size` | `8Gi` | |
| `meilisearch.enabled` | `false` | needs `masterKey` or `existingSecret` |
| `meilisearch.persistence.size` | `10Gi` | |
| `silo.replicaCount` | `1` | rendering fails above 1 by design |
| `silo.existingSecret` | `""` | **required** (or `silo.secretKey` for trials) |
| `silo.puid` / `silo.pgid` | `1000` / `1000` | PUID/PGID for the s6 privilege drop + `fsGroup` |
| `silo.nonRootMode` | `false` | skip the root s6 init (`runAsNonRoot`, `command: [silo]`); needs external PostgreSQL |
| `silo.terminationGracePeriodSeconds` | `120` | compose `stop_grace_period` |
| `silo.updateStrategy` | `Recreate` | required for RWO claims |
| `silo.capabilities` | `[NET_ADMIN]` | set `[]` to drop |
| `silo.transcode.*` | disabled | VA-API hostPath / NVIDIA runtimeClass |
| `silo.hostProc.enabled` | `false` | `/proc` host mounts (LXC nodes only) |
| `silo.persistence.*.size` | see values.yaml | one PVC per data dir |
| `silo.persistence.backups` | `20Gi` → `/var/lib/silo/backups` | dedicated volume for Silo's own backups (see "Backups volume") |
| `silo.extraEnv` | `[]` | replaces compose `env_file` |
| `service.type` | `ClusterIP` | ports 8090 / 8096 / 13378 |
| `gateway.enabled` | `false` | Gateway API HTTPRoute for the web UI |
| `gateway.hostnames` | `[]` | required when enabled |
| `gateway.parentRefs` | `[]` | attach to an existing Gateway |
| `gateway.gateway.create` | `false` | chart-managed Gateway (needs `gatewayClassName`) |
| `gateway.jellyfin.*` / `gateway.audiobookshelf.*` | disabled | optional HTTPRoutes for the compat ports |
| `redis.image.tag` | `8.4-alpine` | pinned minor; bump deliberately |
| `postgres.auth.existingSecretUrlKey` | `""` | take `DATABASE_URL` from your secret via valueFrom |
| `networkPolicy.enabled` | `false` | default-deny ingress+egress on the datastores |
| `networkPolicy.extraIngress` / `.extraEgress` | `[]` | widen the datastore policies |
| `<component>.dropAllCapabilities` | `true` | capDrop `[ALL]` on every workload, minimal re-add (see "Capability hardening"); `false` = compose parity |
| `tests.enabled` | `true` | `helm test` smoke-check hook |

## Install from this repository

This repository doubles as a Helm repository: the `main` branch holds the
chart source and the packaged `.tgz`, the `gh-pages` branch holds the
repository index served by GitHub Pages.

```console
helm repo add silo https://jpjpjunior.github.io/helm-silo
helm repo update
helm install silo silo/silo --set silo.existingSecret=silo-secrets
```

or install the packaged chart directly from the `main` branch:

```console
helm install silo \
  https://raw.githubusercontent.com/jpjpjunior/helm-silo/main/silo-0.4.0.tgz \
  --set silo.existingSecret=silo-secrets
```

## Upgrades

The StatefulSets' `volumeClaimTemplates` carry only version-stable labels
(chart version labels there would make every `helm upgrade` fail with
*"updates to statefulset spec ... are forbidden"*, since VCTs are immutable).
Releases **≤ 0.3.0** had a version-bearing `helm.sh/chart` label on the VCTs;
when upgrading from such a release to ≥ 0.3.1, recreate the StatefulSets
**once** (the PVCs are never touched by a StatefulSet delete — data is
preserved and re-adopted by the recreated StatefulSet):

```console
kubectl delete sts silo-postgres silo-redis silo-meilisearch silo-dragonfly \
  --cascade=orphan --ignore-not-found
helm repo update
helm upgrade silo silo/silo --version 0.4.0 --reuse-values
```

(`--cascade=orphan` keeps the pods running while the objects are recreated;
the new StatefulSet re-adopts them and rolls them onto the same PVCs.)
After swapping redis → dragonfly, the old `data-silo-redis-0` PVC can be
reclaimed once the DragonflyDB backend is verified working.

## Uninstall & data safety

All data PVCs (the seven chart-managed silo volumes and the StatefulSet
claims) carry the `helm.sh/resource-policy: keep` annotation, so
`helm uninstall` never deletes data — the claims stay behind. Reclaim them
deliberately when you really want them gone:

```console
kubectl delete pvc -l app.kubernetes.io/instance=silo -n <namespace>
```

## Publishing (CI)

`.github/workflows/chart.yml` validates every push to `main`: `helm lint`
(which exercises `values.schema.json` and `NOTES.txt`), the chart validator
(`ci/validate_chart.py`, 12+ scenarios), and kubeconform.

Releasing is a tag — the workflow packages the chart, commits the `.tgz` to
`main` and updates the index on `gh-pages` with the built-in `GITHUB_TOKEN`
(no personal access token involved):

```console
# after bumping version in Chart.yaml and committing:
git tag v0.4.0
git push origin main v0.4.0
```

The tag must match `Chart.yaml`'s version (the workflow verifies it). Manual
publishing still works if Actions are unavailable: `helm package . -d .`,
regenerate the index on `gh-pages` (`helm repo index . --url
https://jpjpjunior.github.io/helm-silo`), and push both branches. Add
yourself to `maintainers` in `Chart.yaml`, and optionally submit the repo to
Artifact Hub.
