"""Tests for the infrastructure-hosting tools of modal_server.py.

Modal itself is replaced by a small fake, so nothing here creates a billable
resource. The fake records what the server asks Modal to do; the tests assert
on those requests (runtime, memory, entrypoint, readiness probe) and on every
guard the server applies before spending money.

Run from the modal-gpu directory:  python -m pytest tests -q
"""
import os
import sys
import tarfile
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import modal_server as ms  # noqa: E402


# --------------------------------------------------------------------------- fake Modal
class FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self._rc = [stdout], [stderr], returncode

    def wait(self):
        return self._rc


class FakeFilesystem:
    def __init__(self):
        self.uploads = []

    def copy_from_local(self, local, remote):
        assert os.path.exists(local)
        self.uploads.append((local, remote))
        with tarfile.open(local) as t:  # keep the member names: the file is deleted afterwards
            self.last_members = sorted(m.name for m in t.getmembers())


class FakeTunnel:
    def __init__(self, url):
        self.url = url


class FakeSandbox:
    created = []
    ready_error = None
    exec_results = {}

    def __init__(self, **kw):
        self.kw = kw
        self.object_id = "sb-fake"
        self.filesystem = FakeFilesystem()
        self.terminated = False
        self.exec_calls = []

    @classmethod
    def create(cls, *args, runtime=None, **kw):
        sb = cls(args=args, runtime=runtime, **kw)
        cls.created.append(sb)
        return sb

    def wait_until_ready(self, timeout=300):
        if FakeSandbox.ready_error:
            raise RuntimeError(FakeSandbox.ready_error)

    def exec(self, *argv, timeout=None):
        self.exec_calls.append(argv)
        for prefix, res in FakeSandbox.exec_results.items():
            if " ".join(argv).startswith(prefix):
                return FakeProcess(**res)
        return FakeProcess("ok")

    def tunnels(self, timeout=50):
        return {p: FakeTunnel(f"https://x-{p}.modal.host") for p in self.kw.get("encrypted_ports", [])}

    def terminate(self, wait=False):
        self.terminated = True
        return 0


class FakeImage:
    def __init__(self):
        self.steps = []

    @classmethod
    def from_registry(cls, tag, **kw):
        i = cls()
        i.steps.append(("from_registry", tag))
        return i

    def env(self, vars):
        self.steps.append(("env", vars))
        return self

    def apt_install(self, *pkgs, **kw):
        self.steps.append(("apt_install", list(pkgs)))
        return self


class FakeProbe:
    @staticmethod
    def with_exec(*argv, interval_ms=100):
        return ("probe", argv, interval_ms)


@pytest.fixture()
def fake(monkeypatch):
    """A fake Modal 1.6.0 whose Sandbox.create records requests."""
    FakeSandbox.created = []
    FakeSandbox.ready_error = None
    FakeSandbox.exec_results = {}
    mod = types.SimpleNamespace(
        __version__="1.6.0",
        Image=FakeImage,
        Probe=FakeProbe,
        App=types.SimpleNamespace(lookup=lambda name, create_if_missing=False: f"app:{name}"),
    )

    class Sandbox(FakeSandbox):
        @classmethod
        def create(cls, *args, runtime=None, **kw):
            return FakeSandbox.create(*args, runtime=runtime, **kw)

    mod.Sandbox = Sandbox
    monkeypatch.setattr(ms, "modal", mod)
    monkeypatch.setattr(ms, "_SANDBOXES", {})
    return mod


# --------------------------------------------------------------------------- pure helpers
def test_cost_estimate_uses_published_rates():
    # 2 cores for an hour plus 4 GiB for an hour at $0.00003942/core-s and $0.00000667/GiB-s
    assert ms._estimate_hourly_cost(2.0, 4096) == pytest.approx(2 * 0.14191200 + 4 * 0.02401200)


@pytest.mark.parametrize("version,expected", [("1.6.0", (1, 6, 0)), ("1.6.1.dev3", (1, 6, 1)),
                                              ("1.5.3", (1, 5, 3)), ("weird", (0,))])
def test_version_parsing(monkeypatch, version, expected):
    monkeypatch.setattr(ms, "modal", types.SimpleNamespace(__version__=version))
    assert ms._modal_version_tuple() == expected


def test_vm_runtime_check_rejects_an_old_client(monkeypatch):
    class OldSandbox:
        @staticmethod
        def create(*args, cpu=None, memory=None):  # no `runtime` argument, like modal 1.5.x
            pass
    monkeypatch.setattr(ms, "modal", types.SimpleNamespace(__version__="1.5.3", Sandbox=OldSandbox))
    err = ms._require_vm_runtime()
    assert err and not err["ok"] and "pip install -U 'modal>=1.6.0'" in err["error"]


def test_vm_runtime_check_needs_the_argument_not_just_the_version(monkeypatch):
    class NoRuntime:
        @staticmethod
        def create(*args, cpu=None):
            pass
    monkeypatch.setattr(ms, "modal", types.SimpleNamespace(__version__="1.7.0", Sandbox=NoRuntime))
    assert ms._require_vm_runtime() is not None


def test_vm_runtime_check_passes_on_a_capable_client(monkeypatch):
    class Capable:
        @staticmethod
        def create(*args, runtime=None):
            pass
    monkeypatch.setattr(ms, "modal", types.SimpleNamespace(__version__="1.6.0", Sandbox=Capable))
    assert ms._require_vm_runtime() is None


@pytest.mark.parametrize("path,excluded", [
    (".git/config", True), ("src/app.py", False), ("a/b/secret.key", True), (".env.local", True),
    ("x/.venv/lib/site.py", True), ("docs/.environment", False), ("data/trips.sqlite", True),
    ("keys/server.pem", True), ("compose/compose.core.yaml", False), (".tls/tls.crt", True),
])
def test_upload_exclusion_rules(path, excluded):
    assert ms._upload_excluded(path, ms.DEFAULT_UPLOAD_EXCLUDES) is excluded


def make_tree(tmp_path):
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "stack.yaml").write_text("services: {}\n")
    (tmp_path / ".env.local").write_text("SECRET=1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "a.key").write_text("k\n")
    (tmp_path / "run.sh").write_text("echo hi\n")
    os.symlink("/etc/passwd", tmp_path / "link")
    return tmp_path


def test_tarball_contains_only_allowed_files(tmp_path):
    root = make_tree(tmp_path)
    path, files, total, skipped = ms._build_tarball(str(root), ms.DEFAULT_UPLOAD_EXCLUDES, 10_000_000)
    try:
        with tarfile.open(path) as t:
            names = sorted(m.name for m in t.getmembers())
    finally:
        os.remove(path)
    assert names == ["compose/stack.yaml", "run.sh"]
    assert files == 2 and total > 0
    assert skipped >= 4  # .env.local, .git, keys/a.key, the symlink


def test_tarball_refuses_an_oversized_tree_and_leaves_no_temp_archive(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"x" * 5000)
    tmp = ms.tempfile.gettempdir()
    before = {f for f in os.listdir(tmp) if f.startswith("modal-upload-")}
    with pytest.raises(ValueError, match="upload limit"):
        ms._build_tarball(str(tmp_path), [], 1000)
    assert {f for f in os.listdir(tmp) if f.startswith("modal-upload-")} == before


def test_tarball_rejects_a_missing_directory(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        ms._build_tarball(str(tmp_path / "nope"), [], 1000)


# --------------------------------------------------------------------------- host_infra_sandbox
def test_infra_refuses_without_confirm_and_states_the_cost(fake):
    r = ms.host_infra_sandbox(cpu_cores=2, memory_mib=4096)
    assert not r["ok"] and "confirm must be True" in r["error"]
    assert "$0.38/hour" in r["error"] and "4096 MiB" in r["error"]
    assert FakeSandbox.created == []  # nothing was created


@pytest.mark.parametrize("kwargs", [
    {"cpu_cores": 0.01}, {"cpu_cores": 99}, {"memory_mib": 100}, {"memory_mib": 10**6},
    {"timeout": 10}, {"timeout": 10**6}, {"idle_timeout": 5},
    {"encrypted_ports": [0]}, {"encrypted_ports": [70000]}, {"encrypted_ports": [80, 80]},
    {"encrypted_ports": list(range(1000, 1010))}, {"encrypted_ports": [True]},
    {"apt_packages": ["x; rm -rf /"]}, {"apt_packages": ["Bad Name"]}, {"apt_packages": [5]},
])
def test_infra_validates_inputs_before_anything_is_created(fake, kwargs):
    r = ms.host_infra_sandbox(confirm=True, **kwargs)
    assert not r["ok"] and "error" in r
    assert FakeSandbox.created == []


def test_infra_requests_a_vm_with_dockerd_memory_and_a_probe(fake):
    r = ms.host_infra_sandbox(cpu_cores=2, memory_mib=6144, encrypted_ports=[443, 8080],
                              apt_packages=["jq"], confirm=True)
    assert r["ok"], r
    sb = FakeSandbox.created[0]
    assert sb.kw["runtime"] == "vm" and sb.kw["args"] == ("dockerd",)
    assert sb.kw["cpu"] == 2.0 and sb.kw["memory"] == 6144
    assert sb.kw["encrypted_ports"] == [443, 8080]
    assert sb.kw["readiness_probe"] == ("probe", ("docker", "info"), 500)
    steps = dict(sb.kw["image"].steps)
    assert steps["from_registry"] == "ubuntu:24.04"
    assert "docker.io" in steps["apt_install"] and "docker-compose-v2" in steps["apt_install"] and "jq" in steps["apt_install"]
    assert "gpu" not in sb.kw  # GPUs only exist on the gVisor runtime
    assert r["tunnels"] == {443: "https://x-443.modal.host", 8080: "https://x-8080.modal.host"}
    assert r["estimated_hourly_cost_usd"] == pytest.approx(ms._estimate_hourly_cost(2, 6144), abs=1e-4)
    assert r["handle"] in ms._SANDBOXES


def test_infra_terminates_the_sandbox_when_docker_never_becomes_ready(fake):
    FakeSandbox.ready_error = "probe timed out"
    r = ms.host_infra_sandbox(confirm=True)
    assert not r["ok"] and "terminated automatically" in r["error"]
    assert FakeSandbox.created[0].terminated is True
    assert ms._SANDBOXES == {}  # no handle for a sandbox that no longer exists


def test_infra_reports_creation_failures_without_registering_a_handle(fake, monkeypatch):
    def boom(*a, runtime=None, **k):  # keeps the `runtime` parameter the feature check looks for
        raise RuntimeError("quota exceeded")
    monkeypatch.setattr(fake.Sandbox, "create", staticmethod(boom))
    r = ms.host_infra_sandbox(confirm=True)
    assert not r["ok"] and "quota exceeded" in r["error"]
    assert ms._SANDBOXES == {}


def test_infra_explains_an_old_client_instead_of_failing_obscurely(monkeypatch):
    class OldSandbox:
        @staticmethod
        def create(*a, **k):
            pass
    monkeypatch.setattr(ms, "modal", types.SimpleNamespace(__version__="1.5.3", Sandbox=OldSandbox))
    r = ms.host_infra_sandbox(confirm=True)
    assert not r["ok"] and "pip install -U" in r["error"]


# --------------------------------------------------------------------------- upload, status, presets
def register(fake):
    sb = FakeSandbox()
    ms._SANDBOXES["h1"] = {"sandbox": sb, "object_id": "sb-fake", "gpu": "NONE", "app_name": "a", "created": 0}
    return sb


def test_upload_dir_skips_secrets_by_default_and_extracts_remotely(fake, tmp_path):
    root = make_tree(tmp_path)
    sb = register(fake)
    r = ms.modal_upload_dir("h1", str(root), "/work/stack")
    assert r["ok"] and r["files"] == 2 and r["secrets_included"] is False
    assert sb.filesystem.last_members == ["compose/stack.yaml", "run.sh"]
    extract = " ".join(sb.exec_calls[-1])
    assert "tar -xzf" in extract and "/work/stack" in extract


def test_upload_dir_includes_secret_like_files_only_when_asked(fake, tmp_path):
    root = make_tree(tmp_path)
    sb = register(fake)
    r = ms.modal_upload_dir("h1", str(root), "/work/stack", include_secrets=True)
    assert r["ok"] and ".env.local" in sb.filesystem.last_members and "keys/a.key" in sb.filesystem.last_members
    assert ".git/HEAD" not in sb.filesystem.last_members  # junk exclusions stay


def test_upload_dir_quotes_the_remote_path(fake, tmp_path):
    root = make_tree(tmp_path)
    sb = register(fake)
    ms.modal_upload_dir("h1", str(root), "/work/a b; rm -rf x")
    assert "'/work/a b; rm -rf x'" in " ".join(sb.exec_calls[-1])


def test_upload_dir_rejects_relative_remote_paths_and_unknown_handles(fake, tmp_path):
    register(fake)
    assert not ms.modal_upload_dir("h1", str(tmp_path), "relative/path")["ok"]
    assert not ms.modal_upload_dir("nope", str(tmp_path), "/x")["ok"]


def test_upload_dir_leaves_no_temp_archive_behind(fake, tmp_path):
    root = make_tree(tmp_path)
    register(fake)
    before = {f for f in os.listdir(ms.tempfile.gettempdir()) if f.startswith("modal-upload-")}
    ms.modal_upload_dir("h1", str(root), "/work/stack")
    after = {f for f in os.listdir(ms.tempfile.gettempdir()) if f.startswith("modal-upload-")}
    assert after == before


def test_docker_status_summarises_containers_usage_and_machine(fake):
    FakeSandbox.exec_results = {
        "docker ps": {"stdout": '{"Names":"pg","Image":"postgres","Status":"Up 1m (healthy)","State":"running"}\n'},
        "docker stats": {"stdout": '{"Name":"pg","MemUsage":"54MiB / 256MiB","MemPerc":"21%","CPUPerc":"0.1%"}\n'},
        "free": {"stdout": "Mem: 4096"}, "df": {"stdout": "overlay 512G"},
    }
    register(fake)
    r = ms.modal_docker_status("h1")
    assert r["ok"]
    assert r["containers"] == [{"name": "pg", "image": "postgres", "status": "Up 1m (healthy)", "state": "running"}]
    assert r["usage"][0]["mem"] == "54MiB / 256MiB"


def test_docker_status_explains_when_docker_is_not_there(fake):
    FakeSandbox.exec_results = {"docker ps": {"stderr": "docker: not found", "returncode": 127}}
    register(fake)
    r = ms.modal_docker_status("h1")
    assert not r["ok"] and "host_infra_sandbox" in r["error"]


def test_tunnels_lists_public_urls(fake):
    sb = register(fake)
    sb.kw["encrypted_ports"] = [8443]
    assert ms.modal_sandbox_tunnels("h1") == {"ok": True, "tunnels": {8443: "https://x-8443.modal.host"}}


def test_cpu_preset_validates_memory_and_states_it_in_the_confirmation(fake):
    assert not ms.host_cpu_sandbox(8, memory_mib=64)["ok"]
    r = ms.host_cpu_sandbox(8, memory_mib=4096)  # no confirm
    assert "4096 MiB" in r["error"] and "/hour" in r["error"]
