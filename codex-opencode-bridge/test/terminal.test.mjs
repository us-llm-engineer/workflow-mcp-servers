import test from "node:test";
import assert from "node:assert/strict";
import { execFile as execFileCb, spawn } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, mkdir, writeFile, readFile, rm, realpath } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

process.env.CODEX_OPENCODE_BRIDGE_NO_CODEX_DAEMON_START = "1";

import { codexResumeWindows, resolveCodexSession, sendToCodex } from "../dist/codex.js";
import { resolveClaudeSession, sendToClaude } from "../dist/claude.js";
import { readCmdline } from "../dist/procfs.js";

const execFile = promisify(execFileCb);

// ---------- generic helpers ----------

async function waitFor(fn, { timeoutMs = 8000, intervalMs = 50, label = "condition" } = {}) {
  const start = Date.now();
  for (;;) {
    const result = await fn();
    if (result !== undefined && result !== false && result !== null) return result;
    if (Date.now() - start > timeoutMs) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

async function waitForFileContent(path, predicate, opts = {}) {
  return waitFor(async () => {
    let content;
    try {
      content = await readFile(path, "utf8");
    } catch {
      return undefined;
    }
    return predicate(content) ? content : undefined;
  }, { label: `contents of ${path}`, ...opts });
}

async function waitForCmdline(pid, expected, opts = {}) {
  return waitFor(async () => {
    const cmdline = await readCmdline(pid);
    if (!cmdline || cmdline.length !== expected.length) return undefined;
    return cmdline.every((value, index) => value === expected[index]) ? cmdline : undefined;
  }, { label: `pid ${pid} cmdline to become ${JSON.stringify(expected)}`, ...opts });
}

async function tmuxRaw(sock, args) {
  return execFile("tmux", ["-S", sock, ...args]);
}

async function newPrivateTmux(sock, sessionName, command) {
  await tmuxRaw(sock, ["-f", "/dev/null", "new-session", "-d", "-s", sessionName, "-x", "120", "-y", "30", command]);
}

async function newTmuxWindow(sock, target, windowName, command) {
  // A bare session name as -t (e.g. "t") is treated as that session's current
  // window and collides with it; "t:" (empty window part) appends instead.
  await tmuxRaw(sock, ["new-window", "-t", `${target}:`, "-n", windowName, command]);
}

async function tmuxPanePid(sock, target) {
  const { stdout } = await tmuxRaw(sock, ["display", "-p", "-t", target, "#{pane_pid}"]);
  return Number(stdout.trim());
}

async function killTmuxServer(sock) {
  await tmuxRaw(sock, ["kill-server"]).catch(() => {});
}

function randomCodexUuid() {
  // Version/variant nibbles satisfy the same UUID shape Codex uses; the rest
  // is random so parallel test runs never collide.
  return randomUUID();
}

async function makeRollout(codexHome, uuid, cwd, { dateParts = ["2026", "01", "01"] } = {}) {
  const dir = join(codexHome, "sessions", ...dateParts);
  await mkdir(dir, { recursive: true });
  const path = join(dir, `rollout-2026-01-01T00-00-00-${uuid}.jsonl`);
  await writeFile(path, `${JSON.stringify({ type: "session_meta", payload: { id: uuid, cwd } })}\n`);
  return path;
}

async function appendSessionIndex(codexHome, lines) {
  const path = join(codexHome, "session_index.jsonl");
  const content = lines.map((line) => JSON.stringify(line)).join("\n") + "\n";
  await writeFile(path, content);
}

async function writeClaudeRegistryEntry(claudeConfigDir, entry) {
  const dir = join(claudeConfigDir, "sessions");
  await mkdir(dir, { recursive: true });
  await writeFile(join(dir, `${entry.pid}.json`), JSON.stringify(entry));
}

async function readStartTimeOf(pid) {
  const stat = await readFile(`/proc/${pid}/stat`, "utf8");
  const rest = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
  return rest[19];
}

async function makeTempDir(prefix) {
  return mkdtemp(join(tmpdir(), prefix));
}

// A thread the daemon or VS Code created but that never ran a turn: it has a
// name and a cwd recorded in Codex's state database, but no rollout file yet.
async function makeTurnlessThread(codexHome, uuid, cwd, updatedMs = 0) {
  const { DatabaseSync } = await import("node:sqlite");
  await mkdir(codexHome, { recursive: true });
  const db = new DatabaseSync(join(codexHome, "state_5.sqlite"));
  db.exec(`create table if not exists threads (
    id text primary key, rollout_path text not null, created_at integer not null,
    updated_at integer not null, source text not null, model_provider text not null,
    cwd text not null, title text not null, sandbox_policy text not null,
    approval_mode text not null, tokens_used integer not null default 0,
    has_user_event integer not null, updated_at_ms integer)`);
  db.prepare(
    "insert or ignore into threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, has_user_event, updated_at_ms) values (?, ?, 0, 0, 'vscode', 'openai', ?, '', '', '', 0, ?)",
  ).run(uuid, join(codexHome, "sessions", "2026", "01", "01", `rollout-2026-01-01T00-00-00-${uuid}.jsonl`), cwd, updatedMs);
  db.close();
}

// Creates a real, realpath-canonicalized folder nested under `root` so a
// single `rm(root, { recursive: true })` cleans everything up.
async function makeFolder(root, name) {
  const dir = join(root, name);
  await mkdir(dir, { recursive: true });
  return realpath(dir);
}

// ---------- tests ----------

test("codex: resolves by name and delivers a message via tmux", async () => {
  const root = await makeTempDir("codex-bridge-name-");
  const sock = join(root, "sock");
  try {
    const codexHome = join(root, "codex-home");
    const folder = await makeFolder(root, "folder");
    const uuid = randomCodexUuid();
    const rolloutPath = await makeRollout(codexHome, uuid, folder);
    await appendSessionIndex(codexHome, [
      { id: uuid, thread_name: "old-name", updated_at: "2026-01-01T00:00:00Z" },
      { id: uuid, thread_name: "my-session", updated_at: "2026-01-02T00:00:00Z" },
    ]);

    const outPath = join(root, "OUT");
    await newPrivateTmux(sock, "t", `bash -c 'exec 3<"${rolloutPath}"; stty -echo; exec -a codex cat > "${outPath}"'`);
    const pid = await tmuxPanePid(sock, "t");
    await waitForCmdline(pid, ["codex"]);

    process.env.CODEX_HOME = codexHome;
    const session = await resolveCodexSession(folder, "my-session");
    assert.equal(session.tool, "codex");
    assert.equal(session.sessionId, uuid);
    assert.equal(session.folder, folder);
    assert.equal(session.name, "my-session");
    assert.equal(session.transcriptPath, rolloutPath);

    const message = 'hello "world", it\'s quoting time — spaces too';
    const delivery = await sendToCodex(session, message);
    assert.equal(delivery.transport, "tmux");
    assert.equal(delivery.pid, pid);
    assert.match(delivery.pane, /^%\d+$/);

    await waitForFileContent(outPath, (content) => content === `${message}\n`);
  } finally {
    await killTmuxServer(sock);
    await rm(root, { recursive: true, force: true });
  }
});

test("codex: a pre-daemon Codex outside tmux reports TUI_CONTROL_UNAVAILABLE", async () => {
  const root = await makeTempDir("codex-bridge-notmux-");
  try {
    const codexHome = join(root, "codex-home");
    const folder = join(root, "folder");
    const uuid = randomCodexUuid();
    await makeRollout(codexHome, uuid, folder);
    const rolloutPath = (await execFile("bash", ["-c", `ls ${join(codexHome, "sessions", "2026", "01", "01")}/*.jsonl`])).stdout.trim();

    const env = { ...process.env };
    delete env.TMUX;
    const child = spawn(
      "script",
      ["-qfc", `bash -c 'exec 3<"${rolloutPath}"; exec -a codex sleep 30'`, "/dev/null"],
      { env, stdio: "ignore", detached: true },
    );
    await new Promise((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", reject);
    });
    try {
      const session = { tool: "codex", sessionId: uuid, folder, name: null, transcriptPath: rolloutPath };
      // The spawned process needs a moment to reach its final `exec -a codex
      // sleep 30` form; TARGET_NOT_RUNNING just means it is not ready yet.
      await waitFor(async () => {
        try {
          await sendToCodex(session, "hello");
          throw new Error("sendToCodex unexpectedly succeeded outside tmux");
        } catch (error) {
          if (error.code === "TUI_CONTROL_UNAVAILABLE") return error;
          if (error.code === "TARGET_NOT_RUNNING") return undefined;
          throw error;
        }
      }, { label: "sendToCodex to report TUI_CONTROL_UNAVAILABLE" });
    } finally {
      try { process.kill(-child.pid, "SIGKILL"); } catch { /* already gone */ }
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("codex: FOLDER_MISMATCH and SESSION_NOT_FOUND", async () => {
  const root = await makeTempDir("codex-bridge-mismatch-");
  try {
    const codexHome = join(root, "codex-home");
    const realFolder = await makeFolder(root, "real-folder");
    const otherFolder = await makeFolder(root, "other-folder");
    const uuid = randomCodexUuid();
    await makeRollout(codexHome, uuid, realFolder);

    process.env.CODEX_HOME = codexHome;
    await assert.rejects(
      resolveCodexSession(otherFolder, uuid),
      (error) => error.code === "FOLDER_MISMATCH",
    );
    await assert.rejects(
      resolveCodexSession(realFolder, "no-such-name"),
      (error) => error.code === "SESSION_NOT_FOUND",
    );
    await assert.rejects(
      resolveCodexSession(realFolder, randomCodexUuid()),
      (error) => error.code === "SESSION_NOT_FOUND",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("codex: resolves a turnless thread (no rollout yet) from its state-database cwd", async () => {
  const root = await makeTempDir("codex-bridge-turnless-");
  try {
    const codexHome = join(root, "codex-home");
    const folder = await makeFolder(root, "folder");
    const uuid = randomCodexUuid();
    await makeTurnlessThread(codexHome, uuid, folder);
    await appendSessionIndex(codexHome, [{ id: uuid, thread_name: "fresh-thread", updated_at: "2026-01-01T00:00:00Z" }]);

    process.env.CODEX_HOME = codexHome;
    const session = await resolveCodexSession(folder, "fresh-thread");
    assert.equal(session.sessionId, uuid);
    assert.equal(session.folder, folder);
    assert.equal(session.name, "fresh-thread");
    assert.equal(session.transcriptPath, null);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("codex: two threads with the same name resolve to the one Codex touched most recently", async () => {
  const root = await makeTempDir("codex-bridge-samename-");
  try {
    const codexHome = join(root, "codex-home");
    const folder = await makeFolder(root, "folder");
    const older = randomCodexUuid();
    const newer = randomCodexUuid();
    await makeTurnlessThread(codexHome, older, folder, 1_000);
    await makeTurnlessThread(codexHome, newer, folder, 9_000);
    await appendSessionIndex(codexHome, [
      { id: newer, thread_name: "same-name", updated_at: "2026-01-01T00:00:00Z" },
      { id: older, thread_name: "same-name", updated_at: "2026-01-02T00:00:00Z" },
    ]);
    process.env.CODEX_HOME = codexHome;
    const session = await resolveCodexSession(folder, "same-name");
    assert.equal(session.sessionId, newer, "recency comes from Codex's own state, not from the index order");
    assert.deepEqual(session.olderSessionIds, [older]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("codex: resume windows are found by thread id, and only interactive ones", async () => {
  const uuid = randomCodexUuid();
  const other = randomCodexUuid();
  const spawnWindow = (id) => spawn("script", ["-qfc", `bash -c "exec -a codex sh -c 'sleep 30; true' resume ${id}"`, "/dev/null"], { stdio: "ignore", detached: true });
  const mine = spawnWindow(uuid);
  const theirs = spawnWindow(other);
  try {
    const windows = await waitFor(async () => {
      const found = await codexResumeWindows(uuid);
      return found.length === 1 ? found : undefined;
    }, { label: "the resume window" });
    assert.match(windows[0].tty, /^\/dev\/pts\//);
    assert.equal((await codexResumeWindows(other)).length, 1);
    assert.equal((await codexResumeWindows(randomCodexUuid())).length, 0);
  } finally {
    for (const child of [mine, theirs]) { try { process.kill(-child.pid, "SIGKILL"); } catch { /* gone */ } }
  }
});

test("claude: interactive session resolves and delivers a message via tmux", async () => {
  const root = await makeTempDir("claude-bridge-interactive-");
  const sock = join(root, "sock");
  try {
    const claudeConfigDir = join(root, "claude-home");
    const folder = await makeFolder(root, "folder");
    const outPath = join(root, "OUT");

    await newPrivateTmux(sock, "t", `bash -c 'stty -echo; exec cat > "${outPath}"'`);
    const pid = await tmuxPanePid(sock, "t");
    const procStart = await readStartTimeOf(pid);

    const sessionId = randomUUID();
    await writeClaudeRegistryEntry(claudeConfigDir, {
      pid, sessionId, cwd: folder, procStart, kind: "interactive", name: "fake-claude", status: "busy",
    });

    process.env.CLAUDE_CONFIG_DIR = claudeConfigDir;
    const session = await resolveClaudeSession(folder, "fake-claude");
    assert.equal(session.sessionId, sessionId);
    assert.equal(session.name, "fake-claude");

    const message = "hi from the test";
    const delivery = await sendToClaude(session, message);
    assert.equal(delivery.transport, "tmux");
    assert.equal(delivery.pid, pid);
    assert.match(delivery.pane, /^%\d+$/);

    await waitForFileContent(outPath, (content) => content === `${message}\n`);
  } finally {
    await killTmuxServer(sock);
    await rm(root, { recursive: true, force: true });
  }
});

test("claude: stale registry entry (pid reused) is not live", async () => {
  const root = await makeTempDir("claude-bridge-stale-");
  const sock = join(root, "sock");
  try {
    const claudeConfigDir = join(root, "claude-home");
    const folder = await makeFolder(root, "folder");

    await newPrivateTmux(sock, "t", "bash -c 'stty -echo; exec cat > /dev/null'");
    const pid = await tmuxPanePid(sock, "t");

    const sessionId = randomUUID();
    await writeClaudeRegistryEntry(claudeConfigDir, {
      pid, sessionId, cwd: folder, procStart: "not-the-real-start-time", kind: "interactive", name: "stale-claude",
    });

    process.env.CLAUDE_CONFIG_DIR = claudeConfigDir;
    const session = { tool: "claude", sessionId, folder, name: "stale-claude", transcriptPath: null };
    await assert.rejects(
      sendToClaude(session, "hello"),
      (error) => error.code === "TARGET_NOT_RUNNING",
    );
  } finally {
    await killTmuxServer(sock);
    await rm(root, { recursive: true, force: true });
  }
});

test("claude: two live sessions with the same name resolve to the most recently active one", async () => {
  const root = await makeTempDir("claude-bridge-ambiguous-");
  const sock = join(root, "sock");
  try {
    const claudeConfigDir = join(root, "claude-home");
    const folder = await makeFolder(root, "folder");

    await newPrivateTmux(sock, "t", "bash -c 'stty -echo; exec cat > /dev/null'");
    const pidA = await tmuxPanePid(sock, "t");
    const procStartA = await readStartTimeOf(pidA);

    await newTmuxWindow(sock, "t", "w2", "bash -c 'stty -echo; exec cat > /dev/null'");
    const pidB = await tmuxPanePid(sock, "t:w2");
    const procStartB = await readStartTimeOf(pidB);

    const sessionA = randomUUID();
    const sessionB = randomUUID();
    await writeClaudeRegistryEntry(claudeConfigDir, {
      pid: pidA, sessionId: sessionA, cwd: folder, procStart: procStartA, kind: "interactive", name: "dup-name", startedAt: 1000, updatedAt: 5000,
    });
    await writeClaudeRegistryEntry(claudeConfigDir, {
      pid: pidB, sessionId: sessionB, cwd: folder, procStart: procStartB, kind: "interactive", name: "dup-name", startedAt: 2000, updatedAt: 9000,
    });

    process.env.CLAUDE_CONFIG_DIR = claudeConfigDir;
    const session = await resolveClaudeSession(folder, "dup-name");
    assert.equal(session.sessionId, sessionB, "the more recently active session wins");
    assert.deepEqual(session.olderSessionIds, [sessionA]);
  } finally {
    await killTmuxServer(sock);
    await rm(root, { recursive: true, force: true });
  }
});

test("claude: transcript custom-title fallback resolves to the last title only", async () => {
  const root = await makeTempDir("claude-bridge-fallback-");
  try {
    const claudeConfigDir = join(root, "claude-home");
    const folder = await makeFolder(root, "folder");
    const escapedFolder = folder.replace(/[^A-Za-z0-9]/g, "-");
    const projectDir = join(claudeConfigDir, "projects", escapedFolder);
    await mkdir(projectDir, { recursive: true });

    const sessionId = randomUUID();
    const lines = [
      { type: "custom-title", customTitle: "first-title", sessionId },
      { type: "user", message: "irrelevant filler line" },
      { type: "custom-title", customTitle: "second-title", sessionId },
    ];
    await writeFile(join(projectDir, `${sessionId}.jsonl`), lines.map((l) => JSON.stringify(l)).join("\n") + "\n");

    process.env.CLAUDE_CONFIG_DIR = claudeConfigDir;
    const resolved = await resolveClaudeSession(folder, "second-title");
    assert.equal(resolved.sessionId, sessionId);
    assert.equal(resolved.transcriptPath, join(projectDir, `${sessionId}.jsonl`));

    await assert.rejects(
      resolveClaudeSession(folder, "first-title"),
      (error) => error.code === "SESSION_NOT_FOUND",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
