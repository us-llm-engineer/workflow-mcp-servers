import { spawn } from "node:child_process";
import { closeSync, openSync, readFileSync, statSync } from "node:fs";
import { BridgeError } from "./errors.js";

export interface ExecOptions {
  input?: string;
  timeoutMs?: number;
  maxOutputBytes?: number;
  cwd?: string;
  /** Capture stdout in a regular file, for programs that truncate large pipe output. */
  stdoutPath?: string;
}

export interface ExecResult {
  stdout: string;
  stderr: string;
}

/** Runs a program directly: arguments and input are never interpreted by a shell. */
export function execFile(command: string, args: string[], options: ExecOptions = {}): Promise<ExecResult> {
  const timeoutMs = options.timeoutMs ?? 15_000;
  const maxOutputBytes = options.maxOutputBytes ?? 4 * 1024 * 1024;

  return new Promise((resolve, reject) => {
    let stdout: Buffer<ArrayBufferLike> = Buffer.alloc(0);
    let stderr: Buffer<ArrayBufferLike> = Buffer.alloc(0);
    let timedOut = false;
    let overflowed = false;
    let stdoutFd: number | null = options.stdoutPath ? openSync(options.stdoutPath, "w", 0o600) : null;
    const closeStdoutFile = (): void => {
      if (stdoutFd === null) return;
      closeSync(stdoutFd);
      stdoutFd = null;
    };
    const child = spawn(command, args, {
      shell: false,
      stdio: ["pipe", stdoutFd ?? "pipe", "pipe"],
      cwd: options.cwd,
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);

    const append = (current: Buffer<ArrayBufferLike>, chunk: Buffer<ArrayBufferLike>): Buffer<ArrayBufferLike> => {
      const next = current.length + chunk.length;
      if (next > maxOutputBytes) {
        overflowed = true;
        child.kill("SIGKILL");
        return current;
      }
      return Buffer.concat([current, chunk]);
    };

    child.stdout?.on("data", (chunk: Buffer) => { stdout = append(stdout, chunk); });
    child.stderr?.on("data", (chunk: Buffer) => { stderr = append(stderr, chunk); });
    child.on("error", (error) => {
      clearTimeout(timer);
      closeStdoutFile();
      reject(new BridgeError("COMMAND_FAILED", `Could not run ${command}: ${error.message}`));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      closeStdoutFile();
      if (options.stdoutPath) {
        try {
          if (statSync(options.stdoutPath).size > maxOutputBytes) overflowed = true;
          else stdout = readFileSync(options.stdoutPath);
        } catch (error) {
          const message = error instanceof Error ? error.message : "unknown error";
          reject(new BridgeError("COMMAND_FAILED", `Could not read ${command} output: ${message}`));
          return;
        }
      }
      if (timedOut) {
        reject(new BridgeError("COMMAND_TIMEOUT", `${command} exceeded ${timeoutMs}ms`));
      } else if (overflowed) {
        reject(new BridgeError("COMMAND_FAILED", `${command} produced too much output`));
      } else if (code !== 0) {
        const detail = stderr.toString("utf8").trim();
        reject(new BridgeError("COMMAND_FAILED", `${command} exited with ${code}${detail ? `: ${detail.slice(0, 500)}` : ""}`));
      } else {
        resolve({ stdout: stdout.toString("utf8"), stderr: stderr.toString("utf8") });
      }
    });
    if (options.input !== undefined) child.stdin?.end(options.input, "utf8");
    else child.stdin?.end();
  });
}
