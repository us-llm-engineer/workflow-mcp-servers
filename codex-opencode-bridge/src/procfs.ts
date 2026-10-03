import { readdir, readFile, readlink } from "node:fs/promises";

/** Lists every numeric entry under /proc, i.e. every visible pid. */
export async function listPids(): Promise<number[]> {
  let entries: string[];
  try {
    entries = await readdir("/proc");
  } catch {
    return [];
  }
  return entries.filter((name) => /^\d+$/.test(name)).map(Number);
}

/** Reads /proc/pid/cmdline, splitting on NUL and dropping empty segments. */
export async function readCmdline(pid: number): Promise<string[] | null> {
  try {
    const raw = await readFile(`/proc/${pid}/cmdline`, "utf8");
    return raw.split("\0").filter((segment) => segment.length > 0);
  } catch {
    return null;
  }
}

/** Reads /proc/pid/comm (the kernel-recorded executable name), trimmed. */
export async function readComm(pid: number): Promise<string | null> {
  try {
    const raw = await readFile(`/proc/${pid}/comm`, "utf8");
    return raw.trim();
  } catch {
    return null;
  }
}

/** Resolves fd 0; returns it only when it is a pseudo-terminal device. */
export async function readStdinTty(pid: number): Promise<string | null> {
  try {
    const target = await readlink(`/proc/${pid}/fd/0`);
    return target.startsWith("/dev/pts/") ? target : null;
  } catch {
    return null;
  }
}

/** Resolves /proc/pid/cwd. */
export async function readCwd(pid: number): Promise<string | null> {
  try {
    return await readlink(`/proc/${pid}/cwd`);
  } catch {
    return null;
  }
}

/** Parses /proc/pid/environ (NUL-separated KEY=VALUE pairs) into a record. */
export async function readEnviron(pid: number): Promise<Record<string, string> | null> {
  let raw: string;
  try {
    raw = await readFile(`/proc/${pid}/environ`, "utf8");
  } catch {
    return null;
  }
  const env: Record<string, string> = {};
  for (const entry of raw.split("\0")) {
    if (!entry) continue;
    const eq = entry.indexOf("=");
    if (eq === -1) continue;
    env[entry.slice(0, eq)] = entry.slice(eq + 1);
  }
  return env;
}

/**
 * Reads field 22 (starttime) of /proc/pid/stat. The comm field (field 2) is
 * parenthesized and may itself contain spaces or parentheses, so we split
 * everything after the LAST ")" on whitespace: that remainder's index 0 is
 * field 3 (state), making starttime (field 22) sit at index 19.
 */
export async function readStartTime(pid: number): Promise<string | null> {
  let raw: string;
  try {
    raw = await readFile(`/proc/${pid}/stat`, "utf8");
  } catch {
    return null;
  }
  const closeParen = raw.lastIndexOf(")");
  if (closeParen === -1) return null;
  const rest = raw
    .slice(closeParen + 1)
    .trim()
    .split(/\s+/);
  return rest[19] ?? null;
}

/** Resolves every /proc/pid/fd/* entry; tolerant of individual fds vanishing. */
export async function listOpenFiles(pid: number): Promise<string[]> {
  let names: string[];
  try {
    names = await readdir(`/proc/${pid}/fd`);
  } catch {
    return [];
  }
  const targets = await Promise.all(
    names.map(async (name) => {
      try {
        return await readlink(`/proc/${pid}/fd/${name}`);
      } catch {
        return null;
      }
    }),
  );
  return targets.filter((target): target is string => target !== null);
}
