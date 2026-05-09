// Centralized HTTP + WS client. Vite dev proxy forwards /api and /ws to
// the FastAPI backend, so no base URL is needed in dev. In Tauri/prod
// the backend serves the SPA itself, also same-origin.
import type {
  AgentStatus,
  AutonomousRun,
  BusEvent,
  ChatRetryConfig,
  ChatWSIn,
  ChatWSOut,
  Conversation,
  ConversationSummary,
  EmailConfig,
  LLMInfo,
  LLMTestResult,
  MyKeyBackup,
  MyKeyData,
  MyKeySession,
  MyKeyWriteResult,
  ReportItem,
  Schedule,
  ScheduleType,
  SessionSnapshot,
  TaskRun,
  TaskSchedule,
  TaskScheduleType,
  SetupStatus,
  SOPItem,
  SkillItem,
  SkillSearchResult,
  UploadResult,
  WxContact,
  WxLogEntry,
  WxStatus,
} from './types'

class HttpError extends Error {
  status: number
  body: any
  constructor(status: number, body: any) {
    super(typeof body === 'string' ? body : JSON.stringify(body))
    this.status = status
    this.body = body
  }
}

async function http<T>(method: string, path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    ...init,
  })
  if (!res.ok) {
    let msg: any = await res.text()
    try { msg = JSON.parse(msg) } catch {}
    throw new HttpError(res.status, msg)
  }
  if (res.status === 204) return undefined as unknown as T
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json() as Promise<T>
  return res.text() as unknown as T
}

export const api = {
  // ── status ───────────────────────────────────────────
  status: () => http<{ configured: boolean; ga_root: string | null; python_path?: string | null; resolved_python?: string | null; resolved_python_source?: string; mode?: string; agent?: AgentStatus; wechat?: WxStatus; autonomous?: any }>('GET', '/api/status'),

  // ── setup (always available, even in setup mode) ────
  setupStatus: () => http<SetupStatus>('GET', '/api/setup/status'),
  setupValidate: (ga_root: string) =>
    http<{ valid: boolean; resolved: string }>('POST', '/api/setup/validate', { ga_root }),
  setupSave: (ga_root: string, python_path?: string) =>
    http<{ ok: boolean; ga_root: string; python_path: string | null; resolved_python: string | null; resolved_python_source: string; restart_required: boolean }>(
      'POST',
      '/api/setup/save',
      { ga_root, python_path },
    ),

  // ── agent ────────────────────────────────────────────
  agentStatus: () => http<AgentStatus>('GET', '/api/agent/status'),
  agentAbort: () => http<{ ok: boolean }>('POST', '/api/agent/abort'),
  agentNew: () => http<{ ok: boolean; message: string }>('POST', '/api/agent/new'),
  chatRetryConfig: () => http<ChatRetryConfig>('GET', '/api/agent/chat-retry-config'),
  saveChatRetryConfig: (cfg: ChatRetryConfig) =>
    http<ChatRetryConfig>('PUT', '/api/agent/chat-retry-config', cfg),
  agentSessions: () => http<{ sessions: SessionSnapshot[] }>('GET', '/api/agent/sessions'),
  agentRestoreSession: (idx: number) =>
    http<{ ok: boolean; message: string; full: boolean }>('POST', `/api/agent/sessions/${idx}/restore`),

  // ── llms ─────────────────────────────────────────────
  llms: () => http<{ llms: LLMInfo[] }>('GET', '/api/llms'),
  switchLLM: (index: number) => http<{ llm_no: number; name: string }>('POST', '/api/llms/switch', { index }),
  testLLM: (index: number) => http<LLMTestResult>('POST', `/api/llms/${index}/test`),

  // ── desktop notifications (backend OS notifier) ──────
  notifyInfo: () => http<{ backend: string }>('GET', '/api/notify/info'),
  notify: (title: string, body: string) =>
    http<{ ok: boolean; backend: string; throttled?: boolean; error?: string }>('POST', '/api/notify', { title, body }),

  // ── mykey.py editor ──────────────────────────────────
  mykey: () => http<MyKeyData>('GET', '/api/mykey'),
  putMyKeyRaw: (raw: string) => http<MyKeyWriteResult>('PUT', '/api/mykey/raw', { raw }),
  upsertMyKeySession: (s: MyKeySession) =>
    http<MyKeyWriteResult>('POST', '/api/mykey/sessions', s),
  deleteMyKeySession: (varName: string) =>
    http<MyKeyWriteResult>('DELETE', `/api/mykey/sessions/${encodeURIComponent(varName)}`),
  mykeyBackups: () => http<{ backups: MyKeyBackup[] }>('GET', '/api/mykey/backups'),
  restoreMyKeyBackup: (name: string) =>
    http<MyKeyWriteResult>('POST', `/api/mykey/backups/${encodeURIComponent(name)}/restore`),

  // ── wechat ───────────────────────────────────────────
  wxStatus: () => http<WxStatus>('GET', '/api/wechat/status'),
  wxLogin: () => http<any>('POST', '/api/wechat/login'),
  wxLogout: () => http<{ ok: boolean }>('POST', '/api/wechat/logout'),
  wxStartPoll: () => http<{ started: boolean }>('POST', '/api/wechat/poll/start'),
  wxStopPoll: () => http<{ ok: boolean }>('POST', '/api/wechat/poll/stop'),
  wxContacts: () => http<{ contacts: WxContact[] }>('GET', '/api/wechat/contacts'),
  wxMessages: (uid?: string, limit = 200) => {
    const q = new URLSearchParams()
    if (uid) q.set('uid', uid)
    q.set('limit', String(limit))
    return http<{ messages: WxLogEntry[] }>('GET', `/api/wechat/messages?${q}`)
  },
  wxSend: (uid: string, text?: string, file_path?: string, context_token = '') =>
    http<{ ok: boolean }>('POST', '/api/wechat/send', { uid, text, file_path, context_token }),
  wxClearMessages: () => http<{ ok: boolean }>('DELETE', '/api/wechat/messages'),
  wxAllowlist: () => http<{ allowlist: string[] }>('GET', '/api/wechat/allowlist'),
  wxSetAllowlist: (allowlist: string[]) =>
    http<{ ok: boolean; allowlist: string[] }>('PUT', '/api/wechat/allowlist', { allowlist }),

  // ── conversations ────────────────────────────────────
  conversations: (q?: string, offset = 0, limit = 50) => {
    const sp = new URLSearchParams()
    if (q) sp.set('q', q)
    sp.set('offset', String(offset))
    sp.set('limit', String(limit))
    return http<{ total: number; offset: number; limit: number; items: ConversationSummary[] }>(
      'GET', `/api/conversations?${sp}`)
  },
  conversation: (id: string) => http<Conversation>('GET', `/api/conversations/${id}`),
  renameConversation: (id: string, title: string) =>
    http<{ ok: boolean }>('PATCH', `/api/conversations/${id}`, { title }),
  deleteConversation: (id: string) => http<{ ok: boolean }>('DELETE', `/api/conversations/${id}`),
  exportConversation: (id: string, format: 'md' | 'json') =>
    `/api/conversations/${id}/export?format=${format}`,
  restoreConversation: (id: string) =>
    http<{ ok: boolean; restored_lines: number; title: string; id: string }>('POST', `/api/conversations/${id}/restore`),

  // ── memory ───────────────────────────────────────────
  globalMem: () => http<{ content: string }>('GET', '/api/memory/global'),
  setGlobalMem: (content: string) => http<{ ok: boolean }>('PUT', '/api/memory/global', { content }),
  insight: () => http<{ content: string }>('GET', '/api/memory/insight'),
  setInsight: (content: string) => http<{ ok: boolean }>('PUT', '/api/memory/insight', { content }),
  sops: () => http<{ sops: SOPItem[] }>('GET', '/api/memory/sops'),
  sop: (name: string) => http<{ name: string; content: string }>('GET', `/api/memory/sops/${encodeURIComponent(name)}`),
  setSop: (name: string, content: string) =>
    http<{ ok: boolean }>('PUT', `/api/memory/sops/${encodeURIComponent(name)}`, { content }),
  skills: (limit = 200) => http<{ skills: SkillItem[]; count: number }>('GET', `/api/memory/skills?limit=${limit}`),
  skill: (path: string) => http<{ path: string; content: string }>('GET', `/api/memory/skills/read?path=${encodeURIComponent(path)}`),
  searchSkills: (q: string, limit = 60) =>
    http<SkillSearchResult>('GET', `/api/memory/skills/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  // ── autonomous ───────────────────────────────────────
  schedules: () => http<{ schedules: Schedule[] }>('GET', '/api/autonomous/schedules'),
  upsertSchedule: (s: Partial<Schedule> & { type: ScheduleType }) =>
    http<Schedule>('POST', '/api/autonomous/schedules', s),
  deleteSchedule: (id: string) => http<{ ok: boolean }>('DELETE', `/api/autonomous/schedules/${id}`),
  triggerSchedule: (id: string) => http<{ run_id: string; stream_id: string }>('POST', `/api/autonomous/schedules/${id}/trigger`),
  runs: (limit = 100) => http<{ runs: AutonomousRun[] }>('GET', `/api/autonomous/runs?limit=${limit}`),
  reports: () => http<{ reports: ReportItem[] }>('GET', '/api/autonomous/reports'),
  report: (name: string) => http<{ name: string; content: string }>('GET', `/api/autonomous/reports/${encodeURIComponent(name)}`),

  // ── scheduled tasks ──────────────────────────────────
  taskSchedules: () => http<{ schedules: TaskSchedule[] }>('GET', '/api/tasks/schedules'),
  upsertTaskSchedule: (s: Partial<TaskSchedule> & { type: TaskScheduleType }) =>
    http<TaskSchedule>('POST', '/api/tasks/schedules', s),
  deleteTaskSchedule: (id: string) => http<{ ok: boolean }>('DELETE', `/api/tasks/schedules/${id}`),
  triggerTaskSchedule: (id: string) => http<{ run_id: string; stream_id: string }>('POST', `/api/tasks/schedules/${id}/trigger`),
  taskRuns: (limit = 100) => http<{ runs: TaskRun[] }>('GET', `/api/tasks/runs?limit=${limit}`),
  taskEmailConfig: () => http<EmailConfig>('GET', '/api/tasks/email-config'),
  saveTaskEmailConfig: (cfg: Partial<EmailConfig> & { password?: string }) =>
    http<EmailConfig>('PUT', '/api/tasks/email-config', cfg),
  testTaskEmail: (to: string, subject: string, body: string) =>
    http<{ ok: boolean; to: string; error?: string }>('POST', '/api/tasks/email-test', { to, subject, body }),

  // ── upload ───────────────────────────────────────────
  upload: async (file: File): Promise<UploadResult> => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch('/api/upload', { method: 'POST', body: fd })
    if (!res.ok) throw new HttpError(res.status, await res.text())
    return res.json()
  },
  fileUrlByPath: (absPath: string) => `/api/files-by-path?path=${encodeURIComponent(absPath)}`,

  // ── logs ─────────────────────────────────────────────
  wechatLog: (tail = 200) => http<{ lines: string[] }>('GET', `/api/logs/wechat?tail=${tail}`),
  agentLog: (tail = 200) => http<{ lines: string[]; file: string | null }>('GET', `/api/logs/agent?tail=${tail}`),
}

// ── WebSocket helpers ──────────────────────────────────────
function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${path}`
}

export class ChatSocket {
  ws?: WebSocket
  private readonly url = wsUrl('/ws/chat')
  private reconnectTimer?: number
  private explicitlyClosed = false
  onMessage: (m: ChatWSOut) => void = () => {}
  onState: (s: 'connecting' | 'open' | 'closed') => void = () => {}

  open() {
    this.explicitlyClosed = false
    this.onState('connecting')
    const ws = new WebSocket(this.url)
    this.ws = ws
    ws.onopen = () => this.onState('open')
    ws.onmessage = (ev) => {
      try { this.onMessage(JSON.parse(ev.data) as ChatWSOut) } catch {}
    }
    ws.onclose = () => {
      this.onState('closed')
      if (!this.explicitlyClosed) {
        this.reconnectTimer = window.setTimeout(() => this.open(), 2000)
      }
    }
    ws.onerror = () => { try { ws.close() } catch {} }
  }

  send(msg: ChatWSIn) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg))
  }

  close() {
    this.explicitlyClosed = true
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
    this.ws?.close()
  }
}

export class EventSocket {
  private ws?: WebSocket
  private readonly url: string
  private reconnectTimer?: number
  private explicitlyClosed = false
  onEvent: (e: BusEvent) => void = () => {}

  constructor(prefix = '', replay = 0) {
    this.url = wsUrl(`/ws/events?prefix=${encodeURIComponent(prefix)}&replay=${replay}`)
  }

  open() {
    this.explicitlyClosed = false
    const ws = new WebSocket(this.url)
    this.ws = ws
    ws.onmessage = (ev) => {
      try { this.onEvent(JSON.parse(ev.data) as BusEvent) } catch {}
    }
    ws.onclose = () => {
      if (!this.explicitlyClosed) {
        this.reconnectTimer = window.setTimeout(() => this.open(), 2000)
      }
    }
    ws.onerror = () => { try { ws.close() } catch {} }
  }

  close() {
    this.explicitlyClosed = true
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
    this.ws?.close()
  }
}
