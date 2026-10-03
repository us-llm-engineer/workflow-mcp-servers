# modal-gpu

A single-file (`modal_server.py`, about 2,100 lines) STDIO MCP server that turns [Modal](https://modal.com) sandboxes into agent tools: create a CPU or GPU machine on demand, run Python or shell on it, keep a persistent Jupyter kernel alive between calls, move files in and out, and tear it down. It exposes 28 tools and is built on `mcp.server.fastmcp.FastMCP` and the `modal` SDK.

**It costs real money.** Every sandbox bills per second from creation until termination (a T4 is roughly $0.59/hour; check [modal.com/pricing](https://modal.com/pricing)). The design is built around that fact; see "Cost safety" below.

## Requirements

```bash
pip install 'mcp>=1.2' 'modal>=1.6.0' websocket-client   # websocket-client only for the Jupyter kernel tools; modal>=1.6.0 only for host_infra_sandbox (VM runtime)
modal setup                              # or set MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
python3 modal_server.py                  # stdio server; normally spawned by the MCP client
```

If `modal` or `websocket-client` is missing the server still starts and each tool returns an explanatory `{ok:false, error}` instead of crashing.

## Tools

| Group | Tools |
| --- | --- |
| Account and cleanup | `modal_check_auth`, `modal_list_apps`, `modal_stop_app` |
| Sandbox lifecycle | `modal_create_gpu_sandbox`, `modal_terminate_sandbox`, `modal_list_sandboxes`, `modal_sandbox_status`, `modal_reconnect_sandbox` |
| One-shot execution | `modal_run_code`, `modal_run_shell`, `modal_pip_install`, `modal_sync_files` |
| Persistent kernel | `modal_create_jupyter_kernel`, `modal_run_in_kernel`, `modal_list_kernels`, `modal_stop_jupyter_kernel`, `modal_change_kernel_gpu` |
| Notebook cells (on a kernel) | `modal_add_code_cell`, `modal_add_text_cell`, `modal_get_cells`, `modal_run_cell`, `modal_update_cell`, `modal_delete_cell`, `modal_move_cell` |
| Purpose-built presets | `host_cpu_sandbox` (now with `memory_mib`), `host_one_low_tier_gpu`, `host_two_t4`, `host_one_high_end_gpu` |
| Infrastructure hosting (Docker) | `host_infra_sandbox`, `modal_upload_dir`, `modal_docker_status`, `modal_sandbox_tunnels` |

All tools return a structured `{ok, ...}` dictionary.

## How it works

**Handles and registries.** Creating a sandbox returns a short random `handle`. Three in-memory dictionaries hold the state: `_SANDBOXES` (handle -> Modal `Sandbox`, GPU label, app name, creation time, Modal `object_id`), `_KERNELS` (handle -> Jupyter client, tunnel URL, token) and `_NOTEBOOKS` (handle -> ordered list of cells). Because they live in memory, a server restart orphans running sandboxes; that is what `object_id`, `modal_reconnect_sandbox` (re-attaches through `Sandbox.from_id`), `modal_list_apps` (account-wide view) and `modal_stop_app` (kill an entire app) are for.

**Plain sandboxes.** `modal_create_gpu_sandbox` builds `modal.Image.debian_slim(python_version=...)`, optionally `pip_install`s extra packages, and calls `Sandbox.create(app, image, gpu, timeout, idle_timeout)`. `modal_run_code` runs `python -c <code>` through `sandbox.exec`, `modal_run_shell` runs an argv list with no shell (use `["bash","-c","a | b"]` for pipelines), both with a timeout and returning `returncode`, `stdout`, `stderr`. `modal_sync_files` copies `[remote, local]` pairs with `copy_to_local` / `copy_from_local`, one result per pair, so a bad path does not abort the batch.

**Persistent Jupyter kernels.** `_boot_jupyter_sandbox` creates a sandbox whose *main process* is `jupyter notebook --no-browser --allow-root --ip=0.0.0.0 --port=8888` on `debian_slim(3.12)` with `jupyter~=1.1.0`, exposes port 8888 through Modal's `encrypted_ports` tunnel, and passes a random token as a Modal secret. It polls `/api/status` for up to 60 s; if Jupyter never comes up, or the kernel connection fails, **the sandbox is terminated automatically** so a failed boot cannot keep billing. A small hand-written client, `_JupyterKernelClient`, then speaks Jupyter's public protocol directly: `POST /api/kernels` to create a kernel and a WebSocket on `/api/kernels/<id>/channels`. `execute()` sends an `execute_request` (protocol 5.3), ignores messages whose `parent_header.msg_id` belongs to other requests, collects `stream`, `execute_result`, `display_data` and `error` messages until it has seen both `execute_reply` and `idle` (300 s cap), and `_summarize_kernel_outputs` flattens that into `{ok, status, execution_count, stdout, stderr, result, error{ename,evalue,traceback}}`. Variables and imports therefore survive between `modal_run_in_kernel` calls. The project deliberately avoids third-party Jupyter wrapper libraries; it needs only `urllib` and `websocket-client`.

**Cells.** `modal_add_*_cell`, `modal_get_cells`, `modal_run_cell`, `modal_update_cell`, `modal_delete_cell` and `modal_move_cell` keep a notebook-shaped list per handle (8-hex-character cell ids, `cell_type`, `source`, `outputs`, `execution_count`), mirroring the cell tools of the Colab server in this repo so an agent can use the same workflow on either backend. `modal_run_cell` executes a code cell on the persistent kernel and stores the result on the cell.

**Changing hardware.** A running container cannot be re-sized, so `modal_change_kernel_gpu` stops the kernel, terminates the sandbox (`wait=True`), boots a new one with the requested GPU and re-registers it under the **same handle**. Variables are lost (as with a Colab runtime change) but stored cells survive. If the replacement fails to boot, the handle is dropped and the error says the cells are still readable with `modal_get_cells`.

**Presets.** The `host_*` tools are thin, constrained wrappers over the same boot function, so their handles work with every execution and cell tool: `host_cpu_sandbox(scaling_factor)` (cores = 0.125 x factor, limited to 0.125-16 cores by this server's own guard rails), `host_one_low_tier_gpu` (T4, L4 or A10G), `host_two_t4` (`T4:2`) and `host_one_high_end_gpu` (A100, L40S, H100, H200 or B200). The low/high split is this server's own judgement, not a Modal category.

## Hosting infrastructure (Docker on the VM runtime)

Modal's default Sandboxes run under gVisor, a user-space kernel. It cannot give Docker the virtual network pairs and NAT that container networking needs, so `docker compose` services cannot reach each other there ([Modal guide](https://modal.com/docs/guide/docker-in-sandboxes)). The VM runtime (`runtime="vm"`, Modal client 1.6.0 or newer) gives a Sandbox its own Linux kernel. `host_infra_sandbox` uses it exactly as that guide describes: an `ubuntu:24.04` image with `docker.io` and the compose v2 plugin, `dockerd` as the main process, and a readiness probe on `docker info`.

```text
host_infra_sandbox(cpu_cores=2, memory_mib=6144, encrypted_ports=[443], confirm=True)
modal_upload_dir(handle, "./my-stack", "/work/stack")                 # secrets and caches excluded by default
modal_run_shell(handle, ["bash","-c","cd /work/stack && ./scripts/secrets.sh && docker compose up -d --wait"], timeout=900)
modal_docker_status(handle)                                           # containers, per-container memory, disk
modal_sandbox_tunnels(handle)                                         # public URLs of exposed ports
modal_sync_files(handle, [["/work/stack/results/out.json", "./out.json"]])
modal_terminate_sandbox(handle, confirm=True)
```

- **Memory is explicit.** Modal's default request is 128 MiB; `host_infra_sandbox` defaults to 4096 MiB (512 to 32768 allowed) and `host_cpu_sandbox` takes an optional `memory_mib`.
- **Cost is stated before it is incurred.** The `confirm=True` refusal message gives an estimate from Modal's published Sandbox rates ($0.00003942 per core-second and $0.00000667 per GiB-second, read on 2026-10-03, about $0.142 per core-hour and $0.024 per GiB-hour). Modal bills the higher of the request and actual usage per second, so the estimate is a floor. Example: 2 cores and 4 GiB is about $0.38 per hour.
- **Uploads are safe by default.** `.git`, virtualenvs, `node_modules`, caches, `.env`, `.env.local`, `.tls`, `*.pem`, `*.key` and `*.sqlite` are not uploaded, symlinks are skipped, and trees over 256 MiB are refused before anything leaves the machine. `include_secrets=True` exists but needs the user's explicit approval; creating secrets inside the sandbox is preferable.
- **Old clients fail clearly.** The server checks both the version and that `Sandbox.create` accepts `runtime`, and tells you to run `pip install -U 'modal>=1.6.0'`.
- **Limits.** No GPU on the VM runtime (Modal documents GPUs as gVisor-only). Modal does not document tunnel support specifically for the VM runtime, so `tunnels` reports an error entry instead of failing if a tunnel cannot be read. A Sandbox lives at most 24 hours (the server caps `timeout` at 86400 s and defaults to 3600 s).
- **Verification status.** The tools are covered by 50 unit tests against a fake Modal (`python -m pytest tests -q` in this directory), which check every request sent to Modal (runtime, memory, entrypoint, readiness probe, ports) and every guard that runs before money is spent. The real VM path, including `apt` package names on the image and tunnel behaviour on the VM runtime, has not been exercised against Modal's service by this change; run one short sandbox (about $0.04 for 6 minutes at 2 cores and 4 GiB) before relying on it.

`mcp` 2.x renamed `FastMCP` to `MCPServer`; the server imports whichever exists.

## Cost safety

- Creating or switching a sandbox, and terminating one, require `confirm=True`. Without it the tool returns `{ok: false, error: "confirm must be True to <action> ..."}` describing the action. The server's MCP `instructions` tell the agent to state the GPU type and approximate hourly rate and get the user's go-ahead in the same turn before passing `confirm=True`.
- Defaults are short: 600 s hard lifetime and 120 s idle timeout. They are a safety net, not a substitute for terminating.
- `modal_terminate_sandbox` uses `terminate(wait=True)`. Modal's default (`wait=False`) only dispatches the stop request; waiting returns a confirmed exit code, and on failure the handle stays tracked so the termination can be retried.
- Valid GPU names are checked locally (`T4, L4, A10G, A100, L40S, H100, H200, B200, NONE`, with an optional `:N` count) before any call to Modal.
- Jupyter server URLs and tokens grant code execution in the sandbox; tool responses tell the agent not to paste them elsewhere.

## Client configuration

See [`../configs`](../configs) for a ready-to-copy MCP client entry.
