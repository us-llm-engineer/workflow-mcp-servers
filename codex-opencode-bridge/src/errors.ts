export type BridgeErrorCode =
  | "INVALID_ARGUMENT"
  | "INVALID_ID"
  | "INVALID_FOLDER"
  | "SESSION_NOT_FOUND"
  | "AMBIGUOUS_SESSION"
  | "FOLDER_MISMATCH"
  | "TRANSCRIPT_NOT_FOUND"
  | "AMBIGUOUS_TRANSCRIPT"
  | "AMBIGUOUS_BINDING"
  | "TARGET_NOT_RUNNING"
  | "NOT_IN_TMUX"
  | "CHANNEL_NOT_ENABLED"
  | "TUI_CONTROL_UNAVAILABLE"
  | "COMMAND_FAILED"
  | "COMMAND_TIMEOUT"
  | "TRANSCRIPT_SCHEMA_UNSUPPORTED";

export class BridgeError extends Error {
  constructor(public readonly code: BridgeErrorCode, message: string) {
    super(message);
    this.name = "BridgeError";
  }
}

export function asBridgeError(error: unknown): BridgeError {
  if (error instanceof BridgeError) return error;
  const message = error instanceof Error ? error.message : "Unexpected bridge failure";
  return new BridgeError("COMMAND_FAILED", message);
}
