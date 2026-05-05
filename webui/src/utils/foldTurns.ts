// Port of stapp.fold_turns: split agent stream by "**LLM Running (Turn N) ...**"
// markers into segments. Each non-final segment becomes a foldable detail
// block titled by its <summary>...</summary> if present.

export type Segment =
  | { type: 'text'; content: string }
  | { type: 'fold'; title: string; content: string }

const PH_MARK = '\u0000PH'

export function foldTurns(text: string): Segment[] {
  if (!text) return []
  const placeholders: string[] = []

  // Protect ```` blocks (4+ backticks), incl. unclosed at tail
  let safe = text.replace(/`{4,}[\s\S]*?`{4,}/g, (m) => {
    placeholders.push(m)
    return `${PH_MARK}${placeholders.length - 1}\u0000`
  })
  safe = safe.replace(/`{4,}[^`][\s\S]*$/g, (m) => {
    placeholders.push(m)
    return `${PH_MARK}${placeholders.length - 1}\u0000`
  })

  const restore = (s: string) =>
    s.replace(new RegExp(`${PH_MARK}(\\d+)\\u0000`, 'g'), (_, i) => placeholders[+i] ?? '')

  const parts = safe.split(/(\**LLM Running \(Turn \d+\) \.{3}\**)/).map(restore)
  if (parts.length < 4) return [{ type: 'text', content: text }]

  const segments: Segment[] = []
  if (parts[0].trim()) segments.push({ type: 'text', content: parts[0] })

  const turns: Array<{ marker: string; content: string }> = []
  for (let i = 1; i < parts.length; i += 2) {
    turns.push({ marker: parts[i], content: parts[i + 1] ?? '' })
  }

  turns.forEach((t, idx) => {
    if (idx < turns.length - 1) {
      // strip code blocks + thinking before searching for summary
      const cleaned = t.content
        .replace(/`{3,}[\s\S]*?`{3,}/g, '')
        .replace(/<thinking>[\s\S]*?<\/thinking>/g, '')
      const m = /<summary>\s*([\s\S]*?)\s*<\/summary>/.exec(cleaned)
      // Title shown on the closed <details>. Earlier versions split on the
      // first newline AND chopped at 80 chars with an ellipsis. Both
      // hurt: multi-line <summary> blocks lost everything past line 1,
      // and longer single-line summaries got truncated to "...". The
      // <details> already collapses, so the user only sees the title
      // when they want context — give them the full thing. CSS in
      // styles/index.css lets summary wrap to multiple lines.
      const title = m
        ? m[1].trim().split('\n').map((s) => s.trim()).filter(Boolean).join(' · ')
        : t.marker.replace(/\*+/g, '').trim()
      segments.push({ type: 'fold', title, content: t.content })
    } else {
      segments.push({ type: 'text', content: (t.marker + t.content) })
    }
  })

  return segments
}

/** Shorten very long single-line previews (used in conversation list). */
export function previewText(s: string, n = 80): string {
  const flat = (s || '').replace(/\s+/g, ' ').trim()
  return flat.length > n ? flat.slice(0, n) + '…' : flat
}

/** Format a unix epoch (seconds) as relative time, fallback to local string. */
export function relTime(ts: number): string {
  if (!ts) return ''
  const d = Math.floor(Date.now() / 1000) - ts
  if (d < 60) return `${d}秒前`
  if (d < 3600) return `${Math.floor(d / 60)}分前`
  if (d < 86400) return `${Math.floor(d / 3600)}小时前`
  if (d < 86400 * 30) return `${Math.floor(d / 86400)}天前`
  return new Date(ts * 1000).toLocaleString()
}
