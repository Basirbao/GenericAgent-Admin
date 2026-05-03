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
}

/** Build the UI msg list from a server snapshot — used on (re)connect. */
function applySnapshot(streams: ChatStreamSnapshot[]): ChatMsg[] {
  const out: ChatMsg[] = []
  for (const s of streams) {
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
    const idx = prev.findIndex((m) => m.role === 'assistant' && m.streamId === sid)
    if (idx === -1) {
      // started not yet seen — create on the fly
      return [...prev, { role: 'assistant', content: evt.content, streamId: sid, source: evt.source, streaming: true }]
    }
    const next = prev.slice()
    next[idx] = { ...next[idx], content: evt.content, streaming: true }
    return next
  }
  if (evt.type === 'done') {
    const sid = evt.stream_id
    const idx = prev.findIndex((m) => m.role === 'assistant' && m.streamId === sid)
    if (idx === -1) {
      return [...prev, { role: 'assistant', content: evt.content, streamId: sid, source: evt.source, streaming: false }]
    }
    const next = prev.slice()
    next[idx] = { ...next[idx], content: evt.content, streaming: false }
    return next
  }
  if (evt.type === 'aborted') {
    // Mark every still-streaming bubble as finished — server confirmed abort.
    return prev.map((m) => (m.streaming ? { ...m, streaming: false } : m))
  }
  return prev
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
    const sock = new ChatSocket()
    sock.onState = (s) => {
      set({ conn: s })
      if (s === 'connecting') set({ hydrating: true })
    }
    sock.onMessage = (m) => {
      // The very first message after connect is the chat-state snapshot. If
      // the user has been heavy-chatting, parsing + rendering it synchronously
      // blocks the WebView's main thread for ~3-5 seconds on launch ("can't
      // click for a while"). Defer applying it past the next paint so the UI
      // becomes interactive first; the restored bubbles fade in a moment
      // later. Subsequent live events stay synchronous so streaming feels
      // immediate.
      if (m.type === 'snapshot') {
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
}))
