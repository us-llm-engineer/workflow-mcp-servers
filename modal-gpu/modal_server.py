#!/usr/bin/env python3
"""
modal-gpu MCP Server
=====================
Wraps Modal (https://modal.com) sandboxes as MCP tools so an agent can spin
up a real cloud GPU (T4 by default) on demand, run arbitrary Python/shell
code on it -- e.g. a quick CNN/ML prototype to sanity-check a startup idea's
technical feasibility -- and tear it down again. Runs over stdio -- register
it in Claude Desktop's MCP config so *Desktop itself* spawns this process
locally, on your machine. It shells out to the `modal` Python SDK, which
talks to Modal's cloud over HTTPS; it does not need `git`/`gh` or any local
GPU -- the GPU lives on Modal's infrastructure, not your machine.

Requires:
    pip install modal
    pip install websocket-client   # only needed for the Jupyter-kernel tools
                                    # (modal_create_jupyter_kernel /
                                    # modal_run_in_kernel / etc.) -- the HTTP
                                    # half of the Jupyter kernel protocol
                                    # uses the stdlib's urllib.request,
                                    # already imported below.
    A Modal account + API token configured one of two ways:
      1. Interactive (recommended for a personal machine):
           modal setup
         (opens a browser, writes ~/.modal.toml)
      2. Non-interactive (service user / CI-style):
           set environment variables MODAL_TOKEN_ID and MODAL_TOKEN_SECRET
         (create a service-user token pair from your Modal workspace's
         Settings -> Tokens page)

Jupyter-kernel tools (modal_create_jupyter_kernel, modal_run_in_kernel,
modal_list_kernels, modal_stop_jupyter_kernel) add PERSISTENT-STATE code
execution on top of the same Sandbox primitive above -- unlike
modal_run_code, which runs each call as a fresh `python -c` process with no
memory of previous calls, a kernel keeps one real Jupyter kernel alive
inside its sandbox, so variables/imports from one modal_run_in_kernel call
are still there on the next one, like cells in a notebook. This follows
Modal's own documented pattern (https://modal.com/docs/examples/jupyter_sandbox):
a Sandbox whose main process IS `jupyter notebook --no-browser ...`,
exposed via Sandbox's own `encrypted_ports` tunnel. There is no official,
widely-used Python client for this exact transport -- `jupyter_client`
itself only speaks raw ZeroMQ to a local kernel, or the separate Kernel
Gateway's own protocol via its GatewayClient, neither of which matches a
plain Jupyter Server reached over an HTTPS/WSS tunnel. Rather than depend
on a low-star third-party wrapper for that gap, the small
`_JupyterKernelClient` class below hand-rolls the two calls this actually
needs directly against Jupyter's own public, documented REST + WebSocket
kernel protocol (POST/DELETE /api/kernels, then
/api/kernels/<id>/channels) -- the same protocol nbclient/papermill/
JupyterLab itself speak -- using only the stdlib's urllib.request (already
used below for the boot health-check) plus `websocket-client`, a
long-established, widely used low-level WebSocket library, so there is no
niche dependency in the execution path. This is deliberately NOT the same
thing as automating Modal's own "Notebooks" product (modal.com/notebooks)
-- that's a separate, private, session-cookie-authenticated internal API
with no public contract; this instead reuses Modal's public Sandbox API
plus Jupyter's own open protocol, so it's built entirely on documented,
stable surfaces.

COST WARNING -- read before use:
    Every GPU sandbox this server creates is REAL, BILLABLE cloud compute
    on your Modal account, charged per second from the moment it starts
    until it's terminated (or its idle_timeout/timeout expires). As of this
    writing Modal lists T4 at roughly $0.000164/sec (~$0.59/hr) -- check
    https://modal.com/pricing for the current rate, GPU pricing changes.
    This is why modal_create_gpu_sandbox and modal_terminate_sandbox both
    require confirm=True: get the user's explicit go-ahead before creating
    a sandbox (state the GPU type and that it bills per-second), and always
    terminate sandboxes you're done with rather than letting them idle out.
    modal_list_sandboxes only shows sandboxes THIS server process created
    and still has in memory -- if this server restarts, previously-created
    sandboxes are orphaned from its point of view (though Modal keeps
    billing them until their timeout hits). Use modal_list_apps for the
    account-wide Live/Stopped view (same data as https://modal.com/apps),
    and modal_stop_app to kill an orphaned App entirely if a sandbox-level
    modal_terminate_sandbox isn't enough (e.g. the local handle/object_id
    is gone too).
    Every create/list/status response includes Modal's own durable
    `object_id` for exactly this reason -- if a local handle is ever lost
    (restart, bug), modal_reconnect_sandbox(object_id) gets a live
    reference back so it can still be terminated through these tools
    instead of requiring the dashboard.

    modal_terminate_sandbox calls Sandbox.terminate(wait=True) and blocks
    for a confirmed exit code -- Modal's terminate() defaults to
    wait=False, which only dispatches the stop request and returns
    immediately, before the container is actually confirmed stopped. Never
    call the underlying SDK's terminate() without wait=True from this
    server; "no exception" from the fire-and-forget form is not the same
    as "it's gone."

Design: mirrors the git-code / colab-mcp servers already in this toolbox --
structured {ok, ...} dict returns, dangerous/costly operations gated behind
confirm=True, and short, cost-conscious defaults (10 min hard timeout, 2 min
idle timeout) so a forgotten sandbox can't run away and rack up charges.
"""

import fnmatch
import inspect
import json
import os
import re
import secrets as pysecrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp >= 2 renamed FastMCP to MCPServer; same constructor, @tool() decorator and run()
    from mcp.server.mcpserver import MCPServer as FastMCP

try:
    import modal
except ImportError:
    modal = None  # handled at call time so the server still starts and can explain the fix

try:
    import websocket  # websocket-client package -- only the WS half of the
except ImportError:  # Jupyter kernel protocol needs it; HTTP half uses urllib.
    websocket = None  # handled at call time so the server still starts and can explain the fix

SERVER_INSTRUCTIONS = (
    "GPU sandboxes on Modal cost real money per second from creation until "
    "termination -- T4 is roughly $0.59/hr as of this writing, check "
    "https://modal.com/pricing for the live rate. Before calling "
    "modal_create_gpu_sandbox, tell the user which GPU type and the "
    "approximate hourly rate, and get their explicit go-ahead in this turn "
    "-- then pass confirm=True. Prefer the smallest/cheapest GPU that answers "
    "the question (T4 is usually enough for a feasibility smoke test; only "
    "ask for L4/A100/H100 if the user specifically needs more memory or "
    "speed). Always call modal_terminate_sandbox(confirm=True) when a "
    "sandbox is no longer needed rather than letting the idle_timeout do it "
    "-- that's a safety net, not a plan. modal_list_sandboxes only reflects "
    "sandboxes this running server process created; if in doubt whether "
    "something is still billing, tell the user to check "
    "https://modal.com/apps directly, or call modal_list_apps for the same "
    "account-wide Live/Stopped view from inside a tool call. modal_check_auth "
    "first if any call fails with an auth-shaped error. Keep pip_packages "
    "minimal -- installing large ML stacks (torch+cuda, etc.) adds real "
    "minutes to sandbox boot time, which is also billed. To fully stop "
    "something orphaned, prefer modal_terminate_sandbox for a single sandbox "
    "this server created; only reach for modal_stop_app (confirm=True) when "
    "you need to kill an entire App and everything under it, since that's "
    "broader and irreversible. For a persistent-state workflow (variables "
    "surviving across calls, like notebook cells), use "
    "modal_create_jupyter_kernel + modal_run_in_kernel instead of repeated "
    "modal_create_gpu_sandbox + modal_run_code calls -- same billing model "
    "and same confirm=True gate, just with state retained between calls. "
    "Always modal_stop_jupyter_kernel(confirm=True) when done with one, same "
    "urgency as terminating a plain sandbox. For the common resource shapes, "
    "prefer the purpose-built host_* tools over the generic ones: "
    "host_cpu_sandbox(scaling_factor=...) for GPU-free multi-core CPU work "
    "(cores = scaling_factor * 0.125, must resolve to 1.0-16.0 cores or it's "
    "rejected -- e.g. scaling_factor=64 for 8 cores); host_one_low_tier_gpu "
    "for a single cheap GPU (T4/L4/A10G); host_two_t4 for exactly two T4s; "
    "host_one_high_end_gpu for a single A100/L40S/H100/H200/B200. Every "
    "host_* tool always requests exactly the GPU count implied by its name "
    "(1, except host_two_t4 which is always 2) and returns a handle that "
    "already has a Jupyter kernel attached -- it works with modal_run_code/"
    "modal_run_shell for one-off commands AND modal_run_in_kernel/the cell "
    "tools for persistent state, so there's no need to also call "
    "modal_create_gpu_sandbox or modal_create_jupyter_kernel for the same "
    "resource shape. Fall back to modal_create_jupyter_kernel directly only "
    "for something a host_* tool doesn't cover (e.g. a multi-GPU non-T4 "
    "request like 'A100:4'). To HOST INFRASTRUCTURE (a docker-compose stack: "
    "databases, brokers, query engines, proxies) use host_infra_sandbox -- a "
    "VM-runtime sandbox with dockerd running, because the default gVisor "
    "sandboxes of every other tool cannot run Docker Compose (needs Modal "
    "client >= 1.6.0; GPUs are not available on it). It requires an explicit "
    "memory_mib (Modal's default is 128 MiB) and its confirm=True refusal "
    "message states the estimated hourly cost (about $0.142 per core-hour plus "
    "$0.024 per GiB-hour, a floor because Modal bills the higher of request "
    "and usage): relay that figure and get the user's go-ahead in this turn. "
    "Then modal_upload_dir (secrets, .git, virtualenvs and keys are excluded "
    "by default; never pass include_secrets=True without the user's explicit "
    "approval), modal_run_shell([\"docker\",\"compose\",...], timeout=...) with "
    "a long timeout, modal_docker_status to see containers and memory, "
    "modal_sandbox_tunnels for public URLs, modal_sync_files to fetch results, "
    "and modal_terminate_sandbox(confirm=True) the moment the run is done. "
    "host_cpu_sandbox also accepts memory_mib for non-Docker CPU work."
)

mcp = FastMCP("modal-gpu", instructions=SERVER_INSTRUCTIONS)

MODAL_BIN = shutil.which("modal") or "modal"
CLI_TIMEOUT = 30

DEFAULT_APP_NAME = "startup-idea-testing"
DEFAULT_TIMEOUT = 600       # 10 min hard cap on sandbox lifetime
DEFAULT_IDLE_TIMEOUT = 120  # 2 min with no exec activity -> auto-terminate
EXEC_TIMEOUT = 120          # default per-command exec timeout

VALID_GPUS = {"T4", "L4", "A10G", "A100", "L40S", "H100", "H200", "B200", "NONE"}

# Tiers for the host_* convenience tools below. NOTE: A100 is placed in the
# high-end tier here (alongside L40S/H100/H200/B200) rather than the
# low-tier trio -- Modal's own docs don't publish an official tier split,
# so this is this server's own judgment call, not a documented Modal
# category. Flag it to the user if a different split is wanted.
LOW_TIER_GPUS = {"T4", "L4", "A10G"}
HIGH_END_GPUS = {"A100", "L40S", "H100", "H200", "B200"}

# CPU-core scaling for host_cpu_sandbox(). Modal's own base allocation unit
# for a Function/Sandbox container is 0.125 physical cores (confirmed at
# https://modal.com/docs/guide/resources); scaling_factor is a multiplier
# on that unit. MIN/MAX here are THIS SERVER's own cost-safety guard rails
# (Modal's docs say a maximum is enforced at creation time but don't
# publish the exact number), not a documented Modal platform limit.
CPU_BASE_CORES = 0.125
MIN_HOST_CPU_CORES = 0.125
MAX_HOST_CPU_CORES = 16.0

JUPYTER_PORT = 8888
JUPYTER_BOOT_TIMEOUT = 60  # seconds to wait for the Jupyter server to come up

# --- Infrastructure hosting (VM runtime + Docker) ---------------------------
# Modal's default Sandbox runtime is gVisor, which cannot give Docker the
# virtual network pairs and NAT it needs, so `docker compose` services cannot
# reach each other there. The VM runtime (`runtime="vm"`, Modal client >= 1.6.0)
# runs the Sandbox in its own Linux kernel; Modal's guide for running Docker in
# a Sandbox (https://modal.com/docs/guide/docker-in-sandboxes) starts `dockerd`
# as the entrypoint and runs containers with sandbox.exec(). GPUs are only
# available on the gVisor runtime, so host_infra_sandbox is CPU-only.
MIN_VM_RUNTIME_MODAL_VERSION = (1, 6, 0)
INFRA_BASE_IMAGE = "ubuntu:24.04"
# docker.io + the compose v2 plugin from Ubuntu's own archive; python-is-python3
# so modal_run_code (which execs `python`) works on this image.
INFRA_APT_PACKAGES = ["docker.io", "docker-compose-v2", "curl", "ca-certificates", "git",
                      "python3", "python-is-python3", "rsync"]
INFRA_DEFAULT_CPU = 2.0
INFRA_DEFAULT_MEMORY_MIB = 4096
INFRA_MIN_MEMORY_MIB = 512
# Guard rails of THIS server (Modal enforces its own maximum at creation time
# but does not publish the number): keep an agent from requesting an unbounded
# machine by mistake.
INFRA_MAX_CPU = 16.0
INFRA_MAX_MEMORY_MIB = 32768
INFRA_DEFAULT_TIMEOUT = 3600      # 1 h hard cap; Modal allows up to 24 h
INFRA_MAX_TIMEOUT = 86400
INFRA_DEFAULT_IDLE_TIMEOUT = 900  # exec() activity resets it
INFRA_READY_TIMEOUT = 300         # seconds to wait for dockerd to answer `docker info`
INFRA_MAX_PORTS = 8

# Modal's published Sandbox + Notebooks compute rates (https://modal.com/pricing,
# read 2026-10-03): $0.00003942 per core-second and $0.00000667 per GiB-second.
# Billing is per second on the higher of the request and the actual usage, so the
# estimate below is a floor, not a cap.
CPU_PRICE_PER_CORE_HOUR = 0.00003942 * 3600
MEM_PRICE_PER_GIB_HOUR = 0.00000667 * 3600

# Files that must not leave the machine by default when a directory is uploaded.
DEFAULT_UPLOAD_EXCLUDES = [".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                           ".mypy_cache", ".env", ".env.local", ".tls", "*.pem", "*.key", "*.sqlite"]
DEFAULT_UPLOAD_MAX_MIB = 256

# In-memory registry: short handle -> {"sandbox": Sandbox, "gpu":.., "created": ts, "app_name":..}
_SANDBOXES: dict = {}

# In-memory registry: same handle as its _SANDBOXES entry -> {"kernel": _JupyterKernelClient,
# "tunnel_url":.., "token":.., "created": ts}. Kept separate from _SANDBOXES so the
# existing sandbox-management tools above are untouched and still work unmodified
# on a Jupyter-backed sandbox's handle (e.g. modal_list_sandboxes/modal_sandbox_status
# will show it like any other sandbox); this dict adds the kernel-specific half.
_KERNELS: dict = {}

# In-memory registry: same handle -> ordered list of cell dicts
# {"id":, "cell_type": "code"|"markdown", "source":, "outputs":, "execution_count":}.
# This is OUR OWN bookkeeping of a notebook-shaped document (colab-proxy-mcp's
# add/get/update/delete/move_cell tools track cells the same way) -- it is not
# Jupyter's own Contents API or a saved .ipynb file, just a convenient structure
# for agents that want to build up and rerun a sequence of cells against one
# persistent kernel instead of firing one-off snippets via modal_run_in_kernel.
_NOTEBOOKS: dict = {}


def _require_modal() -> Optional[dict]:
    if modal is None:
        return {
            "ok": False,
            "error": "The `modal` package isn't installed in this Python environment. "
                     "Run: pip install modal",
        }
    return None


def _require_websocket_client() -> Optional[dict]:
    if websocket is None:
        return {
            "ok": False,
            "error": "The `websocket-client` package isn't installed in this Python "
                     "environment. Run: pip install websocket-client",
        }
    return None


def _new_handle() -> str:
    return uuid.uuid4().hex[:8]


def _get_kernel(handle: str):
    entry = _KERNELS.get(handle)
    if not entry:
        return None, {
            "ok": False,
            "error": f"No Jupyter kernel with handle '{handle}' in this server's memory. "
                     f"Call modal_list_kernels to see what's tracked, or "
                     f"modal_create_jupyter_kernel to make a new one.",
        }
    return entry, None


def _get_sandbox(handle: str):
    entry = _SANDBOXES.get(handle)
    if not entry:
        return None, {
            "ok": False,
            "error": f"No sandbox with handle '{handle}' in this server's memory. "
                     f"Call modal_list_sandboxes to see what's tracked, or "
                     f"modal_create_gpu_sandbox to make a new one. (If this server "
                     f"restarted since you created it, the sandbox may still be "
                     f"running and billing on Modal even though it's untracked here "
                     f"-- check https://modal.com/apps.)",
        }
    return entry, None


def _confirm_required(action: str) -> dict:
    return {
        "ok": False,
        "error": f"confirm must be True to {action} -- this creates/destroys billable "
                 f"cloud resources. Get the user's explicit, in-chat confirmation first.",
    }


def _modal_version_tuple() -> tuple:
    """The installed Modal client version as a comparable tuple, e.g. (1, 6, 0).
    Dev/pre-release suffixes are ignored; an unparsable version gives (0,)."""
    raw = getattr(modal, "__version__", "") if modal is not None else ""
    parts = []
    for piece in str(raw).split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts) or (0,)


def _require_vm_runtime() -> Optional[dict]:
    """The VM runtime needs `runtime=` on Sandbox.create, which only exists in
    Modal client >= 1.6.0 (1.5.x has no such argument). Check the actual
    signature as well as the version string so a future rename fails here,
    with an explanation, instead of as an opaque TypeError."""
    err = _require_modal()
    if err:
        return err
    have = _modal_version_tuple()
    try:
        has_runtime = "runtime" in inspect.signature(modal.Sandbox.create).parameters
    except (TypeError, ValueError):
        has_runtime = False
    if have < MIN_VM_RUNTIME_MODAL_VERSION or not has_runtime:
        want = ".".join(str(x) for x in MIN_VM_RUNTIME_MODAL_VERSION)
        return {
            "ok": False,
            "error": f"The installed Modal client ({getattr(modal, '__version__', 'unknown')}) cannot create "
                     f"VM-runtime Sandboxes, which Docker needs (gVisor Sandboxes cannot run Docker Compose). "
                     f"Run: pip install -U 'modal>={want}'",
        }
    return None


def _estimate_hourly_cost(cpu_cores: float, memory_mib: int) -> float:
    """Floor of the hourly cost in USD from Modal's published Sandbox rates.
    Modal bills the higher of the request and actual usage per second, so real
    cost can be higher when the workload bursts above the request."""
    return cpu_cores * CPU_PRICE_PER_CORE_HOUR + (memory_mib / 1024.0) * MEM_PRICE_PER_GIB_HOUR


def _upload_excluded(rel_path: str, patterns: list) -> bool:
    """True when any path component, or the whole relative path, matches one of
    the glob patterns (so `.git` skips the whole tree and `*.key` skips any key
    file at any depth)."""
    parts = rel_path.replace(os.sep, "/").split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def _build_tarball(local_dir: str, patterns: list, max_bytes: int):
    """Pack local_dir into a temporary .tar.gz, skipping excluded paths.
    Returns (tar_path, files, uncompressed_bytes, skipped). The caller deletes
    tar_path. Raises ValueError for a missing directory or an oversized tree,
    before anything leaves the machine."""
    root = os.path.abspath(os.path.expanduser(local_dir))
    if not os.path.isdir(root):
        raise ValueError(f"local_dir '{local_dir}' is not a directory.")
    files = skipped = total = 0
    fd, tar_path = tempfile.mkstemp(suffix=".tar.gz", prefix="modal-upload-")
    os.close(fd)
    try:
        with tarfile.open(tar_path, "w:gz") as tar:
            for dirpath, dirnames, filenames in os.walk(root):
                rel_dir = os.path.relpath(dirpath, root)
                keep = []
                for d in dirnames:
                    rel = d if rel_dir == "." else os.path.join(rel_dir, d)
                    if _upload_excluded(rel, patterns):
                        skipped += 1
                    else:
                        keep.append(d)
                dirnames[:] = keep
                for name in filenames:
                    rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                    full = os.path.join(dirpath, name)
                    if _upload_excluded(rel, patterns) or os.path.islink(full):
                        skipped += 1
                        continue
                    size = os.path.getsize(full)
                    total += size
                    if total > max_bytes:
                        raise ValueError(
                            f"'{local_dir}' exceeds the {max_bytes // (1024 * 1024)} MiB upload limit "
                            f"(after exclusions). Add exclude patterns or raise max_mib.")
                    tar.add(full, arcname=rel, recursive=False)
                    files += 1
    except Exception:
        try:
            os.remove(tar_path)
        except OSError:
            pass
        raise
    return tar_path, files, total, skipped


def _drain(stream) -> str:
    """Fully read a Modal exec stream (iterable of str lines) into one string."""
    try:
        return "".join(stream)
    except Exception as e:
        return f"<error reading stream: {e}>"


class _JupyterKernelClient:
    """Minimal client for a plain Jupyter Server's public, documented REST +
    WebSocket kernel protocol (POST/DELETE /api/kernels, then the
    /api/kernels/<id>/channels websocket) -- replaces the third-party
    `jupyter_kernel_client` package this file used to depend on. There is no
    official high-level Python client for this exact transport: the
    official `jupyter_client` package only speaks raw ZeroMQ to a local
    kernel, or the separate Kernel Gateway protocol, neither of which
    matches a plain `jupyter notebook` server reached over Modal's
    HTTPS/WSS Sandbox tunnel. Built on the stdlib's urllib.request (already
    used above for the boot health-check) and `websocket-client` (a
    long-established, high-star, general-purpose WebSocket library) rather
    than either a niche wrapper or a second niche wrapper -- this is the
    small amount of protocol glue those two foundational libraries don't
    provide on their own. Exposes only the surface this file actually
    uses: start(), execute(), stop()."""

    def __init__(self, server_url: str, token: str):
        self.server_url = server_url.rstrip("/")
        scheme, rest = self.server_url.split("://", 1)
        self.ws_url = ("wss://" if scheme == "https" else "ws://") + rest
        self.token = token
        self.kernel_id = None
        self.ws = None

    def _http(self, method: str, path: str, timeout: int = 30):
        req = urllib.request.Request(
            f"{self.server_url}{path}",
            data=b"{}" if method == "POST" else None,
            method=method,
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode()) if body else None

    def start(self) -> None:
        """Create a new kernel via POST /api/kernels, then open the
        websocket channel to it -- the two steps the old KernelClient did
        internally."""
        info = self._http("POST", "/api/kernels")
        self.kernel_id = info["id"]
        self.ws = websocket.create_connection(
            f"{self.ws_url}/api/kernels/{self.kernel_id}/channels?token={self.token}",
            timeout=30,
        )

    def execute(self, code: str, timeout: int = 300) -> dict:
        """Send one execute_request and block until the kernel reports
        idle, collecting nbformat-shaped outputs in exactly the
        {status, execution_count, outputs} shape the old KernelClient
        returned, so _summarize_kernel_outputs needs no changes."""
        msg_id = uuid.uuid4().hex
        self.ws.send(json.dumps({
            "header": {
                "msg_id": msg_id,
                "username": "modal-gpu-mcp",
                "session": uuid.uuid4().hex,
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }))

        outputs = []
        status = None
        execution_count = None
        got_reply = False
        got_idle = False
        deadline = time.time() + timeout
        self.ws.settimeout(5)
        while time.time() < deadline and not (got_reply and got_idle):
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue  # no message within the poll window -- recheck the deadline
            except websocket.WebSocketConnectionClosedException:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue  # a message belonging to some other request -- ignore
            msg_type = msg.get("header", {}).get("msg_type")
            content = msg.get("content", {})
            if msg_type == "execute_reply":
                status = content.get("status")
                execution_count = content.get("execution_count")
                got_reply = True
            elif msg_type == "stream":
                outputs.append({
                    "output_type": "stream",
                    "name": content.get("name"),
                    "text": content.get("text", ""),
                })
            elif msg_type in ("execute_result", "display_data"):
                outputs.append({"output_type": msg_type, "data": content.get("data", {})})
            elif msg_type == "error":
                outputs.append({
                    "output_type": "error",
                    "ename": content.get("ename"),
                    "evalue": content.get("evalue"),
                    "traceback": content.get("traceback", []),
                })
            elif msg_type == "status" and content.get("execution_state") == "idle":
                got_idle = True

        return {"status": status or "error", "execution_count": execution_count, "outputs": outputs}

    def stop(self) -> None:
        """Close the websocket and delete the kernel via DELETE
        /api/kernels/<id>, mirroring the old KernelClient.stop()."""
        try:
            if self.ws is not None:
                self.ws.close()
        finally:
            self.ws = None
        if self.kernel_id is not None:
            try:
                self._http("DELETE", f"/api/kernels/{self.kernel_id}")
            except Exception:
                pass  # best-effort -- the sandbox termination that follows ends billing regardless
            self.kernel_id = None


def _boot_jupyter_sandbox(gpu_arg: Optional[str], pip_packages: Optional[list],
                           app_name: str, timeout: int, idle_timeout: int,
                           cpu: Optional[float] = None,
                           memory: Optional[int] = None):
    """Create a Sandbox running `jupyter notebook` as its main process,
    wait for it to come up, and connect a _JupyterKernelClient. Shared by
    modal_create_jupyter_kernel, modal_change_kernel_gpu, and every host_*
    convenience tool so the actual boot sequence (documented at
    https://modal.com/docs/examples/jupyter_sandbox) exists in exactly one
    place. cpu is an explicit CPU-core request/limit passed straight to
    Sandbox.create(cpu=...) -- leave it None to fall back to Modal's own
    default (0.125 cores) exactly like every existing caller already does.
    memory is an explicit memory request in MiB passed to Sandbox.create(memory=...)
    -- None keeps Modal's default (128 MiB), which is far too little for most
    real workloads.
    Returns (sandbox, kernel, server_url, token) on success, or
    (None, None, error_dict, None) on failure -- any sandbox created before
    the failure is already terminated by this function, so callers never
    have to clean up a partial boot themselves."""
    token = pysecrets.token_urlsafe(13)
    try:
        app = modal.App.lookup(app_name, create_if_missing=True)
        image = modal.Image.debian_slim(python_version="3.12").pip_install("jupyter~=1.1.0")
        if pip_packages:
            image = image.pip_install(*pip_packages)
        token_secret = modal.Secret.from_dict({"JUPYTER_TOKEN": token})
        sandbox = modal.Sandbox.create(
            "jupyter", "notebook",
            "--no-browser",
            "--allow-root",
            "--ip=0.0.0.0",
            f"--port={JUPYTER_PORT}",
            "--NotebookApp.allow_origin='*'",
            "--NotebookApp.allow_remote_access=1",
            app=app,
            image=image,
            gpu=gpu_arg,
            cpu=cpu,
            memory=memory,
            timeout=timeout,
            idle_timeout=idle_timeout,
            encrypted_ports=[JUPYTER_PORT],
            secrets=[token_secret],
        )
    except Exception as e:
        return None, None, {"ok": False, "error": f"Failed to create Jupyter sandbox: {e}"}, None

    tunnel = sandbox.tunnels()[JUPYTER_PORT]
    server_url = tunnel.url

    def _is_jupyter_up() -> bool:
        try:
            with urllib.request.urlopen(f"{server_url}/api/status?token={token}", timeout=5) as resp:
                if resp.getcode() == 200:
                    return json.loads(resp.read().decode()).get("started", False)
        except Exception:
            return False
        return False

    deadline = time.time() + JUPYTER_BOOT_TIMEOUT
    up = False
    while time.time() < deadline:
        if _is_jupyter_up():
            up = True
            break
        time.sleep(1)
    if not up:
        try:
            sandbox.terminate(wait=True)
        except Exception:
            pass
        return None, None, {
            "ok": False,
            "error": f"Jupyter server didn't come up within {JUPYTER_BOOT_TIMEOUT}s -- "
                     f"the sandbox was terminated automatically rather than left running unused.",
        }, None

    try:
        kernel = _JupyterKernelClient(server_url=server_url, token=token)
        kernel.start()
    except Exception as e:
        try:
            sandbox.terminate(wait=True)
        except Exception:
            pass
        return None, None, {"ok": False, "error": f"Jupyter server came up but connecting a kernel failed: {e}. "
                                                    f"The sandbox was terminated automatically."}, None
    return sandbox, kernel, server_url, token


def _execute_in_kernel(handle: str, code: str) -> dict:
    """Shared implementation behind modal_run_in_kernel and modal_run_cell
    -- kept as a plain function (not calling one @mcp.tool()-decorated
    function from another) so behavior doesn't depend on FastMCP's
    decorator internals."""
    entry, err = _get_kernel(handle)
    if err:
        return err
    try:
        reply = entry["kernel"].execute(code)
    except Exception as e:
        return {"ok": False, "error": f"Execution failed: {e}"}
    return _summarize_kernel_outputs(reply)


def _summarize_kernel_outputs(reply: dict) -> dict:
    """Flatten a _JupyterKernelClient.execute() reply (standard nbformat-style
    `outputs` list: stream/execute_result/display_data/error entries) into
    the same {ok, stdout, stderr, ...} shape the other tools return, so
    callers don't need to know nbformat's output schema."""
    stdout_parts, stderr_parts, result_parts = [], [], []
    error = None
    for out in reply.get("outputs", []):
        otype = out.get("output_type")
        if otype == "stream":
            if out.get("name") == "stderr":
                stderr_parts.append(out.get("text", ""))
            else:
                stdout_parts.append(out.get("text", ""))
        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {})
            if "text/plain" in data:
                result_parts.append(data["text/plain"])
        elif otype == "error":
            error = {
                "ename": out.get("ename"),
                "evalue": out.get("evalue"),
                "traceback": "\n".join(out.get("traceback", [])),
            }
    return {
        "ok": error is None and reply.get("status", "ok") == "ok",
        "status": reply.get("status"),
        "execution_count": reply.get("execution_count"),
        "stdout": "".join(stdout_parts).strip(),
        "stderr": "".join(stderr_parts).strip(),
        "result": "\n".join(result_parts).strip(),
        "error": error,
    }


def _run_modal_cli(args: list, timeout: int = CLI_TIMEOUT) -> dict:
    """Run the `modal` CLI (not the Python SDK -- Modal has no SDK method to
    list or stop apps at the account level, only the CLI/dashboard do this;
    confirmed against modal.com/docs/reference/cli/app). Returns structured
    {ok, returncode, stdout, stderr}, mirroring the git-code server's _run."""
    try:
        proc = subprocess.run(
            [MODAL_BIN, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "`modal` CLI was not found on PATH. Run: pip install modal"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"`modal {' '.join(args)}` timed out after {timeout}s"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


# ---------------------------------------------------------------------------
# Auth / status (always safe, no confirmation needed)
# ---------------------------------------------------------------------------

@mcp.tool()
def modal_check_auth() -> dict:
    """Verify the `modal` package is installed and this machine has working
    Modal credentials (via ~/.modal.toml from `modal setup`, or the
    MODAL_TOKEN_ID/MODAL_TOKEN_SECRET environment variables). Does not create
    any billable resources. Run this first if any other tool fails with an
    auth-shaped error."""
    err = _require_modal()
    if err:
        return err
    token_id = os.environ.get("MODAL_TOKEN_ID")
    token_secret = os.environ.get("MODAL_TOKEN_SECRET")
    env_configured = bool(token_id and token_secret)
    toml_path = os.path.expanduser("~/.modal.toml")
    toml_exists = os.path.exists(toml_path)
    if not env_configured and not toml_exists:
        return {
            "ok": False,
            "error": "No Modal credentials found. Run `modal setup` in a terminal on "
                     "this machine (opens a browser to authenticate), or set the "
                     "MODAL_TOKEN_ID and MODAL_TOKEN_SECRET environment variables "
                     "from a Modal service-user token.",
        }
    try:
        modal.App.lookup(DEFAULT_APP_NAME, create_if_missing=True)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Credentials found but a live call to Modal failed: {e}",
            "env_configured": env_configured,
            "toml_exists": toml_exists,
        }
    return {
        "ok": True,
        "message": "Modal credentials look valid and a live API call succeeded.",
        "env_configured": env_configured,
        "toml_exists": toml_exists,
    }


@mcp.tool()
def modal_list_apps() -> dict:
    """List EVERY App on your Modal account (live, deployed, and recently
    stopped) -- the same account-wide view as the Apps page in the Modal
    dashboard (modal.com/apps), not just what this server process happens
    to remember. Use this to check for orphaned apps still billing after a
    server restart or a past bug, instead of only trusting
    modal_list_sandboxes (which only sees this process's own memory).
    Shells out to `modal app list --json` since Modal's Python SDK has no
    method for this -- only the CLI/dashboard expose account-wide app
    listing."""
    result = _run_modal_cli(["app", "list", "--json"])
    if not result["ok"]:
        return result
    try:
        apps = json.loads(result["stdout"])
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "error": f"`modal app list --json` did not return valid JSON: {e}",
            "raw_stdout": result["stdout"],
        }
    return {"ok": True, "apps": apps}


@mcp.tool()
def modal_stop_app(app_identifier: str, confirm: bool = False) -> dict:
    """Permanently stop an App and terminate all its running containers --
    the same effect as clicking Stop on an app in the Modal dashboard.
    app_identifier is the App ID or name shown by modal_list_apps (or the
    Apps page). REQUIRES confirm=True: this is irreversible and stops
    EVERYTHING under that app, not just one sandbox -- if you only want to
    stop a single sandbox you created via modal_create_gpu_sandbox, prefer
    modal_terminate_sandbox instead so you don't take down other work
    running under the same app name. Shells out to `modal app stop
    APP_IDENTIFIER -y` since this is account-level app management, not
    something the Python SDK exposes."""
    if not confirm:
        return _confirm_required(f"stop app '{app_identifier}' (terminates ALL its containers)")
    return _run_modal_cli(["app", "stop", app_identifier, "-y"])


@mcp.tool()
def modal_list_sandboxes() -> dict:
    """List sandboxes this server process has created and still tracks in
    memory, with their handle, GPU type, age, and whether they're still
    running. Does NOT see sandboxes from a previous server process, or
    anything created outside this server -- for the full picture of what's
    actually billing on your account, check https://modal.com/apps."""
    out = []
    for handle, entry in _SANDBOXES.items():
        sandbox = entry["sandbox"]
        try:
            status = "running" if sandbox.poll() is None else f"exited({sandbox.poll()})"
        except Exception as e:
            status = f"unknown ({e})"
        out.append({
            "handle": handle,
            "object_id": entry.get("object_id"),
            "gpu": entry["gpu"],
            "app_name": entry["app_name"],
            "age_seconds": round(time.time() - entry["created"], 1),
            "status": status,
        })
    return {"ok": True, "sandboxes": out}


@mcp.tool()
def modal_reconnect_sandbox(object_id: str, gpu: str = "unknown", app_name: str = "unknown") -> dict:
    """Reattach to a sandbox this server lost track of -- e.g. after a
    server restart, or one whose handle was dropped by a past bug -- using
    Modal's own durable `object_id` (returned by modal_create_gpu_sandbox /
    modal_list_sandboxes / modal_sandbox_status, shaped like 'sb-...').
    Uses modal.Sandbox.from_id() to get a live reference back, then
    registers it under a new local handle so modal_terminate_sandbox,
    modal_run_code, etc. work on it again. gpu/app_name are just labels for
    display in modal_list_sandboxes -- they aren't verified against Modal,
    so pass what you actually remember (or leave as 'unknown')."""
    err = _require_modal()
    if err:
        return err
    try:
        sandbox = modal.Sandbox.from_id(object_id)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Could not reconnect to object_id '{object_id}': {e}. If it's already "
                     f"terminated, this is expected -- check https://modal.com/apps to confirm.",
        }
    handle = _new_handle()
    _SANDBOXES[handle] = {
        "sandbox": sandbox,
        "object_id": object_id,
        "gpu": gpu,
        "app_name": app_name,
        "created": time.time(),
    }
    return {
        "ok": True,
        "handle": handle,
        "object_id": object_id,
        "message": f"Reconnected -- use handle '{handle}' with modal_terminate_sandbox, "
                   f"modal_run_code, modal_sandbox_status, etc.",
    }


@mcp.tool()
def modal_sandbox_status(handle: str) -> dict:
    """Check whether a specific sandbox (by handle from modal_create_gpu_sandbox
    or modal_list_sandboxes) is still running or has exited."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    sandbox = entry["sandbox"]
    try:
        rc = sandbox.poll()
    except Exception as e:
        return {"ok": False, "error": f"Could not poll sandbox: {e}"}
    return {
        "ok": True,
        "handle": handle,
        "object_id": entry.get("object_id"),
        "running": rc is None,
        "returncode": rc,
        "age_seconds": round(time.time() - entry["created"], 1),
    }


# ---------------------------------------------------------------------------
# Create / destroy (billable -- confirm=True required both ways)
# ---------------------------------------------------------------------------

@mcp.tool()
def modal_create_gpu_sandbox(
    gpu: str = "T4",
    python_version: str = "3.11",
    pip_packages: Optional[list] = None,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
) -> dict:
    """Create a live, billable Modal sandbox with a GPU attached (default
    T4) and a Debian+Python base image. Returns a short `handle` string --
    pass that to modal_run_code / modal_pip_install / modal_terminate_sandbox.
    gpu: one of NONE, T4, L4, A10G, A100, L40S, H100, H200, B200 (append
    ':N' for multiple, e.g. 'A100:2'). pip_packages: extra packages baked
    into the image before boot (keep this minimal -- it adds billed boot
    time). timeout: hard cap in seconds on the sandbox's total lifetime
    (default 600 = 10 min). idle_timeout: auto-terminate after this many
    seconds with no exec() activity (default 120 = 2 min) -- a safety net,
    not a substitute for calling modal_terminate_sandbox when you're done.
    REQUIRES confirm=True -- this starts a real per-second charge the moment
    it succeeds; state the GPU type and approximate cost to the user and get
    their go-ahead in this turn first."""
    err = _require_modal()
    if err:
        return err
    if not confirm:
        return _confirm_required(f"create a {gpu} GPU sandbox (billed per second while it runs)")
    base_gpu = gpu.split(":")[0].upper()
    if base_gpu not in VALID_GPUS:
        return {"ok": False, "error": f"Unknown gpu '{gpu}'. Valid types: {sorted(VALID_GPUS)}"}
    gpu_arg = None if base_gpu == "NONE" else gpu

    try:
        app = modal.App.lookup(app_name, create_if_missing=True)
        image = modal.Image.debian_slim(python_version=python_version)
        if pip_packages:
            image = image.pip_install(*pip_packages)
        sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            gpu=gpu_arg,
            timeout=timeout,
            idle_timeout=idle_timeout,
        )
    except Exception as e:
        return {"ok": False, "error": f"Failed to create sandbox: {e}"}

    handle = _new_handle()
    object_id = sandbox.object_id
    _SANDBOXES[handle] = {
        "sandbox": sandbox,
        "object_id": object_id,
        "gpu": gpu,
        "app_name": app_name,
        "created": time.time(),
    }
    return {
        "ok": True,
        "handle": handle,
        "object_id": object_id,
        "gpu": gpu,
        "timeout": timeout,
        "idle_timeout": idle_timeout,
        "message": f"Sandbox '{handle}' is live and billing now. Call "
                   f"modal_terminate_sandbox('{handle}', confirm=True) when done. "
                   f"object_id '{object_id}' is Modal's own durable ID for this sandbox -- "
                   f"if this server ever loses '{handle}' (restart, bug, etc.), that ID is "
                   f"what lets you reconnect via modal.Sandbox.from_id() and still terminate "
                   f"it instead of dead-ending at the dashboard.",
    }


@mcp.tool()
def modal_terminate_sandbox(handle: str, confirm: bool = False) -> dict:
    """Stop a sandbox and CONFIRM it has actually exited before returning --
    ends its billing immediately. REQUIRES confirm=True. Always call this
    when you're finished with a sandbox rather than relying on its
    timeout/idle_timeout to clean up.

    Blocks on Sandbox.terminate(wait=True): by Modal's own docs, terminate()
    defaults to wait=False, which just dispatches the stop request and
    returns immediately -- "ok" from that call means the request was sent,
    not that the container actually stopped. wait=True blocks until Modal
    confirms the exit and hands back the real exit code, so a returned
    ok=True here means it is actually gone, not just asked-to-leave."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    if not confirm:
        return _confirm_required(f"terminate sandbox '{handle}'")
    try:
        exit_code = entry["sandbox"].terminate(wait=True)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Failed to confirm termination: {e}. The stop request may or may not "
                     f"have been dispatched -- handle '{handle}' (object_id "
                     f"'{entry.get('object_id')}') is kept tracked so you can retry, rather "
                     f"than dropped here on an unconfirmed result.",
        }
    try:
        entry["sandbox"].detach()
    except Exception:
        pass  # best-effort cleanup of the local client connection; termination is already confirmed
    del _SANDBOXES[handle]
    return {
        "ok": True,
        "message": f"Sandbox '{handle}' confirmed exited (exit code {exit_code}).",
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# Run code inside a live sandbox
# ---------------------------------------------------------------------------

@mcp.tool()
def modal_run_code(handle: str, code: str, timeout: int = EXEC_TIMEOUT) -> dict:
    """Run a snippet of Python code inside an existing sandbox (via
    `python -c`) and return its stdout/stderr/returncode. Use this for the
    actual experiment -- e.g. training a tiny model and printing accuracy,
    or a quick numeric feasibility check for a startup idea. The sandbox
    must already exist (modal_create_gpu_sandbox first)."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    try:
        process = entry["sandbox"].exec("python", "-c", code, timeout=timeout)
        stdout = _drain(process.stdout)
        stderr = _drain(process.stderr)
        returncode = process.wait()
    except Exception as e:
        return {"ok": False, "error": f"Execution failed: {e}"}
    return {
        "ok": returncode == 0,
        "returncode": returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }


@mcp.tool()
def modal_run_shell(handle: str, args: list, timeout: int = EXEC_TIMEOUT) -> dict:
    """Run an arbitrary shell command inside an existing sandbox, e.g.
    ["nvidia-smi"] to confirm which GPU is actually attached, or
    ["ls", "-la", "/tmp"]. args is passed as argv (no shell involved, so no
    quoting/escaping surprises) -- for a shell pipeline use
    ["bash", "-c", "your | pipeline"]."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    if not args:
        return {"ok": False, "error": "args must be a non-empty list."}
    try:
        process = entry["sandbox"].exec(*args, timeout=timeout)
        stdout = _drain(process.stdout)
        stderr = _drain(process.stderr)
        returncode = process.wait()
    except Exception as e:
        return {"ok": False, "error": f"Execution failed: {e}"}
    return {
        "ok": returncode == 0,
        "returncode": returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }


@mcp.tool()
def modal_pip_install(handle: str, packages: list, timeout: int = 300) -> dict:
    """Install additional pip packages into an already-running sandbox
    (convenience wrapper around modal_run_shell for `pip install ...`) --
    use this when you discover you need a package you didn't bake into the
    image at modal_create_gpu_sandbox time. Adds real, billed wall-clock
    time proportional to what's being installed."""
    if not packages:
        return {"ok": False, "error": "packages must be a non-empty list."}
    return modal_run_shell(handle, ["pip", "install", "-q", *packages], timeout=timeout)


# ---------------------------------------------------------------------------
# File sync between the local machine and a live sandbox
# ---------------------------------------------------------------------------

@mcp.tool()
def modal_sync_files(handle: str, files: list, direction: str = "download") -> dict:
    """Copy a batch of files between the local machine and a live sandbox.

    files: a list of [remote_file_path, local_file_path] pairs (or 2-tuples).
    direction: "download" (default) copies sandbox -> local for every pair;
    "upload" copies local -> sandbox. The sync for a given pair only takes
    effect once that pair's copy call succeeds -- a bad path or permission
    error on one pair is reported in that pair's own result and does not
    stop the rest of the batch from being attempted. Parent directories are
    created as needed on whichever side is being written to."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    if direction not in ("download", "upload"):
        return {"ok": False, "error": f"direction must be 'download' or 'upload', got {direction!r}."}
    if not files:
        return {"ok": False, "error": "files must be a non-empty list of (remote_file_path, local_file_path) pairs."}

    sandbox = entry["sandbox"]
    results = []
    for item in files:
        try:
            remote_path, local_path = item
        except (TypeError, ValueError):
            results.append({
                "ok": False,
                "remote_file_path": None,
                "local_file_path": None,
                "error": f"Malformed entry {item!r}; expected a (remote_file_path, local_file_path) pair.",
            })
            continue
        try:
            if direction == "download":
                sandbox.filesystem.copy_to_local(remote_path, local_path)
            else:
                sandbox.filesystem.copy_from_local(local_path, remote_path)
            results.append({"ok": True, "remote_file_path": remote_path, "local_file_path": local_path})
        except Exception as e:
            results.append({
                "ok": False,
                "remote_file_path": remote_path,
                "local_file_path": local_path,
                "error": f"{type(e).__name__}: {e}",
            })

    failed = sum(1 for r in results if not r["ok"])
    return {
        "ok": failed == 0,
        "direction": direction,
        "synced": len(results) - failed,
        "failed": failed,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Jupyter kernels -- persistent-state execution (billable -- confirm=True
# required for create/stop, same as the plain sandbox tools above)
# ---------------------------------------------------------------------------

@mcp.tool()
def modal_create_jupyter_kernel(
    gpu: str = "T4",
    pip_packages: Optional[list] = None,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
) -> dict:
    """Create a live, billable Modal sandbox whose main process is a real
    Jupyter server (following Modal's own documented pattern at
    https://modal.com/docs/examples/jupyter_sandbox), then connect a kernel
    to it. Returns a short `handle` -- pass that to modal_run_in_kernel /
    modal_stop_jupyter_kernel. Unlike modal_create_gpu_sandbox +
    modal_run_code (fresh process per call, no memory between calls), code
    run via modal_run_in_kernel on this handle shares ONE persistent kernel,
    so variables/imports/state carry over from call to call, like cells in
    a notebook. gpu/pip_packages/timeout/idle_timeout/app_name mean the same
    as in modal_create_gpu_sandbox. REQUIRES confirm=True -- same billing
    model as any other GPU sandbox; state the GPU type and approximate cost
    to the user and get their go-ahead in this turn first. If the Jupyter
    server fails to come up within the boot window, the sandbox is
    terminated automatically rather than left running unused."""
    err = _require_modal() or _require_websocket_client()
    if err:
        return err
    if not confirm:
        return _confirm_required(f"create a {gpu} GPU Jupyter kernel sandbox (billed per second while it runs)")
    base_gpu = gpu.split(":")[0].upper()
    if base_gpu not in VALID_GPUS:
        return {"ok": False, "error": f"Unknown gpu '{gpu}'. Valid types: {sorted(VALID_GPUS)}"}
    gpu_arg = None if base_gpu == "NONE" else gpu

    sandbox, kernel, server_url, token = _boot_jupyter_sandbox(gpu_arg, pip_packages, app_name, timeout, idle_timeout)
    if sandbox is None:
        return server_url  # this is the error dict in the failure case

    handle = _new_handle()
    object_id = sandbox.object_id
    _SANDBOXES[handle] = {
        "sandbox": sandbox,
        "object_id": object_id,
        "gpu": gpu,
        "app_name": app_name,
        "created": time.time(),
    }
    _KERNELS[handle] = {
        "kernel": kernel,
        "server_url": server_url,
        "token": token,
        "created": time.time(),
    }
    return {
        "ok": True,
        "handle": handle,
        "object_id": object_id,
        "gpu": gpu,
        "timeout": timeout,
        "idle_timeout": idle_timeout,
        "message": f"Jupyter kernel '{handle}' is live and billing now. Use modal_run_in_kernel "
                   f"for persistent-state execution, and modal_stop_jupyter_kernel('{handle}', "
                   f"confirm=True) when done. The server URL and token are sensitive (they grant "
                   f"full code-execution access to this sandbox) -- don't paste them anywhere "
                   f"outside this tool's own bookkeeping.",
    }


@mcp.tool()
def modal_run_in_kernel(handle: str, code: str) -> dict:
    """Run a snippet of Python code in an existing Jupyter kernel (from
    modal_create_jupyter_kernel) and return its outputs. Unlike
    modal_run_code, this kernel is PERSISTENT -- variables, imports, and
    other state from earlier modal_run_in_kernel calls on the same handle
    are still available, exactly like re-running cells in one notebook.
    There's no separate per-call timeout here; a runaway cell keeps running
    (and billing) until the sandbox's own timeout/idle_timeout hits, or
    until modal_stop_jupyter_kernel is called -- keep test code bounded."""
    return _execute_in_kernel(handle, code)


@mcp.tool()
def modal_list_kernels() -> dict:
    """List Jupyter kernels this server process has created and still
    tracks in memory, with their handle and age. Does NOT show the
    underlying sandbox's GPU/status -- cross-reference the same handle in
    modal_list_sandboxes for that (a Jupyter kernel's sandbox is tracked
    there too, since it's a sandbox like any other)."""
    out = []
    for handle, entry in _KERNELS.items():
        out.append({
            "handle": handle,
            "age_seconds": round(time.time() - entry["created"], 1),
        })
    return {"ok": True, "kernels": out}


@mcp.tool()
def modal_stop_jupyter_kernel(handle: str, confirm: bool = False) -> dict:
    """Stop a Jupyter kernel AND terminate its underlying sandbox, ending
    billing immediately -- the Jupyter-kernel equivalent of
    modal_terminate_sandbox, but also cleans up the local kernel client
    connection first. REQUIRES confirm=True. Always call this when done
    with a kernel rather than relying on its idle_timeout."""
    kernel_entry, kerr = _get_kernel(handle)
    if kerr:
        return kerr
    sandbox_entry, serr = _get_sandbox(handle)
    if serr:
        return serr
    if not confirm:
        return _confirm_required(f"stop Jupyter kernel '{handle}' (terminates its sandbox)")
    try:
        kernel_entry["kernel"].stop()
    except Exception:
        pass  # best-effort; the sandbox termination below is what actually stops billing
    try:
        exit_code = sandbox_entry["sandbox"].terminate(wait=True)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Failed to confirm sandbox termination: {e}. Handle '{handle}' (object_id "
                     f"'{sandbox_entry.get('object_id')}') is kept tracked so you can retry.",
        }
    try:
        sandbox_entry["sandbox"].detach()
    except Exception:
        pass
    del _KERNELS[handle]
    del _SANDBOXES[handle]
    return {
        "ok": True,
        "message": f"Jupyter kernel '{handle}' stopped and its sandbox confirmed exited "
                   f"(exit code {exit_code}).",
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# Notebook-style cell management on top of a Jupyter kernel -- parity with
# colab-proxy-mcp's add/get/update/delete/move/run_code_cell + change_runtime.
# See the _NOTEBOOKS comment above for what this bookkeeping is (and isn't).
# ---------------------------------------------------------------------------

def _get_notebook(handle: str):
    """Validate `handle` is a live Jupyter kernel and return its cell list,
    creating an empty one on first use."""
    entry, err = _get_kernel(handle)
    if err:
        return None, err
    return _NOTEBOOKS.setdefault(handle, []), None


def _new_cell_id() -> str:
    return uuid.uuid4().hex[:8]


def _find_cell(cells: list, cell_id: str) -> int:
    for i, c in enumerate(cells):
        if c["id"] == cell_id:
            return i
    return -1


@mcp.tool()
def modal_add_code_cell(handle: str, code: str = "", cell_index: int = 0) -> dict:
    """Add a new code cell to the in-memory notebook tracked for this
    Jupyter kernel handle -- the modal-gpu analog of colab-proxy-mcp's
    add_code_cell. This only STORES the cell; it does not execute it --
    call modal_run_cell afterward (or use modal_run_in_kernel directly for
    a one-off snippet with no cell bookkeeping). cell_index is the position
    to insert at (0 = beginning, matching colab-proxy-mcp's own default);
    pass a large number to append at the end."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    cell_id = _new_cell_id()
    idx = max(0, min(cell_index, len(cells)))
    cells.insert(idx, {"id": cell_id, "cell_type": "code", "source": code,
                        "outputs": None, "execution_count": None})
    return {"ok": True, "cell_id": cell_id, "cell_index": idx}


@mcp.tool()
def modal_add_text_cell(handle: str, content: str = "", cell_index: int = -1) -> dict:
    """Add a new markdown/text cell (not executable) to the in-memory
    notebook tracked for this Jupyter kernel handle -- the modal-gpu analog
    of colab-proxy-mcp's add_text_cell. cell_index=-1 (default, matching
    colab-proxy-mcp) appends at the end; any non-negative value inserts at
    that position instead."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    cell_id = _new_cell_id()
    idx = len(cells) if cell_index < 0 else max(0, min(cell_index, len(cells)))
    cells.insert(idx, {"id": cell_id, "cell_type": "markdown", "source": content,
                        "outputs": None, "execution_count": None})
    return {"ok": True, "cell_id": cell_id, "cell_index": idx}


@mcp.tool()
def modal_get_cells(handle: str) -> dict:
    """List all cells (id, type, source, and last-run outputs if any) in
    the in-memory notebook tracked for this Jupyter kernel handle -- the
    modal-gpu analog of colab-proxy-mcp's get_cells."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    return {"ok": True, "cells": cells}


@mcp.tool()
def modal_run_cell(handle: str, cell_id: str) -> dict:
    """Execute one stored code cell's current source against the
    persistent Jupyter kernel and record the result on the cell -- the
    modal-gpu analog of colab-proxy-mcp's run_code_cell. Markdown cells
    can't be run; use modal_get_cells to read their content instead. Same
    billing/runaway-cell caveats as modal_run_in_kernel apply."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    idx = _find_cell(cells, cell_id)
    if idx < 0:
        return {"ok": False, "error": f"No cell '{cell_id}' in kernel '{handle}'. "
                                       f"Call modal_get_cells to see what's there."}
    cell = cells[idx]
    if cell["cell_type"] != "code":
        return {"ok": False, "error": f"Cell '{cell_id}' is a {cell['cell_type']} cell, not code -- nothing to run."}
    result = _execute_in_kernel(handle, cell["source"])
    cell["outputs"] = result
    cell["execution_count"] = result.get("execution_count")
    return result


@mcp.tool()
def modal_update_cell(handle: str, cell_id: str, content: str = "") -> dict:
    """Update a stored cell's source/content (code or markdown) -- the
    modal-gpu analog of colab-proxy-mcp's update_cell. Does not re-run a
    code cell automatically; call modal_run_cell afterward if you want the
    new code executed."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    idx = _find_cell(cells, cell_id)
    if idx < 0:
        return {"ok": False, "error": f"No cell '{cell_id}' in kernel '{handle}'."}
    cells[idx]["source"] = content
    return {"ok": True, "cell_id": cell_id}


@mcp.tool()
def modal_delete_cell(handle: str, cell_id: str) -> dict:
    """Delete a stored cell -- the modal-gpu analog of colab-proxy-mcp's
    delete_cell. Only removes it from the tracked notebook; has no effect
    on variables the kernel already picked up from having run it before
    (there's no 'undo a past execution' in a live kernel)."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    idx = _find_cell(cells, cell_id)
    if idx < 0:
        return {"ok": False, "error": f"No cell '{cell_id}' in kernel '{handle}'."}
    cells.pop(idx)
    return {"ok": True, "message": f"Cell '{cell_id}' deleted."}


@mcp.tool()
def modal_move_cell(handle: str, cell_id: str, cell_index: int = 0) -> dict:
    """Move a stored cell to a new position -- the modal-gpu analog of
    colab-proxy-mcp's move_cell. Purely reorders the tracked notebook list;
    has no effect on kernel state."""
    cells, err = _get_notebook(handle)
    if err:
        return err
    idx = _find_cell(cells, cell_id)
    if idx < 0:
        return {"ok": False, "error": f"No cell '{cell_id}' in kernel '{handle}'."}
    cell = cells.pop(idx)
    new_idx = max(0, min(cell_index, len(cells)))
    cells.insert(new_idx, cell)
    return {"ok": True, "cell_id": cell_id, "cell_index": new_idx}


@mcp.tool()
def modal_change_kernel_gpu(
    handle: str,
    gpu: str = "T4",
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    confirm: bool = False,
) -> dict:
    """Change the GPU accelerator for an existing Jupyter kernel -- the
    modal-gpu analog of colab-proxy-mcp's change_runtime. Modal sandboxes
    can't hot-swap hardware on a live container, so under the hood this
    TERMINATES the current sandbox and boots a fresh one with the requested
    GPU, reconnecting a new kernel under the SAME handle so existing
    references (and any cells from modal_add_code_cell/add_text_cell) keep
    working. This matches what changing a Colab runtime does too: variables
    and imports in the old kernel are lost -- a hardware change is a reset
    in both products. Stored cells' CODE survives and is not automatically
    re-run; call modal_run_cell on each afterward if you want them
    re-executed on the new hardware. REQUIRES confirm=True -- this ends
    billing on the current GPU and starts billing on the new one; tell the
    user both GPU types and get their go-ahead in this turn first."""
    kernel_entry, kerr = _get_kernel(handle)
    if kerr:
        return kerr
    sandbox_entry, serr = _get_sandbox(handle)
    if serr:
        return serr
    if not confirm:
        return _confirm_required(
            f"change kernel '{handle}' from GPU '{sandbox_entry['gpu']}' to '{gpu}' "
            f"(terminates the current sandbox, boots a new one)"
        )
    err = _require_modal() or _require_websocket_client()
    if err:
        return err
    base_gpu = gpu.split(":")[0].upper()
    if base_gpu not in VALID_GPUS:
        return {"ok": False, "error": f"Unknown gpu '{gpu}'. Valid types: {sorted(VALID_GPUS)}"}
    gpu_arg = None if base_gpu == "NONE" else gpu
    app_name = sandbox_entry["app_name"]

    try:
        kernel_entry["kernel"].stop()
    except Exception:
        pass
    try:
        sandbox_entry["sandbox"].terminate(wait=True)
    except Exception as e:
        return {"ok": False, "error": f"Failed to terminate the old sandbox before switching GPU: {e}. "
                                       f"Handle '{handle}' is left as-is -- retry once this clears."}
    try:
        sandbox_entry["sandbox"].detach()
    except Exception:
        pass

    new_sandbox, new_kernel, server_url, new_token = _boot_jupyter_sandbox(gpu_arg, None, app_name, timeout, idle_timeout)
    if new_sandbox is None:
        # The old sandbox is already gone -- don't leave the handle pointing at a dead
        # sandbox silently. Cells (if any) are preserved under _NOTEBOOKS so nothing
        # about the user's code is lost, but this handle needs a fresh
        # modal_create_jupyter_kernel to become usable again.
        del _SANDBOXES[handle]
        del _KERNELS[handle]
        return {
            "ok": False,
            "error": f"Old sandbox terminated, but booting the replacement with gpu='{gpu}' failed: "
                     f"{server_url.get('error')}. Handle '{handle}' is now invalid for "
                     f"sandbox/kernel tools -- its cells are still in modal_get_cells('{handle}') "
                     f"though. Call modal_create_jupyter_kernel to start over.",
        }

    _SANDBOXES[handle] = {
        "sandbox": new_sandbox,
        "object_id": new_sandbox.object_id,
        "gpu": gpu,
        "app_name": app_name,
        "created": time.time(),
    }
    _KERNELS[handle] = {
        "kernel": new_kernel,
        "server_url": server_url,
        "token": new_token,
        "created": time.time(),
    }
    return {
        "ok": True,
        "handle": handle,
        "object_id": new_sandbox.object_id,
        "gpu": gpu,
        "message": f"Kernel '{handle}' is now running on '{gpu}'. All prior variables/imports "
                   f"were lost (new kernel, same as a Colab runtime change) -- stored cells "
                   f"survived and can be re-run with modal_run_cell.",
    }


def _register_jupyter_sandbox(sandbox, kernel, server_url: str, token: str, gpu_label: str,
                               app_name: str, timeout: int, idle_timeout: int) -> dict:
    """Register a freshly booted Jupyter sandbox+kernel under a new handle
    and build the standard success response -- shared by every host_*
    creation tool below so the bookkeeping (and the resulting handle's
    shape) lives in exactly one place. gpu_label is stored in the same
    `_SANDBOXES[...]["gpu"]` field modal_list_sandboxes/modal_sandbox_status
    already display -- for a CPU-only sandbox this holds a
    "NONE (cpu=N cores)"-style label instead of an actual GPU name, so
    those existing tools show the resource info with no changes of their
    own."""
    handle = _new_handle()
    object_id = sandbox.object_id
    _SANDBOXES[handle] = {
        "sandbox": sandbox,
        "object_id": object_id,
        "gpu": gpu_label,
        "app_name": app_name,
        "created": time.time(),
    }
    _KERNELS[handle] = {
        "kernel": kernel,
        "server_url": server_url,
        "token": token,
        "created": time.time(),
    }
    return {
        "ok": True,
        "handle": handle,
        "object_id": object_id,
        "gpu": gpu_label,
        "timeout": timeout,
        "idle_timeout": idle_timeout,
        "message": f"Sandbox '{handle}' is live and billing now. Use modal_run_code/"
                   f"modal_run_shell for one-off commands, modal_run_in_kernel for "
                   f"persistent-state execution, and modal_stop_jupyter_kernel('{handle}', "
                   f"confirm=True) when done. The server URL and token are sensitive (they "
                   f"grant full code-execution access to this sandbox) -- don't paste them "
                   f"anywhere outside this tool's own bookkeeping.",
    }


# ---------------------------------------------------------------------------
# Infrastructure hosting: a VM-runtime Sandbox running dockerd. Use this to
# host a docker-compose stack (databases, brokers, query engines, proxies);
# the default gVisor Sandboxes used by every other tool here cannot run it.
# ---------------------------------------------------------------------------

_APT_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")


def _exec_capture(sandbox, args: list, timeout: int) -> dict:
    """Run argv in the sandbox and return {returncode, stdout, stderr}."""
    process = sandbox.exec(*args, timeout=timeout)
    stdout = _drain(process.stdout)
    stderr = _drain(process.stderr)
    returncode = process.wait()
    return {"returncode": returncode, "stdout": stdout.strip(), "stderr": stderr.strip()}


@mcp.tool()
def host_infra_sandbox(
    cpu_cores: float = INFRA_DEFAULT_CPU,
    memory_mib: int = INFRA_DEFAULT_MEMORY_MIB,
    apt_packages: Optional[list] = None,
    encrypted_ports: Optional[list] = None,
    timeout: int = INFRA_DEFAULT_TIMEOUT,
    idle_timeout: int = INFRA_DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
) -> dict:
    """Create a live, billable, GPU-FREE Modal VM sandbox that runs Docker
    (dockerd as the main process, `docker` and `docker compose` installed),
    for hosting infrastructure such as a compose stack. Why a VM: the default
    gVisor sandboxes cannot give Docker container networking, so compose
    services could not reach each other; Modal's VM runtime has its own Linux
    kernel and can (https://modal.com/docs/guide/docker-in-sandboxes).
    Requires Modal client >= 1.6.0 (checked; the error says how to upgrade).
    cpu_cores: 0.125-16 (default 2). memory_mib: 512-32768 (default 4096) --
    memory is requested explicitly because Modal's default is only 128 MiB.
    apt_packages: extra Ubuntu packages baked into the image (adds billed
    boot time). encrypted_ports: TCP ports to expose over TLS tunnels (at most
    8); the returned `tunnels` map has the public URL for each -- tunnel
    support on the VM runtime is not documented by Modal, so the response
    reports honestly if a tunnel could not be read. timeout: hard lifetime cap
    in seconds (default 3600, max 86400); idle_timeout resets on exec()
    activity (default 900) and is a safety net, not a substitute for
    modal_terminate_sandbox. GPUs are not available on this runtime.
    Typical flow: host_infra_sandbox -> modal_upload_dir (the stack's files)
    -> modal_run_shell(["docker","compose",...], timeout=...) ->
    modal_docker_status -> collect results with modal_sync_files ->
    modal_terminate_sandbox. Running dockerd inside a Sandbox makes disk and
    image pulls part of the billed time, so pull once and keep the sandbox
    for the whole test run instead of recreating it.
    REQUIRES confirm=True. Cost: about $0.142 per core-hour plus $0.024 per
    GiB-hour at Modal's published Sandbox rates (billed per second on the
    higher of request and usage, so this is a floor): the refusal message
    states the estimate for the exact shape requested -- relay it to the
    user and get their go-ahead in this turn first."""
    err = _require_vm_runtime()
    if err:
        return err
    try:
        cpu_cores = float(cpu_cores)
        memory_mib = int(memory_mib)
        timeout = int(timeout)
        idle_timeout = int(idle_timeout)
    except (TypeError, ValueError):
        return {"ok": False, "error": "cpu_cores must be a number and memory_mib, timeout, idle_timeout integers."}
    if not (MIN_HOST_CPU_CORES <= cpu_cores <= INFRA_MAX_CPU):
        return {"ok": False, "error": f"cpu_cores={cpu_cores:g} is outside {MIN_HOST_CPU_CORES:g}-{INFRA_MAX_CPU:g}."}
    if not (INFRA_MIN_MEMORY_MIB <= memory_mib <= INFRA_MAX_MEMORY_MIB):
        return {"ok": False, "error": f"memory_mib={memory_mib} is outside "
                                      f"{INFRA_MIN_MEMORY_MIB}-{INFRA_MAX_MEMORY_MIB}."}
    if not (60 <= timeout <= INFRA_MAX_TIMEOUT):
        return {"ok": False, "error": f"timeout={timeout} is outside 60-{INFRA_MAX_TIMEOUT} seconds."}
    if idle_timeout < 30:
        return {"ok": False, "error": "idle_timeout must be at least 30 seconds."}
    ports = list(encrypted_ports or [])
    if (len(ports) > INFRA_MAX_PORTS or len(set(ports)) != len(ports)
            or any(not isinstance(p, int) or isinstance(p, bool) or not (1 <= p <= 65535) for p in ports)):
        return {"ok": False, "error": f"encrypted_ports must be at most {INFRA_MAX_PORTS} distinct integers in 1-65535."}
    extra = list(apt_packages or [])
    bad = [p for p in extra if not isinstance(p, str) or not _APT_NAME.match(p)]
    if bad:
        return {"ok": False, "error": f"Invalid apt package name(s): {bad}. Use plain Ubuntu package names."}

    hourly = _estimate_hourly_cost(cpu_cores, memory_mib)
    if not confirm:
        return _confirm_required(
            f"create a {cpu_cores:g}-core, {memory_mib} MiB VM sandbox running Docker (about "
            f"${hourly:.2f}/hour floor at Modal's published rates, up to "
            f"${hourly * timeout / 3600:.2f} if it runs to its {timeout} s timeout)")

    try:
        app = modal.App.lookup(app_name, create_if_missing=True)
        image = (modal.Image.from_registry(INFRA_BASE_IMAGE)
                 .env({"DEBIAN_FRONTEND": "noninteractive"})
                 .apt_install(*INFRA_APT_PACKAGES, *extra))
        sandbox = modal.Sandbox.create(
            "dockerd",
            app=app,
            image=image,
            runtime="vm",
            cpu=cpu_cores,
            memory=memory_mib,
            timeout=timeout,
            idle_timeout=idle_timeout,
            encrypted_ports=ports,
            readiness_probe=modal.Probe.with_exec("docker", "info", interval_ms=500),
        )
    except Exception as e:
        return {"ok": False, "error": f"Failed to create the VM sandbox: {e}"}

    try:
        sandbox.wait_until_ready(timeout=INFRA_READY_TIMEOUT)
    except Exception as e:
        try:
            sandbox.terminate(wait=True)
        except Exception:
            pass
        return {"ok": False, "error": f"dockerd did not become ready within {INFRA_READY_TIMEOUT}s ({e}). "
                                      f"The sandbox was terminated automatically rather than left billing."}

    versions = {}
    try:
        for key, argv in (("docker", ["docker", "version", "--format", "{{.Server.Version}}"]),
                          ("compose", ["docker", "compose", "version", "--short"])):
            r = _exec_capture(sandbox, argv, 60)
            versions[key] = r["stdout"] if r["returncode"] == 0 else f"unavailable: {r['stderr'][:120]}"
    except Exception as e:
        versions["error"] = f"{type(e).__name__}: {e}"

    tunnels = {}
    if ports:
        try:
            for port, tun in sandbox.tunnels().items():
                tunnels[int(port)] = getattr(tun, "url", None)
        except Exception as e:
            tunnels = {"error": f"could not read tunnels: {type(e).__name__}: {e}"}

    handle = _new_handle()
    object_id = sandbox.object_id
    _SANDBOXES[handle] = {
        "sandbox": sandbox,
        "object_id": object_id,
        "gpu": f"NONE (vm runtime, cpu={cpu_cores:g} cores, memory={memory_mib} MiB, docker)",
        "app_name": app_name,
        "created": time.time(),
    }
    return {
        "ok": True,
        "handle": handle,
        "object_id": object_id,
        "runtime": "vm",
        "cpu_cores": cpu_cores,
        "memory_mib": memory_mib,
        "versions": versions,
        "tunnels": tunnels,
        "estimated_hourly_cost_usd": round(hourly, 4),
        "timeout": timeout,
        "idle_timeout": idle_timeout,
        "message": f"VM sandbox '{handle}' is live and billing now (about ${hourly:.2f}/hour floor). "
                   f"Docker is ready. Call modal_terminate_sandbox('{handle}', confirm=True) when done; "
                   f"object_id '{object_id}' lets you reconnect and terminate it if this server loses the handle.",
    }


@mcp.tool()
def modal_upload_dir(
    handle: str,
    local_dir: str,
    remote_dir: str,
    exclude: Optional[list] = None,
    include_secrets: bool = False,
    max_mib: int = DEFAULT_UPLOAD_MAX_MIB,
    timeout: int = 300,
) -> dict:
    """Upload a whole local directory (for example a compose project) into a
    live sandbox as one compressed archive, then extract it at remote_dir.
    Far fewer calls than modal_sync_files for a tree of files.
    Safe by default: paths matching `.git`, `.venv`, `node_modules`,
    `__pycache__`, caches, `.env`, `.env.local`, `.tls`, `*.pem`, `*.key` and
    `*.sqlite` are NOT uploaded, symlinks are skipped, and the tree is refused
    if it exceeds max_mib (default 256) after exclusions -- all checked before
    anything leaves the machine. exclude: extra glob patterns (matched against
    every path component and the whole relative path). include_secrets=True
    additionally uploads the secret-like files (.env, .env.local, .tls, *.pem,
    *.key) -- only with the user's explicit approval, since the sandbox is a
    remote machine; generated secrets are better created inside the sandbox
    (for example by running the project's own secrets script there)."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    if not remote_dir or not remote_dir.startswith("/"):
        return {"ok": False, "error": "remote_dir must be an absolute path inside the sandbox."}
    patterns = list(DEFAULT_UPLOAD_EXCLUDES) + list(exclude or [])
    if include_secrets:
        secret_like = {".env", ".env.local", ".tls", "*.pem", "*.key"}
        patterns = [p for p in patterns if p not in secret_like]
    try:
        tar_path, files, total, skipped = _build_tarball(local_dir, patterns, int(max_mib) * 1024 * 1024)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    remote_tar = f"/tmp/upload-{_new_handle()}.tar.gz"
    sandbox = entry["sandbox"]
    try:
        sandbox.filesystem.copy_from_local(tar_path, remote_tar)
        q_dir, q_tar = shlex.quote(remote_dir), shlex.quote(remote_tar)
        r = _exec_capture(sandbox, ["bash", "-c",
                                    f"mkdir -p {q_dir} && tar -xzf {q_tar} -C {q_dir} && rm -f {q_tar}"],
                          timeout)
    except Exception as e:
        return {"ok": False, "error": f"Upload failed: {type(e).__name__}: {e}"}
    finally:
        try:
            os.remove(tar_path)
        except OSError:
            pass
    if r["returncode"] != 0:
        return {"ok": False, "error": f"Extraction failed: {r['stderr'][:300]}"}
    return {
        "ok": True,
        "remote_dir": remote_dir,
        "files": files,
        "uncompressed_bytes": total,
        "skipped_paths": skipped,
        "excluded_patterns": patterns,
        "secrets_included": include_secrets,
    }


@mcp.tool()
def modal_docker_status(handle: str, timeout: int = 120) -> dict:
    """One-call health picture of a Docker host sandbox (host_infra_sandbox):
    every container with its state and health (`docker ps -a`), live memory
    and CPU per running container (`docker stats --no-stream`), and the
    machine's memory and disk. Use it between compose steps to see what is up,
    what exited, and how close each service is to its memory limit before
    deciding to raise the sandbox's memory or terminate it."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    sandbox = entry["sandbox"]

    def jsonl(text: str) -> list:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        return rows

    try:
        ps = _exec_capture(sandbox, ["docker", "ps", "-a", "--format", "{{json .}}"], timeout)
        if ps["returncode"] != 0:
            return {"ok": False, "error": f"docker ps failed (is this a host_infra_sandbox?): {ps['stderr'][:300]}"}
        stats = _exec_capture(sandbox, ["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout)
        mem = _exec_capture(sandbox, ["free", "-m"], 30)
        disk = _exec_capture(sandbox, ["df", "-h", "/", "/var/lib/docker"], 30)
    except Exception as e:
        return {"ok": False, "error": f"Status failed: {type(e).__name__}: {e}"}
    containers = [{"name": c.get("Names"), "image": c.get("Image"), "status": c.get("Status"),
                   "state": c.get("State")} for c in jsonl(ps["stdout"])]
    usage = [{"name": s.get("Name"), "mem": s.get("MemUsage"), "mem_percent": s.get("MemPerc"),
              "cpu_percent": s.get("CPUPerc")} for s in jsonl(stats["stdout"])]
    return {
        "ok": True,
        "containers": containers,
        "usage": usage,
        "machine_memory": mem["stdout"],
        "disk": disk["stdout"],
    }


@mcp.tool()
def modal_sandbox_tunnels(handle: str, timeout: int = 50) -> dict:
    """List the public tunnel URLs of a sandbox's exposed ports (created with
    encrypted_ports): {port: url}. The URLs are reachable by anyone who has
    them while the sandbox runs, so treat them as sensitive."""
    entry, err = _get_sandbox(handle)
    if err:
        return err
    try:
        tunnels = entry["sandbox"].tunnels(timeout=int(timeout))
    except Exception as e:
        return {"ok": False, "error": f"Could not read tunnels: {type(e).__name__}: {e}"}
    return {"ok": True, "tunnels": {int(p): getattr(t, "url", None) for p, t in tunnels.items()}}


# ---------------------------------------------------------------------------
# host_* tools -- purpose-built entry points for common resource shapes, so
# an agent can pick the right one by name alone instead of juggling gpu/cpu
# parameters on one generic tool. Each one boots a Jupyter-capable sandbox
# via the SAME _boot_jupyter_sandbox() used by modal_create_jupyter_kernel,
# so every handle they return works with modal_run_code, modal_run_shell,
# modal_run_in_kernel, the cell-management tools, and
# modal_stop_jupyter_kernel exactly like a handle from
# modal_create_jupyter_kernel would -- these are thin, constrained wrappers,
# not a new execution model. modal_create_gpu_sandbox and
# modal_create_jupyter_kernel remain available unchanged for anything these
# presets don't cover (e.g. an exotic "A100:4" multi-GPU request).
# ---------------------------------------------------------------------------

@mcp.tool()
def host_cpu_sandbox(
    scaling_factor: float,
    pip_packages: Optional[list] = None,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
    memory_mib: Optional[int] = None,
) -> dict:
    """Create a live, billable, GPU-FREE Modal sandbox with a flexible
    number of CPU cores, and connect a Jupyter kernel to it -- for
    high-end multi-core CPU work (data prep, numeric code, anything that
    doesn't need a GPU). CPU cores are computed as
    cores = scaling_factor * 0.125 (Modal's own base allocation unit) --
    e.g. scaling_factor=1 -> 0.125 cores (Modal's own default), 
    scaling_factor=8 -> 1.0 core, scaling_factor=64 -> 8.0 cores,
    scaling_factor=128 -> 16.0 cores. Must resolve to between 0.125 and
    16.0 cores inclusive (i.e. scaling_factor between 1.0 and 128);
    anything outside that range is REJECTED with an error rather than
    clamped, so pass a scaling_factor you actually mean. The returned handle works
    with modal_run_code/modal_run_shell for one-off commands AND
    modal_run_in_kernel/the cell tools for persistent-state execution --
    both hit the same sandbox. REQUIRES confirm=True -- Modal's Sandbox &
    Notebooks CPU rate is roughly $0.14/core/hr as of this writing (check
    https://modal.com/pricing for the live rate); state the resulting
    core count and approximate hourly cost to the user and get their
    go-ahead in this turn first.
    memory_mib: optional memory request in MiB (Modal's default is only 128
    MiB; pass e.g. 4096 for a 4 GiB workload). Memory is billed at roughly
    $0.024 per GiB-hour on top of the CPU rate, and the cost estimate in the
    confirm message includes it. Leave None to keep Modal's default.
    This preset runs on the default (gVisor) runtime and CANNOT run Docker:
    use host_infra_sandbox for Docker or Compose workloads."""
    err = _require_modal() or _require_websocket_client()
    if err:
        return err
    if memory_mib is not None and not (128 <= int(memory_mib) <= INFRA_MAX_MEMORY_MIB):
        return {"ok": False, "error": f"memory_mib={memory_mib} is outside the allowed "
                                      f"128-{INFRA_MAX_MEMORY_MIB} MiB range."}
    cpu_cores = scaling_factor * CPU_BASE_CORES
    if cpu_cores < MIN_HOST_CPU_CORES or cpu_cores > MAX_HOST_CPU_CORES:
        return {
            "ok": False,
            "error": f"scaling_factor={scaling_factor} resolves to {cpu_cores:g} CPU cores "
                     f"(scaling_factor * {CPU_BASE_CORES}), which is outside the allowed "
                     f"{MIN_HOST_CPU_CORES:g}-{MAX_HOST_CPU_CORES:g} core range. Pick a "
                     f"scaling_factor between {MIN_HOST_CPU_CORES / CPU_BASE_CORES:g} and "
                     f"{MAX_HOST_CPU_CORES / CPU_BASE_CORES:g}.",
        }
    est_mem = int(memory_mib) if memory_mib is not None else 128  # Modal's default request
    if not confirm:
        return _confirm_required(
            f"create a {cpu_cores:g}-core, {est_mem} MiB CPU sandbox (about "
            f"${_estimate_hourly_cost(cpu_cores, est_mem):.2f}/hour at Modal's published rates, "
            f"billed per second while it runs)")

    sandbox, kernel, server_url, token = _boot_jupyter_sandbox(
        None, pip_packages, app_name, timeout, idle_timeout, cpu=cpu_cores,
        memory=int(memory_mib) if memory_mib is not None else None,
    )
    if sandbox is None:
        return server_url  # error dict in the failure case

    mem_label = f", memory={int(memory_mib)} MiB" if memory_mib is not None else ""
    return _register_jupyter_sandbox(
        sandbox, kernel, server_url, token,
        gpu_label=f"NONE (cpu={cpu_cores:g} cores{mem_label})",
        app_name=app_name, timeout=timeout, idle_timeout=idle_timeout,
    )


@mcp.tool()
def host_one_low_tier_gpu(
    gpu: str = "T4",
    pip_packages: Optional[list] = None,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
) -> dict:
    """Create a live, billable Modal sandbox with exactly ONE entry-level
    GPU (T4, L4, or A10G) and connect a Jupyter kernel to it -- the
    cheapest tier, right for a feasibility smoke test or anything that
    doesn't need serious VRAM/throughput. Always exactly 1 GPU -- pass
    the bare GPU name with no ':N' count suffix (use host_two_t4 if you
    specifically need 2 GPUs). The returned handle works with
    modal_run_code/modal_run_shell for one-off commands AND
    modal_run_in_kernel/the cell tools for persistent-state execution.
    REQUIRES confirm=True -- T4 is roughly $0.59/hr, L4/A10G somewhat
    more, check https://modal.com/pricing for live rates; state the GPU
    type and approximate cost to the user and get their go-ahead in this
    turn first."""
    err = _require_modal() or _require_websocket_client()
    if err:
        return err
    base_gpu = gpu.split(":")[0].upper()
    if base_gpu not in LOW_TIER_GPUS:
        return {
            "ok": False,
            "error": f"'{gpu}' isn't a low-tier GPU. Valid types here: "
                     f"{sorted(LOW_TIER_GPUS)} (exactly 1 GPU only -- use "
                     f"host_one_high_end_gpu for L40S/H100/H200/B200/A100, or "
                     f"host_two_t4 for two T4s).",
        }
    if not confirm:
        return _confirm_required(f"create a 1x{base_gpu} GPU sandbox (billed per second while it runs)")

    sandbox, kernel, server_url, token = _boot_jupyter_sandbox(base_gpu, pip_packages, app_name, timeout, idle_timeout)
    if sandbox is None:
        return server_url

    return _register_jupyter_sandbox(
        sandbox, kernel, server_url, token,
        gpu_label=base_gpu, app_name=app_name, timeout=timeout, idle_timeout=idle_timeout,
    )


@mcp.tool()
def host_two_t4(
    pip_packages: Optional[list] = None,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
) -> dict:
    """Create a live, billable Modal sandbox with exactly TWO T4 GPUs and
    connect a Jupyter kernel to it -- for workloads that specifically
    benefit from multi-GPU (e.g. data-parallel training) without paying
    for a single higher-end card. The returned handle works with
    modal_run_code/modal_run_shell for one-off commands AND
    modal_run_in_kernel/the cell tools for persistent-state execution.
    REQUIRES confirm=True -- roughly double a single T4's ~$0.59/hr, check
    https://modal.com/pricing for the live rate; state the approximate
    cost to the user and get their go-ahead in this turn first."""
    err = _require_modal() or _require_websocket_client()
    if err:
        return err
    if not confirm:
        return _confirm_required("create a 2xT4 GPU sandbox (billed per second while it runs)")

    sandbox, kernel, server_url, token = _boot_jupyter_sandbox("T4:2", pip_packages, app_name, timeout, idle_timeout)
    if sandbox is None:
        return server_url

    return _register_jupyter_sandbox(
        sandbox, kernel, server_url, token,
        gpu_label="T4:2", app_name=app_name, timeout=timeout, idle_timeout=idle_timeout,
    )


@mcp.tool()
def host_one_high_end_gpu(
    gpu: str = "L40S",
    pip_packages: Optional[list] = None,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    confirm: bool = False,
) -> dict:
    """Create a live, billable Modal sandbox with exactly ONE high-end GPU
    (A100, L40S, H100, H200, or B200) and connect a Jupyter kernel to it --
    for workloads that genuinely need the extra VRAM/throughput (larger
    models, bigger batches). Always exactly 1 GPU -- pass the bare GPU
    name with no ':N' count suffix. The returned handle works with
    modal_run_code/modal_run_shell for one-off commands AND
    modal_run_in_kernel/the cell tools for persistent-state execution.
    REQUIRES confirm=True -- these are the most expensive tier by a wide
    margin (H100/H200/B200 in particular), check
    https://modal.com/pricing for live rates; state the GPU type and
    approximate cost to the user and get their explicit go-ahead in this
    turn first -- don't default to the biggest card without a reason."""
    err = _require_modal() or _require_websocket_client()
    if err:
        return err
    base_gpu = gpu.split(":")[0].upper()
    if base_gpu not in HIGH_END_GPUS:
        return {
            "ok": False,
            "error": f"'{gpu}' isn't a high-end GPU. Valid types here: "
                     f"{sorted(HIGH_END_GPUS)} (exactly 1 GPU only -- use "
                     f"host_one_low_tier_gpu for T4/L4/A10G).",
        }
    if not confirm:
        return _confirm_required(f"create a 1x{base_gpu} GPU sandbox (billed per second while it runs)")

    sandbox, kernel, server_url, token = _boot_jupyter_sandbox(base_gpu, pip_packages, app_name, timeout, idle_timeout)
    if sandbox is None:
        return server_url

    return _register_jupyter_sandbox(
        sandbox, kernel, server_url, token,
        gpu_label=base_gpu, app_name=app_name, timeout=timeout, idle_timeout=idle_timeout,
    )


if __name__ == "__main__":
    mcp.run()
