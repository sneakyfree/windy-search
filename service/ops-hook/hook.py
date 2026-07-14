"""windy ops-hook — the doctor that is NOT in the patient (ADR-060 §3.6).

CANONICAL, fleet-generic implementation. Extracted from the windy-mind
donor (windy-mind #61) and fully env-parameterized so every Class-C host
runs the SAME bytes — platforms vendor this file verbatim and guard it
with a drift test (the ecosystem's vendor+drift-guard pattern); per-host
differences live in the systemd unit's env file, never in code.

A tiny, stdlib-only HTTP service that runs on a platform's host as its own
systemd unit — OUTSIDE the compose project it operates on — so "the api is
dead/wedged" is a condition an agent can FIX without SSH. It is the
host-side half of the mutating baseline knobs:

  apply_update  →  POST /hook/redeploy   (health-gated rebuild, last-known-
                                          good image rollback, optional
                                          expected-sha attestation)
  set_setting   →  POST /hook/config     (ALLOWLISTED key edit of the host
                                          env file, atomic + backed up,
                                          recreate, health-gate, auto-
                                          restore on a failed gate)
  restart_app   →  POST /hook/restart    (compose restart + health-gate)
  reconnect.<s> →  POST /hook/restart-service {service}  (multi-service hosts:
                                          restart ONE allowlisted sibling
                                          service, gated on compose's own
                                          service state — the aggregator's
                                          read view informs which to pick)

Configuration (systemd unit env file, 0600 — see deploy/ templates):

  OPS_HOOK_TOKEN            REQUIRED — bearer credential; refuses to boot without.
  OPS_HOOK_PRODUCT          e.g. "windy-search" (reporting name).
  OPS_HOOK_WORKDIR          cwd for compose commands, e.g. /opt/windy-search/deploy-prod.
  OPS_HOOK_COMPOSE_CMD      full compose prefix, exactly as the platform's
                            SUBSTRATE documents it, e.g.
                            "docker compose -p windysearch-prod --env-file .env.production".
  OPS_HOOK_SERVICE          compose service to operate on, e.g. "search-api".
  OPS_HOOK_IMAGE_REF        image ref compose builds/pulls — VERIFY ON HOST
                            (`docker compose ... images`), e.g. "windy-search-api:local".
  OPS_HOOK_BUILD_MODE       "build" (default; rebuild in place from the host
                            tree) or "pull" (fetch a new registry image, for
                            ghcr-published services). Match how prod deploys.
  OPS_HOOK_ENV_FILE         absolute path of the env file /hook/config edits.
  OPS_HOOK_CONFIG_ALLOWLIST comma-separated settable keys (provider keys +
                            LOG_LEVEL class ONLY — never the hook token,
                            DATABASE_URL/REDIS_URL, or signing secrets).
  OPS_HOOK_SERVICES         comma-separated sibling compose services this hook
                            may restart individually (multi-service hosts like
                            Chat). Empty → /hook/restart-service disabled. The
                            per-service reconnect knob; distinct from SERVICE
                            (the single build/config target).
  OPS_HOOK_PATIENT_URL      e.g. http://127.0.0.1:8500 (must serve /health, /version).
  OPS_HOOK_MIGRATE_CMD      optional, e.g. "alembic upgrade head" — run via
                            `compose exec -T <service> ...` after up; empty = skip.
  OPS_HOOK_BIND / _PORT     loopback bind (default 127.0.0.1:8901); unique
                            port per platform on consolidated hosts.
  OPS_HOOK_GATE_ATTEMPTS / _INTERVAL   health-gate polling (default 30 × 2s).

Security model (all three layers required): loopback bind (TLS exposure only
via the host proxy at /hook/*); constant-time bearer; and a mechanical
always_confirm — every mutation needs a single-use 60s nonce from
POST /hook/confirm. One operation at a time (409 while busy). Verdicts are
{passed, stages, duration_ms} — `passed`, never top-level `ok` (reserved by
the ADR-060 invoke envelope).

No third-party imports: the doctor must not share a dependency graph (or a
venv, or a container) with any patient.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK_VERSION = "2.1.0"

# ── Config (the systemd unit's env file, NOT the patient's) ───────────
PRODUCT = os.environ.get("OPS_HOOK_PRODUCT", "windy-unknown")
WORKDIR = os.environ.get("OPS_HOOK_WORKDIR", "/opt")
COMPOSE_CMD = shlex.split(
    os.environ.get("OPS_HOOK_COMPOSE_CMD", "docker compose")
)
SERVICE = os.environ.get("OPS_HOOK_SERVICE", "api")
# How redeploy refreshes the image: "build" rebuilds in place from the
# host tree (Mind/Search/admin/Clone — locally-built `:local` images);
# "pull" fetches a new registry image (ghcr-published services). Set per
# host to match how prod actually deploys.
BUILD_MODE = os.environ.get("OPS_HOOK_BUILD_MODE", "build").strip().lower()
IMAGE_REF = os.environ.get("OPS_HOOK_IMAGE_REF", "")
LAST_GOOD_REF = (IMAGE_REF.rsplit(":", 1)[0] + ":last-good") if IMAGE_REF else ""
ENV_FILE = os.environ.get("OPS_HOOK_ENV_FILE", "")
CONFIG_KEY_ALLOWLIST = frozenset(
    k.strip()
    for k in os.environ.get("OPS_HOOK_CONFIG_ALLOWLIST", "").split(",")
    if k.strip()
)
# Multi-service hosts (Chat's ~13 services) — the sibling compose services
# this hook may restart individually. Empty → /hook/restart-service disabled.
# SERVICE (above) is still the single build/config target; this is the
# broader set for the per-service reconnect knob.
SERVICES_ALLOWLIST = frozenset(
    s.strip()
    for s in os.environ.get("OPS_HOOK_SERVICES", "").split(",")
    if s.strip()
)
PATIENT_URL = os.environ.get("OPS_HOOK_PATIENT_URL", "http://127.0.0.1:8080")
MIGRATE_CMD = shlex.split(os.environ.get("OPS_HOOK_MIGRATE_CMD", ""))
BIND_HOST = os.environ.get("OPS_HOOK_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("OPS_HOOK_PORT", "8901"))

HEALTH_GATE_ATTEMPTS = int(os.environ.get("OPS_HOOK_GATE_ATTEMPTS", "30"))
HEALTH_GATE_INTERVAL = float(os.environ.get("OPS_HOOK_GATE_INTERVAL", "2.0"))
NONCE_TTL = 60.0

AUTH_REMEDIATION = (
    "Send `Authorization: Bearer <token>` matching the hook's OPS_HOOK_TOKEN "
    "(held in the ops-hook unit env, 0600 — NOT the patient's env file)."
)
CONFIRM_REMEDIATION = (
    "This is an always_confirm operation: first POST /hook/confirm to get a "
    "single-use nonce (valid 60s), then retry with {\"nonce\": \"<nonce>\"}."
)


def default_runner(cmd: list[str], timeout: float = 600.0) -> tuple[int, str]:
    """Run a command; return (returncode, tail-of-output). Output is kept
    to a short tail for the journal and never included in responses."""
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=WORKDIR
    )
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    return proc.returncode, tail


def default_http_get(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (loopback)
        return r.status, r.read()


class OpsHook:
    """All behavior, dependency-injected for tests: `runner` executes
    commands, `http_get` probes the patient, `now` supplies time."""

    def __init__(self, runner=default_runner, http_get=default_http_get, now=time.monotonic):
        self.runner = runner
        self.http_get = http_get
        self.now = now
        self._nonces: dict[str, float] = {}
        self._op_lock = threading.Lock()

    # ── auth + confirm ────────────────────────────────────────────────
    def check_token(self, header: str | None) -> bool:
        expected = os.environ.get("OPS_HOOK_TOKEN", "")
        if not expected or not header or not header.lower().startswith("bearer "):
            return False
        presented = header[7:].strip()
        return hmac.compare_digest(
            hashlib.sha256(presented.encode()).digest(),
            hashlib.sha256(expected.encode()).digest(),
        )

    def issue_nonce(self) -> dict:
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = self.now() + NONCE_TTL
        for n, exp in list(self._nonces.items()):
            if exp < self.now():
                del self._nonces[n]
        return {"nonce": nonce, "expires_in": int(NONCE_TTL)}

    def consume_nonce(self, nonce: str | None) -> bool:
        if not nonce:
            return False
        expiry = self._nonces.pop(nonce, None)
        return expiry is not None and expiry >= self.now()

    # ── patient probes ────────────────────────────────────────────────
    def patient_health(self) -> dict:
        try:
            status, _ = self.http_get(f"{PATIENT_URL}/health")
            return {"reachable": True, "http": status}
        except Exception as e:
            return {"reachable": False, "error": type(e).__name__}

    def _health_gate(self, expected_sha: str | None = None) -> tuple[bool, str]:
        """Poll the patient until /health answers 200; optionally verify
        /version's commit_sha (attestation that the intended build serves)."""
        for _ in range(HEALTH_GATE_ATTEMPTS):
            try:
                status, _ = self.http_get(f"{PATIENT_URL}/health")
                if status == 200:
                    if expected_sha:
                        vstatus, vbody = self.http_get(f"{PATIENT_URL}/version")
                        if vstatus != 200:
                            return False, "version_unreachable"
                        served = json.loads(vbody).get("commit_sha") or ""
                        if not served.startswith(expected_sha):
                            return False, "sha_mismatch"
                    return True, "healthy"
            except Exception:
                pass
            time.sleep(HEALTH_GATE_INTERVAL)
        return False, "health_gate_timeout"

    # ── compose helper ────────────────────────────────────────────────
    def _compose(self, *args: str) -> list[str]:
        return [*COMPOSE_CMD, *args]

    # ── operations ────────────────────────────────────────────────────
    def op_restart(self) -> dict:
        stages = []
        rc, _ = self.runner(self._compose("restart", SERVICE))
        stages.append({"name": "restart", "ok": rc == 0})
        if rc == 0:
            ok, detail = self._health_gate()
            stages.append({"name": "health_gate", "ok": ok, "detail": detail})
        return self._verdict(stages)

    def _service_up_gate(self, service: str) -> tuple[bool, str]:
        """Poll `compose ps <service>` until it reports up. Uses compose's own
        state (no HTTP/auth dependency) — the right gate for a sibling service
        on a shared network that the host process can't reach directly."""
        for _ in range(HEALTH_GATE_ATTEMPTS):
            rc, out = self.runner(self._compose("ps", service))
            low = out.lower()
            if rc == 0 and ("exit" not in low and "restarting" not in low) \
                    and ("running" in low or " up " in low or "up " in low):
                return True, "running"
            time.sleep(HEALTH_GATE_INTERVAL)
        return False, "service_gate_timeout"

    def op_restart_service(self, service: str) -> dict:
        """Restart ONE named sibling service (multi-service hosts). This is
        the per-service reconnect knob the aggregator's read view informs —
        an agent sees `media: down`, restarts just media, leaves the other
        twelve alone. Allowlisted; gated on compose's own service state."""
        if not SERVICES_ALLOWLIST:
            return self._verdict([{
                "name": "config",
                "ok": False,
                "detail": "OPS_HOOK_SERVICES unset — per-service restart disabled on this host",
            }])
        if service not in SERVICES_ALLOWLIST:
            return self._verdict([{
                "name": "allowlist",
                "ok": False,
                "detail": f"service not restartable; allowed: {sorted(SERVICES_ALLOWLIST)}",
            }])
        stages = [{"name": "allowlist", "ok": True}]
        rc, _ = self.runner(self._compose("restart", service))
        stages.append({"name": "restart", "ok": rc == 0, "detail": service})
        if rc == 0:
            ok, detail = self._service_up_gate(service)
            stages.append({"name": "service_gate", "ok": ok, "detail": detail})
        return self._verdict(stages)

    def op_redeploy(self, expected_sha: str | None) -> dict:
        """Rebuild the patient from the tree at WORKDIR, health-gate it, and
        roll back to the last-known-good image on a failed gate.

        Rebuild-in-place semantics: heals wedged containers and applies
        already-synced code/env. Pulling fresh code onto the host needs a
        host git credential and is a separate, Grant-gated decision."""
        stages = []
        if not IMAGE_REF:
            return self._verdict([{
                "name": "config",
                "ok": False,
                "detail": "OPS_HOOK_IMAGE_REF unset — verify on host and set it in the unit env",
            }])

        rc, _ = self.runner(["docker", "tag", IMAGE_REF, LAST_GOOD_REF])
        stages.append({
            "name": "snapshot_last_good",
            "ok": rc == 0,
            "detail": "current image tagged last-good" if rc == 0 else "tag_failed",
        })
        if rc != 0:
            return self._verdict(stages)

        if BUILD_MODE == "pull":
            rc, _ = self.runner(self._compose("pull", SERVICE))
            stages.append({"name": "pull", "ok": rc == 0})
        else:
            rc, _ = self.runner(self._compose("build", SERVICE))
            stages.append({"name": "build", "ok": rc == 0})
        if rc != 0:
            return self._verdict(stages)

        rc, _ = self.runner(self._compose("up", "-d", "--no-deps", SERVICE))
        stages.append({"name": "up", "ok": rc == 0})

        if rc == 0 and MIGRATE_CMD:
            rc_mig, _ = self.runner(self._compose("exec", "-T", SERVICE, *MIGRATE_CMD))
            stages.append({"name": "migrations", "ok": rc_mig == 0})

        gate_ok, detail = self._health_gate(expected_sha)
        stages.append({"name": "health_gate", "ok": gate_ok, "detail": detail})

        if not gate_ok or rc != 0:
            rb1, _ = self.runner(["docker", "tag", LAST_GOOD_REF, IMAGE_REF])
            rb2, _ = self.runner(
                self._compose("up", "-d", "--force-recreate", "--no-deps", SERVICE)
            )
            rolled = rb1 == 0 and rb2 == 0
            gate2_ok, gate2_detail = self._health_gate() if rolled else (False, "rollback_failed")
            stages.append({
                "name": "rollback_last_good",
                "ok": rolled and gate2_ok,
                "detail": gate2_detail if rolled else "rollback_failed",
            })
        return self._verdict(stages)

    def op_config(self, key: str, value: str) -> dict:
        stages = []
        if not ENV_FILE or not CONFIG_KEY_ALLOWLIST:
            return self._verdict([{
                "name": "config",
                "ok": False,
                "detail": "OPS_HOOK_ENV_FILE / OPS_HOOK_CONFIG_ALLOWLIST unset — config edits disabled on this host",
            }])
        if key not in CONFIG_KEY_ALLOWLIST:
            return self._verdict([{
                "name": "allowlist",
                "ok": False,
                "detail": f"key not settable; allowed: {sorted(CONFIG_KEY_ALLOWLIST)}",
            }])
        if not _valid_env_value(value):
            return self._verdict([{
                "name": "validate",
                "ok": False,
                "detail": "value must be a single printable line under 256 chars",
            }])
        stages.append({"name": "allowlist", "ok": True})

        try:
            _write_env_key(ENV_FILE, key, value)
            stages.append({"name": "write_env", "ok": True, "detail": "atomic write, .prev backup kept"})
        except Exception as e:
            stages.append({"name": "write_env", "ok": False, "detail": type(e).__name__})
            return self._verdict(stages)

        # compose `restart` does NOT re-read env_file — recreate instead
        # (the compose-restart-skips-env_file trap, learned fleet-wide).
        rc, _ = self.runner(self._compose("up", "-d", "--force-recreate", "--no-deps", SERVICE))
        stages.append({"name": "recreate", "ok": rc == 0})

        gate_ok, detail = self._health_gate()
        stages.append({"name": "health_gate", "ok": gate_ok, "detail": detail})

        if not gate_ok or rc != 0:
            restored = _restore_env_backup(ENV_FILE)
            rb, _ = self.runner(self._compose("up", "-d", "--force-recreate", "--no-deps", SERVICE))
            gate2_ok, gate2_detail = self._health_gate() if (restored and rb == 0) else (False, "restore_failed")
            stages.append({
                "name": "restore_env",
                "ok": restored and rb == 0 and gate2_ok,
                "detail": gate2_detail if restored else "no_backup",
            })
        return self._verdict(stages)

    def config_view(self) -> dict:
        """Redacted read: which allowlisted keys are PRESENT (non-empty).
        Values never leave the host."""
        present: dict[str, bool] = {}
        current = _read_env(ENV_FILE) if ENV_FILE else {}
        for key in sorted(CONFIG_KEY_ALLOWLIST):
            present[key] = bool(current.get(key))
        return {"service": f"{PRODUCT}-ops-hook", "settable": present}

    @staticmethod
    def _verdict(stages: list[dict]) -> dict:
        # `passed`, never top-level `ok` — reserved by the invoke envelope.
        return {"passed": all(s["ok"] for s in stages), "stages": stages}


# ── env-file editing (atomic, backed up, injection-proof) ─────────────
def _valid_env_value(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) < 256
        and "\n" not in value
        and "\r" not in value
        and value.isprintable()
    )


def _read_env(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v
    return result


def _write_env_key(path: str, key: str, value: str) -> None:
    """Replace-or-append KEY=value. Atomic (tempfile + rename), previous
    file kept at <path>.prev, mode 0600."""
    lines: list[str] = []
    replaced = False
    if os.path.exists(path):
        shutil.copy2(path, path + ".prev")
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                stripped = line.lstrip()
                if not stripped.startswith("#") and stripped.partition("=")[0].strip() == key:
                    lines.append(f"{key}={value}")
                    replaced = True
                else:
                    lines.append(line)
    if not replaced:
        lines.append(f"{key}={value}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".env.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restore_env_backup(path: str) -> bool:
    backup = path + ".prev"
    if not os.path.exists(backup):
        return False
    os.replace(backup, path)
    os.chmod(path, 0o600)
    return True


# ── HTTP layer ────────────────────────────────────────────────────────
def make_handler(hook: OpsHook):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"windy-ops-hook/{HOOK_VERSION}"

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if hook.check_token(self.headers.get("Authorization")):
                return True
            self._send(401, {
                "ok": False,
                "error": "missing_or_invalid_token",
                "remediation": AUTH_REMEDIATION,
            })
            return False

        def _body(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return {}

        def _confirmed(self, body: dict) -> bool:
            if hook.consume_nonce(body.get("nonce")):
                return True
            self._send(428, {
                "ok": False,
                "error": "confirm_required",
                "remediation": CONFIRM_REMEDIATION,
            })
            return False

        def _run_op(self, fn) -> None:
            if not hook._op_lock.acquire(blocking=False):
                self._send(409, {
                    "ok": False,
                    "error": "operation_in_progress",
                    "remediation": "One mutation at a time — retry after the current operation finishes.",
                })
                return
            try:
                started = time.monotonic()
                verdict = fn()
                verdict["duration_ms"] = int((time.monotonic() - started) * 1000)
                self._send(200, verdict)
            finally:
                hook._op_lock.release()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/hook/health":
                # The doctor's own pulse is auth-free (content-free), like
                # every /health in the fleet; everything else needs the token.
                self._send(200, {
                    "ok": True,
                    "service": f"{PRODUCT}-ops-hook",
                    "version": HOOK_VERSION,
                    "patient": hook.patient_health(),
                    "restartable_services": sorted(SERVICES_ALLOWLIST),
                })
                return
            if not self._authed():
                return
            if self.path == "/hook/config":
                self._send(200, hook.config_view())
                return
            self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authed():
                return
            body = self._body()
            if self.path == "/hook/confirm":
                self._send(200, hook.issue_nonce())
                return
            if self.path == "/hook/restart":
                if self._confirmed(body):
                    self._run_op(hook.op_restart)
                return
            if self.path == "/hook/redeploy":
                if self._confirmed(body):
                    expected = body.get("expected_commit_sha")
                    self._run_op(lambda: hook.op_redeploy(expected))
                return
            if self.path == "/hook/config":
                if self._confirmed(body):
                    key, value = str(body.get("key", "")), str(body.get("value", ""))
                    self._run_op(lambda: hook.op_config(key, value))
                return
            if self.path == "/hook/restart-service":
                if self._confirmed(body):
                    service = str(body.get("service", ""))
                    self._run_op(lambda: hook.op_restart_service(service))
                return
            self._send(404, {"ok": False, "error": "not_found"})

        def log_message(self, fmt: str, *args) -> None:
            # journald gets one line; never request bodies.
            print(f"[ops-hook] {self.address_string()} {self.command} {self.path}")

    return Handler


def main() -> None:
    if not os.environ.get("OPS_HOOK_TOKEN"):
        raise SystemExit(
            "FATAL: OPS_HOOK_TOKEN not set — refusing to serve mutation "
            "endpoints without a credential."
        )
    hook = OpsHook()
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), make_handler(hook))
    print(
        f"[ops-hook] {HOOK_VERSION} {PRODUCT} serving on {BIND_HOST}:{BIND_PORT} "
        f"(workdir={WORKDIR}, service={SERVICE})"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
