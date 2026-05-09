// chatStore — process-wide chat state.
//
// Why a global store rather than LiveChat-local state?
//   1. Switching tabs (LiveChat ⇆ Wechat ⇆ Conversations …) used to unmount
//      LiveChat, killing both the WebSocket and msg state. Now the socket
//      lives at the App level and msgs survive navigation.
//   2. Autonomous-evolution / wechat / reflect submissions are routed
//      through the SAME bus channel in the backend. The single subscription
//      here surfaces them in the live chat view as new conversation threads.
//
// State model:
//   - msgs is an ordered list of UI bubbles. Each assistant bubble carries
//     its `streamId` so successive {next} chunks can mutate the same
//     bubble. User bubbles do too (so we can disambiguate which user
//     prompt each assistant reply belongs to). webui-source bubbles are
//     pre-added on submit (with image previews) and adopt the streamId
//     when the matching `started` event arrives.

import { create } from 'zustand'
import type { ChatStreamSnapshot, ChatWSOut } from '@/api/types'
import { ChatSocket } from '@/api/client'
import type { PasteAttachment } from '@/components/ImagePasteInput'

export type ChatMsgRole = 'user' | 'assistant' | 'system'

export interface ChatMsg {
  role: ChatMsgRole
  content: string
  streamId?: string                // matched stream (omitted for system notes)
  source?: string                  // 'user' | 'webui' | 'autonomous' | 'wechat' | 'reflect' | …
  streaming?: boolean              // assistant bubble currently receiving
  attachments?: PasteAttachment[]  // local-only previews for the user bubble
  pendingWebui?: boolean           // set on local pre-add until `started` arrives
}

interface ChatState {
  msgs: ChatMsg[]
  conn: 'connecting' | 'open' | 'closed'
  streaming: boolean              // true if any stream still receiving
  hydrating: boolean              // true between connect and first snapshot apply
  sock: ChatSocket | null

  start: () => void
  stop: () => void

  /** Submit a user message via webui. Pre-adds a local user bubble so the
   *  attachment thumbnails show immediately. The bubble's streamId is
   *  filled in when the `started` event echoes back. */
  submitWebui: (text: string, atts: PasteAttachment[]) => void
  abort: () => void

  /** Wipe local view (used by /new). Doesn't talk to the server. */
  clearLocal: () => void
  /** Push a system / banner bubble (e.g. /new ack, LLM switched, restore notice). */
  pushSystem: (content: string) => void
  /** Clear stale local streaming locks when the backend is already idle. */
  markIdle: () => void
}

// LiveChat shows the user's own webui session + admin-side flows
// (autonomous evolution, reflect, /llm internal). It does NOT show the
// wechat bot — those live in the dedicated WechatBot page so the two
// channels don't pollute each other.
const HIDDEN_SOURCES = new Set(['wechat'])
const isHiddenSource = (s?: string) => !!s && HIDDEN_SOURCES.has(s)

/** Build the UI msg list from a server snapshot — used on (re)connect. */
function applySnapshot(streams: ChatStreamSnapshot[]): ChatMsg[] {
  const out: ChatMsg[] = []
  const seenRetryNotices = new Set<string>()
  for (const s of streams) {
    if (isHiddenSource(s.source)) continue
    if (s.source === 'chat_error_retry') {
      const attempt = s.retry_attempt || 0
      const noticeKey = `${s.logical_id || s.stream_id}:${attempt}`
      if (!seenRetryNotices.has(noticeKey)) {
        seenRetryNotices.add(noticeKey)
        out.push({
          role: 'assistant',
          content: `_自动重试请求${s.done ? '已完成' : '进行中'}（${attempt || '?'}${s.retry_max ? `/${s.retry_max}` : ''}${s.retry_reason ? ` · ${s.retry_reason}` : ''}）。_`,
          streamId: `${s.stream_id}:retry-snapshot`,
          source: 'chat_error_retry_notice',
        })
      }
      if (s.content || !s.done) {
        out.push({
          role: 'assistant',
          content: s.content,
          streamId: s.stream_id,
          source: s.source,
          streaming: !s.done,
        })
      }
      continue
    }
    if (s.query) {
      out.push({
        role: 'user',
        content: s.query,
        streamId: s.stream_id,
        source: s.source,
      })
    }
    if (s.content || !s.done) {
      out.push({
        role: 'assistant',
        content: s.content,
        streamId: s.stream_id,
        source: s.source,
        streaming: !s.done,
      })
    }
  }
  return out
}

/** Apply a single server event to the message list. */
function applyEvent(prev: ChatMsg[], evt: ChatWSOut): ChatMsg[] {
  if (evt.type === 'snapshot') {
    return applySnapshot(evt.streams)
  }
  if (evt.type === 'reset') {
    // Server-driven wipe (new conversation / session restore).
    return []
  }
  if (evt.type === 'started') {
    const sid = evt.stream_id
    const source = evt.source ?? 'user'
    const query = evt.query ?? ''
    if (isHiddenSource(source)) return prev
    const retryAttempt = evt.retry_attempt ?? 0
    if (source === 'chat_error_retry') {
      const retryKey = evt.logical_id || evt.retry_of || sid
      const note = `_自动重试请求已开始（${retryAttempt || '?'}${evt.retry_max ? `/${evt.retry_max}` : ''}${evt.retry_reason ? ` · ${evt.retry_reason}` : ''}）。_`
      const noticeId = `${retryKey}:retry:${retryAttempt}`
      const next = prev.filter((m) => m.streamId !== noticeId)
      return [
        ...next,
        { role: 'assistant', content: note, streamId: noticeId, source: 'chat_error_retry_notice' },
        { role: 'assistant', content: '', streamId: sid, source, streaming: true },
      ]
    }
    // 1. If our local pre-add bubble is still pending and source is webui,
    //    adopt this stream_id rather than creating a duplicate.
    if (source === 'webui') {
      const idx = [...prev].reverse().findIndex((m) => m.role === 'user' && m.pendingWebui)
      if (idx !== -1) {
        const realIdx = prev.length - 1 - idx
        const adopted = prev.slice()
        adopted[realIdx] = { ...adopted[realIdx], streamId: sid, pendingWebui: false }
        // Append empty assistant bubble for the streaming reply.
        return [...adopted, { role: 'assistant', content: '', streamId: sid, source, streaming: true }]
      }
    }
    // 2. Fresh stream from another source (or a webui submission whose pre-add
    //    is missing — happens after a tab reload). Add a user + assistant pair.
    const next = prev.slice()
    if (!next.some((m) => m.role === 'user' && m.streamId === sid)) {
      next.push({ role: 'user', content: query, streamId: sid, source })
    }
    if (!next.some((m) => m.role === 'assistant' && m.streamId === sid)) {
      next.push({ role: 'assistant', content: '', streamId: sid, source, streaming: true })
    }
    return next
  }
  if (evt.type === 'next') {
    const sid = evt.stream_id
    if (isHiddenSource(evt.source)) return prev
    const next = ensureRetryStartNotice(prev, evt)
    const idx = next.findIndex((m) => m.role === 'assistant' && m.streamId === sid)
    if (idx === -1) {
      // started not yet seen — create on the fly
      return [...next, { role: 'assistant', content: evt.content, streamId: sid, source: evt.source, streaming: true }]
    }
    const updated = next.slice()
    updated[idx] = { ...updated[idx], content: evt.content, streaming: true }
    return updated
  }
  if (evt.type === 'done') {
    const sid = evt.stream_id
    if (isHiddenSource(evt.source)) return prev
    const next = ensureRetryStartNotice(prev, evt)
    const idx = next.findIndex((m) => m.role === 'assistant' && m.streamId === sid)
    if (idx === -1) {
      return [...next, { role: 'assistant', content: evt.content, streamId: sid, source: evt.source, streaming: false }]
    }
    const updated = next.slice()
    updated[idx] = { ...updated[idx], content: evt.content, streaming: false }
    return updated
  }
  if (evt.type === 'retry') {
    if (isHiddenSource(evt.source)) return prev
    const reason = evt.reason?.label || evt.retry_reason || '可恢复错误'
    const noticeId = `${evt.logical_id || evt.stream_id}:retry:${evt.attempt}`
    const next = prev.filter((m) => m.streamId !== noticeId)
    return [
      ...next,
      {
        role: 'assistant',
        content: `_检测到 ${reason}，正在自动重试（${evt.attempt}/${evt.max_attempts}）。_`,
        streamId: noticeId,
        source: 'chat_error_retry_notice',
      },
    ]
  }
  if (evt.type === 'retry_exhausted') {
    if (isHiddenSource(evt.source)) return prev
    const reason = evt.reason?.label || evt.retry_reason || '可恢复错误'
    return [
      ...prev,
      {
        role: 'assistant',
        content: `_检测到 ${reason}，但自动重试已达到上限（${evt.max_attempts}/${evt.max_attempts}）。_`,
        streamId: `${evt.stream_id}:retry-exhausted`,
        source: 'chat_error_retry_notice',
      },
    ]
  }
  if (evt.type === 'aborted') {
    // Mark every still-streaming bubble as finished — server confirmed abort.
    return prev.map((m) => (m.streaming ? { ...m, streaming: false } : m))
  }
  return prev
}

function ensureRetryStartNotice(prev: ChatMsg[], evt: ChatWSOut): ChatMsg[] {
  if (evt.type !== 'next' && evt.type !== 'done') return prev
  if (evt.source !== 'chat_error_retry') return prev
  const attempt = evt.retry_attempt ?? 0
  const noticeId = `${evt.logical_id || evt.retry_of || evt.stream_id}:retry:${attempt}`
  if (prev.some((m) => m.streamId === noticeId)) return prev
  const note = `_自动重试请求已开始（${attempt || '?'}${evt.retry_max ? `/${evt.retry_max}` : ''}${evt.retry_reason ? ` · ${evt.retry_reason}` : ''}）。_`
  return [
    ...prev,
    { role: 'assistant', content: note, streamId: noticeId, source: 'chat_error_retry_notice' },
  ]
}

function anyStreaming(msgs: ChatMsg[]): boolean {
  return msgs.some((m) => m.streaming)
}

export const useChatStore = create<ChatState>((set, get) => ({
  msgs: [],
  conn: 'connecting',
  streaming: false,
  hydrating: true,
  sock: null,

  start: () => {
    if (get().sock) return

    // Coalesce chat:next bursts. Background:
    //   When the agent streams a long markdown answer, the backend emits a
    //   {type:'next', content: <cumulative-so-far>} every ~50 ms. Without
    //   throttling, each one triggers set() → React re-render →
    //   ReactMarkdown re-parses the entire (growing) bubble. Past ~50 KB
    //   the WKWebView renderer falls behind its GPU watchdog, the WebKit
    //   process is killed, pywebview reload-recovers the URL, the new tab
    //   reconnects → snapshot replays the same in-flight stream → crashes
    //   again. From the user's POV the connection-status badge cycles
    //   "连接中…/断开" and 停止/LLM-切换 buttons are unclickable because
    //   React never reaches an idle frame.
    //
    //   Strategy: leading-edge + trailing flush, 100 ms quiet window.
    //   First next of a quiet period applies immediately so streaming
    //   feels live. Subsequent ones in the next 100 ms are merged
    //   keyed by stream_id — content is cumulative so we keep only
    //   the newest. Non-next events (snapshot/started/done/aborted/reset)
    //   flush pending nexts first then apply, ensuring 'done' always
    //   lands AFTER the latest visible content.
    const pendingNext: Map<string, ChatWSOut & { type: 'next' }> = new Map()
    let nextTimer: number | null = null
    let lastFlush = 0
    const FLUSH_MS = 100

    const flushNext = () => {
      if (nextTimer != null) {
        window.clearTimeout(nextTimer)
        nextTimer = null
      }
      if (pendingNext.size === 0) return
      const evts = Array.from(pendingNext.values())
      pendingNext.clear()
      lastFlush = Date.now()
      set((st) => {
        let msgs = st.msgs
        for (const e of evts) msgs = applyEvent(msgs, e)
        return { msgs, streaming: anyStreaming(msgs) }
      })
    }

    const sock = new ChatSocket()
    sock.onState = (s) => {
      set({ conn: s })
      if (s === 'connecting') set({ hydrating: true })
    }
    sock.onMessage = (m) => {
      // Snapshot is large; defer past the next paint so the WebView
      // becomes interactive first (preserved from prior behaviour).
      if (m.type === 'snapshot') {
        // Drop any in-flight next throttle — the snapshot is the
        // authoritative state.
        pendingNext.clear()
        if (nextTimer != null) { window.clearTimeout(nextTimer); nextTimer = null }
        const apply = () => set((st) => {
          const msgs = applyEvent(st.msgs, m)
          return { msgs, streaming: anyStreaming(msgs), hydrating: false }
        })
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(() => requestAnimationFrame(apply))
        } else {
          setTimeout(apply, 0)
        }
        return
      }

      if (m.type === 'next') {
        pendingNext.set(m.stream_id, m)
        const since = Date.now() - lastFlush
        if (since >= FLUSH_MS) {
          flushNext()
        } else if (nextTimer == null) {
          nextTimer = window.setTimeout(flushNext, FLUSH_MS - since)
        }
        return
      }

      // Any other event (started / done / aborted / reset / error / pong):
      // flush queued next first so done's final content lands AFTER the
      // most recent streaming chunk, not before.
      if (pendingNext.size > 0) flushNext()
      set((st) => {
        const msgs = applyEvent(st.msgs, m)
        return { msgs, streaming: anyStreaming(msgs) }
      })
    }
    sock.open()
    set({ sock })
  },

  stop: () => {
    get().sock?.close()
    set({ sock: null, conn: 'closed' })
  },

  submitWebui: (text, atts) => {
    const sock = get().sock
    if (!sock) return
    // Local pre-add: user bubble carries the attachments so MessageBubble
    // can render thumbnails. We deliberately keep the *visible* text clean
    // — the prompt-engineering tokens (FILE_HINT / [用户发送文件:...]) only
    // go to the LLM, not the bubble.
    const userBubble: ChatMsg = {
      role: 'user',
      content: text,
      source: 'webui',
      attachments: atts.length ? atts : undefined,
      pendingWebui: true,
    }
    set((st) => ({ msgs: [...st.msgs, userBubble], streaming: true }))

    // Build the actual prompt: text + file markers (matches wechatapp.py convention)
    const fileMarkers = atts.map((a) => `[用户发送文件: ${a.path}]`).join('\n')
    const fileHint = atts.length
      ? 'If you need to show files to user, use [FILE:filepath] in your response.\n\n'
      : ''
    const promptText = fileHint + text + (fileMarkers ? (text ? '\n' : '') + fileMarkers : '')
    sock.send({
      type: 'submit',
      text: promptText,
      images: atts.map((a) => a.path),
      source: 'webui',
    })
  },

  abort: () => {
    get().sock?.send({ type: 'abort' })
  },

  clearLocal: () => set({ msgs: [], streaming: false }),
  pushSystem: (content) =>
    set((st) => ({ msgs: [...st.msgs, { role: 'assistant', content, source: 'system' }] })),
  markIdle: () =>
    set((st) => ({
      msgs: st.msgs.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
      streaming: false,
    })),
}))
