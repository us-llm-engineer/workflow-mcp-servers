export type TranscriptItem =
  | { kind: "chat"; role: "user" | "assistant"; text: string; text_truncated?: boolean }
  | {
      kind: "tool";
      name: string;
      input: unknown;
      status: string;
      output_preview: string | null;
      output_truncated: boolean;
    };

/** The agent product named by the `session_id` prefix. */
export type AgentTool = "opencode" | "codex" | "claude";

/** A session reference resolved to one native session inside one folder. */
export interface ResolvedSession {
  tool: AgentTool;
  /** Native ID: OpenCode `ses_…`, Codex thread UUID, Claude Code session UUID. */
  sessionId: string;
  /** Canonical (realpath) working folder of the session. */
  folder: string;
  /** Human session name/title when known. */
  name: string | null;
  /** Absolute transcript file for Codex/Claude; null for OpenCode or when not yet written. */
  transcriptPath: string | null;
  /** Older sessions in the same folder that share this session's name; this one is the most recent. */
  olderSessionIds?: string[];
}

/** How a message was handed to a live interface. */
export interface Delivery {
  transport: "opencode-tui" | "tmux" | "codex-app-server" | "claude-channel";
  pid: number;
  /** Terminal of the receiving process; null when delivery went through a shared server. */
  tty: string | null;
  /** tmux pane id such as `%3` for tmux transport, otherwise null. */
  pane: string | null;
  /** Live windows of older same-named sessions, or older duplicate windows of this session. Reported, never closed. */
  older_windows?: OlderWindow[];
  /** Something the caller should know about how the message was routed. */
  note?: string;
  /** Codex only: whether the message started running or is still waiting in Codex's queue. */
  queue?: {
    state: "started" | "waiting_for_current_turn" | "queued_not_started";
    submission_id: string | null;
    ahead: number;
    error?: string;
  };
}

/** A live window the bridge did not use because a more recent one exists. */
export interface OlderWindow {
  pid: number;
  tty: string | null;
  session_id: string;
  reason: "older_session_with_same_name" | "duplicate_window";
}
