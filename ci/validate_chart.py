#!/usr/bin/env python3
"""Validate the silo Helm chart: lint + render scenarios + assertions."""
import os
import subprocess
import sys
import yaml

HELM = os.environ.get("HELM_BIN", "helm")
CHART = os.environ.get("CHART_DIR", ".")
TV = os.environ.get("TEST_VALUES_DIR", "ci/test-values")

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def render(values_file):
    cmd = [HELM, "template", "silo", CHART, "-f", values_file]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p


def docs(p):
    out = []
    for d in yaml.safe_load_all(p.stdout):
        if d:
            out.append(d)
    return out


def by_kind(ds, kind):
    return [d for d in ds if d["kind"] == kind]


def get_env(dep, name):
    for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]:
        if e["name"] == name:
            return e
    return None


def vol_mounts(dep):
    return dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]


def volumes(dep):
    return dep["spec"]["template"]["spec"]["volumes"]


def run_scenario(tag, values_file, assertions):
    print(f"\n=== Scenario {tag} ===")
    p = render(values_file)
    if p.returncode != 0:
        check(False, f"{tag}: helm template failed: {p.stderr.strip()[:500]}")
        return
    lint = subprocess.run([HELM, "lint", CHART, "-f", values_file],
                          capture_output=True, text=True)
    check(lint.returncode == 0, f"{tag}: helm lint clean ({lint.stdout.strip().splitlines()[-1] if lint.stdout else ''})")
    ds = docs(p)
    assertions(ds)


def a_default(ds):
    k = {}
    for d in ds:
        k.setdefault(d["kind"], []).append(d)
    check(len(k.get("StatefulSet", [])) == 2, "A: 2 StatefulSets (postgres, redis)")
    check(len(k.get("Deployment", [])) == 1, "A: 1 Deployment (silo)")
    check(len(k.get("Service", [])) == 3, "A: 3 Services (2 headless + 1 main)")
    check(len(k.get("PersistentVolumeClaim", [])) == 8, "A: 8 PVCs (silo mounts, incl backups)")
    b = [p for p in k["PersistentVolumeClaim"] if p["metadata"]["name"] == "silo-backups"][0]
    check(b["spec"]["resources"]["requests"]["storage"] == "20Gi", "A: backups PVC 20Gi")
    check(len(k.get("Secret", [])) == 1, "A: 1 chart Secret (postgres-auth)")
    dep = by_kind(ds, "Deployment")[0]
    stss = by_kind(ds, "StatefulSet")
    sts_names = [s["metadata"]["name"] for s in stss]
    check("silo-postgres" in sts_names and "silo-redis" in sts_names, "A: STS names silo-postgres/silo-redis")
    # images
    c = dep["spec"]["template"]["spec"]["containers"][0]
    check(c["image"] == "ghcr.io/silo-server/silo-server:latest", f"A: silo image ({c['image']})")
    check(c["imagePullPolicy"] == "Always", "A: silo pullPolicy Always")
    pg = [x for x in stss[0]["spec"]["template"]["spec"]["containers"] + stss[1]["spec"]["template"]["spec"]["containers"] if x["name"] in ("postgres", "redis")]
    imgs = {x["name"]: x["image"] for x in pg}
    check(imgs.get("postgres") == "pgvector/pgvector:pg18", f"A: postgres image ({imgs.get('postgres')})")
    check(imgs.get("redis") == "redis:8.4-alpine", f"A: redis image pinned ({imgs.get('redis')})")
    for x in pg:
        sec = x.get("securityContext", {})
        check(sec.get("allowPrivilegeEscalation") is False, f"A: {x['name']} allowPrivilegeEscalation false")
        check("capabilities" not in sec, f"A: {x['name']} default cap set (no capDrop, no capAdd)")
    podsec = dep["spec"]["template"]["spec"].get("securityContext", {})
    check(podsec.get("fsGroupChangePolicy") == "OnRootMismatch",
          f"A: fsGroupChangePolicy OnRootMismatch ({podsec.get('fsGroupChangePolicy')})")
    # wait-for-datastores init container: rendered only when an in-cluster
    # datastore is enabled, and hardened (restricted-PSS shape)
    ics = dep["spec"]["template"]["spec"].get("initContainers", [])
    ic0 = ics[0] if ics else {}
    icsec = ic0.get("securityContext", {})
    check(ics and icsec.get("runAsNonRoot") is True and icsec.get("runAsUser") == 65534,
          f"A: wait-for-datastores hardened (runAsNonRoot, uid 65534) ({icsec})")
    check(ics and icsec.get("capabilities", {}).get("drop") == ["ALL"], "A: initContainer drops ALL caps")
    icscript = (ic0.get("args") or [""])[0]
    check("postgres" in icscript and "redis" in icscript, "A: initContainer waits on postgres+redis")
    # securityContext / strategy / grace
    ps = dep["spec"]["template"]["spec"]["securityContext"]
    check(ps.get("fsGroup") == 1000 and "runAsUser" not in ps and not ps.get("runAsNonRoot"), "A: pod starts as root for s6 init (fsGroup=pgid, no runAsUser)")
    check(get_env(dep, "PUID") and get_env(dep, "PUID")["value"] == "1000" and get_env(dep, "PGID")["value"] == "1000", "A: PUID/PGID env vars for s6 privilege drop")
    check(dep["spec"]["strategy"]["type"] == "Recreate", "A: Recreate strategy")
    check(dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 120, "A: terminationGracePeriodSeconds 120")
    capsc = c.get("securityContext", {}).get("capabilities", {})
    check("drop" not in capsc, f"A: silo keeps the default cap set (no capDrop) ({capsc.get('drop')})")
    check(capsc.get("add") == ["NET_ADMIN"], f"A: silo capAdd NET_ADMIN only, compose parity ({capsc.get('add')})")
    # env
    e = get_env(dep, "DATABASE_URL")
    check(e and "valueFrom" in e and "value" not in e, "A: DATABASE_URL via secretKeyRef (no plaintext in manifest)")
    e = get_env(dep, "REDIS_URL")
    check(e and e.get("value") == "redis://silo-redis:6379", f"A: REDIS_URL ({e and e.get('value')})")
    e = get_env(dep, "SECRET_KEY")
    check(e and e["valueFrom"]["secretKeyRef"]["name"] == "silo-secrets", "A: SECRET_KEY from existing secret")
    e = get_env(dep, "POSTGRES_TUNE")
    check(e and e.get("value") == "auto", "A: POSTGRES_TUNE auto")
    # args + shm
    pgsts = [s for s in stss if s["metadata"]["name"] == "silo-postgres"][0]
    pc = pgsts["spec"]["template"]["spec"]["containers"][0]
    check("shared_preload_libraries=pg_stat_statements" in pc["args"], "A: pg_stat_statements arg")
    shm = [v for v in pgsts["spec"]["template"]["spec"]["volumes"] if v["name"] == "shm"][0]["emptyDir"]
    check(shm.get("medium") == "Memory" and shm.get("sizeLimit") == "8Gi", f"A: shm tmpfs 8Gi ({shm})")
    # initContainer waits for both
    ic = dep["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    check("silo-postgres 5432" in ic and "silo-redis 6379" in ic, "A: initContainer waits for postgres+redis")
    # probes tcp
    check(c["startupProbe"]["tcpSocket"]["port"] == "http", "A: tcpSocket startup probe")
    # service ports
    svc = [s for s in by_kind(ds, "Service") if s["metadata"]["name"] == "silo"][0]
    ports = {p["name"]: (p["port"], p["targetPort"]) for p in svc["spec"]["ports"]}
    check(ports == {"web": (8090, "http"), "jellyfin": (8096, "jellyfin"), "audiobookshelf": (13378, "audiobookshelf")}, f"A: service ports {ports}")
    mounts = {m["mountPath"]: m["name"] for m in vol_mounts(dep)}
    check(mounts.get("/mnt/media") == "media" and "/mnt/media" in mounts, "A: media at /mnt/media")
    check(mounts.get("/tmp/silo-transcode") == "transcode", "A: transcode mount")
    vs = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in volumes(dep) if "persistentVolumeClaim" in v}
    check(vs.get("media") == "silo-media", f"A: media claim silo-media ({vs.get('media')})")
    check(vs.get("backups") == "silo-backups", "A: backups claim silo-backups")
    check(mounts.get("/var/lib/silo/backups") == "backups", "A: backups mounted at /var/lib/silo/backups")
    check(len(vs) == 8, f"A: 8 PVC-backed volumes ({len(vs)})")
    vct_stability(ds, "A")
    o_data_safety(ds)
    r_test_hook(ds)


STABLE_VCT_KEYS = {"app.kubernetes.io/name", "app.kubernetes.io/instance", "app.kubernetes.io/component"}


def vct_stability(ds, tag):
    """volumeClaimTemplates are immutable in the StatefulSet spec: their
    metadata labels must be version-stable or every chart version bump
    fails the upgrade server-side ("updates to statefulset spec ... forbidden")."""
    for s in by_kind(ds, "StatefulSet"):
        for vct in s["spec"]["volumeClaimTemplates"]:
            bad = set(vct["metadata"].get("labels", {})) - STABLE_VCT_KEYS
            check(not bad, f"{tag}: VCT labels version-stable on {s['metadata']['name']} ({sorted(bad) or 'ok'})")


def b_meili(ds):
    sts_names = [s["metadata"]["name"] for s in by_kind(ds, "StatefulSet")]
    check("silo-meilisearch" in sts_names, "B: meilisearch StatefulSet")
    check(len(by_kind(ds, "Secret")) == 2, "B: postgres-auth + meili-auth secrets")
    msts = [s for s in by_kind(ds, "StatefulSet") if s["metadata"]["name"] == "silo-meilisearch"][0]
    mc = msts["spec"]["template"]["spec"]["containers"][0]
    check(mc["image"] == "getmeili/meilisearch:v1.54.0", "B: meili pinned image")
    msec = mc.get("securityContext", {})
    check(msec.get("allowPrivilegeEscalation") is False and "capabilities" not in msec,
          f"B: meili default cap set, APE false ({msec})")
    envs = {e["name"]: e for e in mc["env"]}
    check("MEILI_MASTER_KEY" in envs and "valueFrom" in envs["MEILI_MASTER_KEY"], "B: MEILI_MASTER_KEY via secretKeyRef")
    check(envs["MEILI_UPGRADE_DB"]["value"] == "false", "B: MEILI_UPGRADE_DB false")
    check(mc["readinessProbe"]["httpGet"]["path"] == "/health", "B: /health probe")
    dep = by_kind(ds, "Deployment")[0]
    extra = [e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "SILO_MEILI_URL"]
    check(len(extra) == 1, "B: extraEnv passthrough")
    vct_stability(ds, "B")


def c_external(ds):
    kinds = [d["kind"] for d in ds]
    check("StatefulSet" not in kinds, "C: no StatefulSets (postgres/redis off)")
    check(len(by_kind(ds, "PersistentVolumeClaim")) == 7, "C: 7 PVCs (media external, backups still chart-managed)")
    dep = by_kind(ds, "Deployment")[0]
    e = get_env(dep, "DATABASE_URL")
    check(e and e.get("value") == "postgres://silo:dbpass@my-cluster:5432/silo?sslmode=disable", "C: databaseUrlOverride used")
    e = get_env(dep, "REDIS_URL")
    check(e and e.get("value") == "redis://my-redis:6379", "C: redisUrlOverride used")
    vs = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in volumes(dep) if "persistentVolumeClaim" in v}
    check(vs.get("media") == "media-nfs", "C: media existingClaim")
    ic = dep["spec"]["template"]["spec"].get("initContainers", [])
    check(not ic, "C: no wait-for-datastores initContainer (no in-cluster stores)")
    check(len(by_kind(ds, "Service")) == 1, "C: only main Service")
    ps = dep["spec"]["template"]["spec"]["securityContext"]
    check(ps.get("runAsUser") == 1000 and ps.get("runAsGroup") == 1000 and ps.get("runAsNonRoot") is True, "C: nonRootMode runAsUser/Group 1000, runAsNonRoot")
    c0 = dep["spec"]["template"]["spec"]["containers"][0]
    check(c0.get("command") == ["silo"], f"C: nonRootMode command [{c0.get('command')}]")


def d_gpu(ds):
    dep = by_kind(ds, "Deployment")[0]
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    check(spec.get("runtimeClassName") == "nvidia", "D: runtimeClassName nvidia")
    res = c.get("resources", {})
    check(res.get("requests", {}).get("nvidia.com/gpu") == 1 and res.get("limits", {}).get("nvidia.com/gpu") == 1, "D: nvidia.com/gpu claim 1")
    caps = c.get("securityContext", {}).get("capabilities", {})
    check(caps.get("drop") == ["ALL"], "D: drop ALL retained (capabilities: [])")
    check("NET_ADMIN" not in caps.get("add", []), "D: no NET_ADMIN (capabilities: [])")
    hp = [v for v in spec["volumes"] if v["name"].startswith("host-proc-")]
    check(len(hp) == 3 and all(v["hostPath"]["path"].startswith("/proc/") for v in hp), "D: 3 /proc hostPath mounts")
    va = [v for v in spec["volumes"] if v["name"] == "vaapi"]
    check(va and va[0]["hostPath"]["path"] == "/dev/dri", "D: /dev/dri hostPath")
    svc = [s for s in by_kind(ds, "Service") if s["metadata"]["name"] == "silo"][0]
    check(svc["spec"]["type"] == "LoadBalancer", "D: service LoadBalancer")


GW_API = "gateway.networking.k8s.io/v1"


def f_gateway_external(ds):
    routes = by_kind(ds, "HTTPRoute")
    check(len(routes) == 3, f"F: 3 HTTPRoutes ({len(routes)})")
    check(not by_kind(ds, "Gateway"), "F: no chart-managed Gateway (external parentRef)")
    names = {r["metadata"]["name"] for r in routes}
    check(names == {"silo", "silo-jellyfin", "silo-audiobookshelf"}, f"F: route names {names}")
    ports = {r["metadata"]["name"]: r["spec"]["rules"][0]["backendRefs"][0]["port"] for r in routes}
    check(ports == {"silo": 8090, "silo-jellyfin": 8096, "silo-audiobookshelf": 13378}, f"F: backend ports {ports}")
    hosts = {r["metadata"]["name"]: r["spec"].get("hostnames") for r in routes}
    check(hosts["silo"] == ["silo.example.com"] and hosts["silo-jellyfin"] == ["jellyfin.example.com"] and hosts["silo-audiobookshelf"] == ["audiobooks.example.com"], f"F: hostnames {hosts}")
    for r in routes:
        pr = r["spec"]["parentRefs"][0]
        ok = pr["name"] == "my-gateway" and pr.get("namespace") == "gateway-system" and pr.get("sectionName") == "https"
        check(ok, f"F: parentRef on {r['metadata']['name']}")
        check(r["apiVersion"] == GW_API, f"F: apiVersion v1 on {r['metadata']['name']}")


def g_gateway_create(ds):
    gws = by_kind(ds, "Gateway")
    check(len(gws) == 1 and gws[0]["metadata"]["name"] == "silo-gateway", "G: chart-managed Gateway silo-gateway")
    check(gws[0]["spec"]["gatewayClassName"] == "envoy", "G: gatewayClassName envoy")
    l = gws[0]["spec"]["listeners"][0]
    check(l["name"] == "http" and l["port"] == 80 and l["protocol"] == "HTTP" and l["allowedRoutes"]["namespaces"]["from"] == "Same", "G: default HTTP :80 listener, same namespace")
    routes = by_kind(ds, "HTTPRoute")
    check(len(routes) == 1 and routes[0]["metadata"]["name"] == "silo", "G: single web HTTPRoute")
    pr = routes[0]["spec"]["parentRefs"][0]
    check(pr["name"] == "silo-gateway" and "namespace" not in pr, "G: HTTPRoute wired to created Gateway")
    br = routes[0]["spec"]["rules"][0]["backendRefs"][0]
    check(br["name"] == "silo" and br["port"] == 8090 and br["weight"] == 1, f"G: backendRef ({br})")
    check(routes[0]["spec"]["hostnames"] == ["silo.example.com"], "G: web hostnames")


def j_dragonfly(ds):
    stss = by_kind(ds, "StatefulSet")
    names = [s["metadata"]["name"] for s in stss]
    check("silo-dragonfly" in names and "silo-redis" not in names, f"J: dragonfly STS, no redis STS ({names})")
    df = [s for s in stss if s["metadata"]["name"] == "silo-dragonfly"][0]
    c = df["spec"]["template"]["spec"]["containers"][0]
    check(c["image"] == "docker.dragonflydb.io/dragonflydb/dragonfly:v2.0.2", f"J: pinned dragonfly image ({c['image']})")
    dsec = c.get("securityContext", {})
    check(dsec.get("allowPrivilegeEscalation") is False and "capabilities" not in dsec,
          f"J: dragonfly default cap set, APE false ({dsec})")
    args = c.get("args", [])
    check("--proactor_threads=1" in args and "--alsologtostderr" in args, f"J: default flags ({args})")
    check(c["readinessProbe"]["tcpSocket"]["port"] == "redis", "J: tcpSocket readiness on 6379")
    svc = [s for s in by_kind(ds, "Service") if s["metadata"]["name"] == "silo-dragonfly"][0]
    check(svc["spec"].get("clusterIP") == "None", "J: headless identity service")
    dep = by_kind(ds, "Deployment")[0]
    e = get_env(dep, "REDIS_URL")
    check(e and e.get("value") == "redis://silo-dragonfly:6379", f"J: REDIS_URL -> dragonfly ({e and e.get('value')})")
    ic = dep["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    check("silo-dragonfly 6379" in ic and "silo-redis" not in ic, "J: initContainer waits for dragonfly")
    vct = df["spec"]["volumeClaimTemplates"]
    check(vct and vct[0]["spec"]["resources"]["requests"]["storage"] == "8Gi", "J: dragonfly data PVC 8Gi")
    vct_stability(ds, "J")


def o_data_safety(ds):
    """All chart-managed silo PVCs must survive `helm uninstall`."""
    keep = [d for d in by_kind(ds, "PersistentVolumeClaim")
            if d["metadata"].get("annotations", {}).get("helm.sh/resource-policy") == "keep"]
    check(len(keep) == 8, f"A: resource-policy keep on all 8 chart PVCs ({len(keep)})")


def r_test_hook(ds):
    """helm test hook Job with smoke checks for silo + enabled datastores."""
    jobs = by_kind(ds, "Job")
    check(len(jobs) == 1, f"A: helm test Job rendered ({len(jobs)})")
    if not jobs:
        return
    job = jobs[0]
    check(job["metadata"]["annotations"].get("helm.sh/hook") == "test", "A: Job has helm.sh/hook: test")
    script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]
    check("silo 8090" in script and "silo-postgres 5432" in script and "silo-redis 6379" in script,
          "A: smoke checks silo web + postgres + redis")


def m_determinism():
    """Go templates iterate maps in random order: unsorted ranges shuffle
    volumeMounts, PVCs and Service ports between renders. Five renders of
    the same values must be byte-identical. (The generated postgres
    password is pinned here: offline it is random per render by design and
    lookup-stable on a real cluster.)"""
    outs = set()
    for _ in range(5):
        p = subprocess.run([HELM, "template", "silo", CHART, "-f", f"{TV}/a-defaults.yaml",
                            "--set", "postgres.auth.password=fixed-for-diff"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            check(False, f"M: render failed: {p.stderr.strip()[:300]}")
            return
        outs.add(p.stdout)
    check(len(outs) == 1, f"M: 5 renders byte-identical ({len(outs)} distinct outputs)")


def n_schema_typo():
    """values.schema.json must reject unknown keys and bad enums with a loud
    error instead of silently no-oping."""
    p = subprocess.run([HELM, "template", "silo", CHART, "-f", f"{TV}/a-defaults.yaml",
                        "--set", "redis.enabeled=true"], capture_output=True, text=True)
    check(p.returncode != 0 and "enabeled" in (p.stderr + p.stdout),
          "N: typo'd value key rejected by schema")
    p = subprocess.run([HELM, "template", "silo", CHART, "-f", f"{TV}/a-defaults.yaml",
                        "--set", "service.type=Oops"], capture_output=True, text=True)
    check(p.returncode != 0, "N: invalid service.type rejected by schema enum")


def p_networkpolicy():
    """Opt-in NetworkPolicies: none by default; when enabled, one
    default-deny (ingress+egress) policy per in-cluster datastore allowing
    only silo + helm-test pods in, with extraIngress appended."""
    ds0 = docs(render(f"{TV}/a-defaults.yaml"))
    check(len(by_kind(ds0, "NetworkPolicy")) == 0, "P: no NetworkPolicies by default")
    p = subprocess.run([HELM, "template", "silo", CHART, "-f", f"{TV}/a-defaults.yaml",
                        "--set", "networkPolicy.enabled=true",
                        "--set", "networkPolicy.extraIngress[0].from[0].podSelector.matchLabels.team=ops",
                        "--set", "networkPolicy.extraIngress[0].ports[0].port=5432"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        check(False, f"P: netpol render failed: {p.stderr.strip()[:300]}")
        return
    pols = by_kind(docs(p), "NetworkPolicy")
    check(len(pols) == 2, f"P: 2 policies with postgres+redis enabled ({len(pols)})")
    pg = [x for x in pols if x["metadata"]["name"] == "silo-postgres"]
    if not pg:
        check(False, "P: postgres NetworkPolicy missing")
        return
    pg = pg[0]
    sel = pg["spec"]["podSelector"]["matchLabels"]
    check(sel.get("app.kubernetes.io/component") == "postgres", "P: policy selects postgres pods")
    check(pg["spec"].get("policyTypes") == ["Ingress", "Egress"], "P: Ingress+Egress policyTypes")
    check(pg["spec"].get("egress") == [], "P: datastore egress denied (deny-all)")
    froms = [f["podSelector"]["matchLabels"].get("app.kubernetes.io/component")
             for f in pg["spec"]["ingress"][0]["from"]]
    check(froms == ["silo", "test"], f"P: ingress from silo + helm-test pods ({froms})")
    ports = [x["port"] for x in pg["spec"]["ingress"][0]["ports"]]
    check(ports == [5432], f"P: postgres port 5432 ({ports})")
    check(len(pg["spec"]["ingress"]) == 2, "P: extraIngress appended as second ingress item")
    rd = [x for x in pols if x["metadata"]["name"] == "silo-redis"][0]
    rports = [x["port"] for x in rd["spec"]["ingress"][0]["ports"]]
    check(rports == [6379], f"P: redis port 6379 ({rports})")


def q_urlkey():
    """postgres.auth.existingSecretUrlKey: DATABASE_URL comes from the
    user's secret via valueFrom — no plaintext URL in the pod spec, no
    chart-managed secrets, and it renders offline (no lookup)."""
    p = subprocess.run([HELM, "template", "silo", CHART, "-f", f"{TV}/a-defaults.yaml",
                        "--set", "postgres.auth.existingSecret=my-pg",
                        "--set", "postgres.auth.existingSecretUrlKey=DATABASE_URL"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        check(False, f"Q: render failed: {p.stderr.strip()[:300]}")
        return
    ds = docs(p)
    dep = by_kind(ds, "Deployment")[0]
    e = get_env(dep, "DATABASE_URL")
    ref = e and e.get("valueFrom", {}).get("secretKeyRef", {})
    check(ref.get("name") == "my-pg" and ref.get("key") == "DATABASE_URL",
          f"Q: DATABASE_URL via valueFrom from user secret ({ref})")
    check(len(by_kind(ds, "Secret")) == 0, "Q: no chart-managed secrets when existingSecret set")


def main():
    run_scenario("A", f"{TV}/a-defaults.yaml", a_default)
    run_scenario("B", f"{TV}/b-meili.yaml", b_meili)
    run_scenario("C", f"{TV}/c-external.yaml", c_external)
    run_scenario("D", f"{TV}/d-gpu.yaml", d_gpu)
    run_scenario("F", f"{TV}/f-gateway-external.yaml", f_gateway_external)
    run_scenario("G", f"{TV}/g-gateway-create.yaml", g_gateway_create)
    run_scenario("J", f"{TV}/j-dragonfly.yaml", j_dragonfly)
    print("\n=== Scenario E (expected failure) ===")
    p = render(f"{TV}/e-existing-pg-secret.yaml")
    check(p.returncode != 0 and "existingSecret" in p.stderr and "not found" in p.stderr,
          "E: render fails loudly when existingSecret can't be looked up offline")
    print("\n=== Scenario H (expected failure) ===")
    p = render(f"{TV}/h-gateway-broken.yaml")
    check(p.returncode != 0 and "parentRefs" in p.stderr and "gateway.create" in p.stderr,
          "H: render fails when gateway.enabled without parentRefs/create")
    print("\n=== Scenario I (expected failure) ===")
    p = render(f"{TV}/i-nonroot-broken.yaml")
    check(p.returncode != 0 and "nonRootMode" in p.stderr and "databaseUrlOverride" in p.stderr,
          "I: render fails when nonRootMode without databaseUrlOverride")
    print("\n=== Scenario K (expected failure) ===")
    p = render(f"{TV}/k-both-backends.yaml")
    check(p.returncode != 0 and "mutually exclusive" in p.stderr,
          "K: render fails when redis and dragonfly are both enabled")

    m_determinism()
    n_schema_typo()
    p_networkpolicy()
    q_urlkey()

    print("\n================ SUMMARY ================")
    if failures:
        print(f"{len(failures)} FAILURES:")
        for f in failures:
            print(f" - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
