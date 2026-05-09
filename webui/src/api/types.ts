// Types mirror server/schemas.py and service dataclasses.

// ── Setup ─────────────────────────────────────────────────
export interface SetupCandidate {
  path: string
  valid: boolean
}

export interface SetupStatus {
  configured: boolean
  ga_root: string | null
  python_path: string | null
  resolved_python: string | null
  resolved_python_source: string
  admin_data: string
  candidates: SetupCandidate[]
}

export interface AgentStatus {
  is_running: boolean
  llm_no: number
  llm_name: string
  llm_model: string
  last_reply_time: number
  queued_tasks: number
  history_lines: number
}

export interface LLMInfo {
  index: number
  name: string
  current: boolean
}

export interface LLMTestResult {
  ok: boolean
  latency_ms?: number
  preview?: string
  model?: string
  name?: string
  error?: string
}

export interface SkillSearchHit {
  path: string
  matches: Array<{ line: number; text: string }>
}
export interface SkillSearchResult {
  hits: SkillSearchHit[]
  scanned: number
  truncated: boolean
  query: string
}

export interface SessionSnapshot {
  path: string
  mtime: number
  preview: string
  rounds: number
}

export interface ChatRetryConfig {
  enabled: boolean
  max_attempts: number
}

// ── WeChat ────────────────────────────────────────────────
export interface WxQRState {
  status: string                // idle | waiting_scan | scanning | confirmed | expired | timeout | error
  qrcode_id?: string
  url?: string
  bot_id?: string
  error?: string
}

export interface WxStatus {
  logged_in: boolean
  bot_id: string
  polling: boolean
  qr: WxQRState
  contacts: number
  allowlist: string[]
  log_count: number
}

export interface WxContact {
  uid: string
  last_text: string
  last_ts: number
  msg_count: number
  nickname: string
}

export interface WxLogEntry {
  ts: number
  direction: 'in' | 'out'
  uid: string
  text: string
  media: string[]
  context_token: string
  nickname?: string
}

// ── Conversations ─────────────────────────────────────────
export interface ConversationSummary {
  id: string
  title: string
  message_count: number
  last_user_preview: string
}

export interface Message {
  role: 'user' | 'assistant' | 'system' | string
  content: string
}

export interface Conversation {
  id: string
  title: string
  messages: Message[]
}

// ── Memory / SOPs ─────────────────────────────────────────
export interface SOPItem { name: string; size: number; mtime: number }
export interface SkillItem { path: string; name: string; size: number; mtime: number }

// ── Autonomous ────────────────────────────────────────────
export type ScheduleType = 'idle' | 'cron' | 'interval'

export interface Schedule {
  id: string
  type: ScheduleType
  enabled: boolean
  prompt: string
  idle_minutes: number
  cron: string
  interval_minutes: number
  last_fired_at: number
  fire_count: number
  name: string
}

export interface AutonomousRun {
  id: string
  schedule_id: string
  fired_at: number
  prompt_preview: string
  report_paths: string[]
  note: string
}

export interface ReportItem { name: string; size: number; mtime: number }

// ── Scheduled Tasks ───────────────────────────────────────
export type TaskScheduleType = 'cron' | 'interval'

export interface TaskSchedule {
  id: string
  type: TaskScheduleType
  enabled: boolean
  prompt: string
  cron: string
  interval_minutes: number
  notify_email: boolean
  email_to: string
  email_subject: string
  last_fired_at: number
  fire_count: number
  name: string
}

export interface TaskRun {
  id: string
  task_id: string
  task_name: string
  fired_at: number
  stream_id: string
  finished_at: number
  status: 'running' | 'done' | 'error' | 'timeout' | string
  prompt_preview: string
  result_preview: string
  email_sent: boolean
  email_error: string
  note: string
}

export interface EmailConfig {
  host: string
  port: number
  username: string
  from_addr: string
  default_to: string
  use_tls: boolean
  use_ssl: boolean
  password_set: boolean
}

// ── Upload ────────────────────────────────────────────────
export interface UploadResult {
  file_id: string
  name: string
  path: string
  url: string
  mime: string
  size: number
}

// ── mykey.py editor ──────────────────────────────────────
export type MyKeySessionType = 'native_claude' | 'native_oai' | 'claude' | 'oai' | 'mixin'

export interface MyKeySession {
  var: string
  type: MyKeySessionType
  fields: Record<string, any>     // apikey omitted; apikey_masked present
  lineno?: number
  end_lineno?: number
}

export interface MyKeyData {
  path: string
  exists: boolean
  raw: string
  mtime: number
  structured: {
    sessions: MyKeySession[]
    mixin: MyKeySession | null
    globals: Record<string, any>
  }
}

export interface MyKeyWriteResult {
  ok: boolean
  backup?: string | null
  llms?: LLMInfo[]
  warnings?: string[]
  structured?: MyKeyData['structured']
  error?: string
  message?: string
  line?: number
  col?: number
}

export interface MyKeyBackup {
  name: string
  mtime: number
  size: number
}

// ── Events ────────────────────────────────────────────────
export interface BusEvent {
  topic: string
  payload: Record<string, any>
  ts: number
}

// ── Chat WS protocol ─────────────────────────────────────
export type ChatWSIn =
  | { type: 'submit'; text: string; images?: string[]; source?: string }
  | { type: 'abort' }
  | { type: 'ping' }

export interface ChatStreamSnapshot {
  stream_id: string
  source: string
  query: string
  content: string
  done: boolean
  started_at: number
  finished_at: number
  logical_id?: string
  retry_attempt?: number
  retry_max?: number
  retry_of?: string
  retry_reason?: string
}

export interface ChatRetryReason {
  code: string
  label: string
  marker: string
}

export type ChatWSOut =
  | { type: 'snapshot'; streams: ChatStreamSnapshot[] }
  | { type: 'reset'; reason?: string }
  | { type: 'started'; stream_id: string; source?: string; query?: string; ts?: number; logical_id?: string; retry_attempt?: number; retry_max?: number; retry_of?: string; retry_reason?: string }
  | { type: 'heartbeat'; stream_id: string }
  | { type: 'next'; stream_id: string; content: string; source?: string; logical_id?: string; retry_attempt?: number; retry_max?: number; retry_of?: string; retry_reason?: string }
  | { type: 'done'; stream_id: string; content: string; source?: string; logical_id?: string; retry_attempt?: number; retry_max?: number; retry_of?: string; retry_reason?: string }
  | { type: 'retry'; stream_id: string; source?: string; logical_id?: string; attempt: number; max_attempts: number; reason?: ChatRetryReason; retry_reason?: string }
  | { type: 'retry_exhausted'; stream_id: string; source?: string; logical_id?: string; attempt: number; max_attempts: number; reason?: ChatRetryReason; retry_reason?: string }
  | { type: 'aborted' }
  | { type: 'pong' }
  | { type: 'error'; error: string }
