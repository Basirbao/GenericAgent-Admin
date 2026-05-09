import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import cronstrue from 'cronstrue/i18n'
import { CronExpressionParser } from 'cron-parser'
import { api } from '@/api/client'
import type { EmailConfig, TaskSchedule, TaskScheduleType } from '@/api/types'
import { PageShell } from '@/components/PageShell'
import { relTime } from '@/utils/foldTurns'
import { dialog } from '@/stores/dialogStore'

export function Tasks() {
  const qc = useQueryClient()
  const { data: schData } = useQuery({ queryKey: ['tasks.schedules'], queryFn: api.taskSchedules, refetchInterval: 8000 })
  const { data: runData } = useQuery({ queryKey: ['tasks.runs'], queryFn: () => api.taskRuns(80), refetchInterval: 8000 })
  const [editor, setEditor] = useState<Partial<TaskSchedule> | null>(null)

  const schedules = schData?.schedules ?? []
  const runs = runData?.runs ?? []

  const triggerNow = async (id: string) => {
    const ok = await dialog.confirm(
      '立即触发一次定时任务？',
      '这会让 Agent 立刻执行该任务 Prompt，完成后按任务设置发送邮件。',
      { confirmText: '触发' },
    )
    if (!ok) return
    await api.triggerTaskSchedule(id)
    qc.invalidateQueries({ queryKey: ['tasks.runs'] })
  }

  return (
    <PageShell
      title="定时任务"
      description="按 cron / 周期执行 Agent Prompt，例如每天 08:00 抓取信息并邮件通知。"
      actions={
        <button
          onClick={() => setEditor({ type: 'cron', enabled: true, cron: '0 8 * * *', name: '', notify_email: false })}
          className="px-3 py-1.5 rounded-lg bg-accent text-white text-sm"
        >+ 新建任务</button>
      }
    >
      <div className="p-6 space-y-6 max-w-6xl mx-auto">
        <section>
          <h2 className="text-sm font-semibold text-slate-300 mb-3">任务计划</h2>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {schedules.map((s) => <TaskCard key={s.id} s={s} onEdit={() => setEditor(s)} onFire={() => triggerNow(s.id)} />)}
            {schedules.length === 0 && <div className="text-slate-500 text-sm">尚无定时任务</div>}
          </div>
        </section>

        <EmailSettings />

        <section>
          <h2 className="text-sm font-semibold text-slate-300 mb-3">运行历史</h2>
          <div className="rounded-xl border border-line bg-bg-card overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-bg-soft text-xs text-slate-400">
                <tr>
                  <th className="text-left p-2.5">时间</th>
                  <th className="text-left p-2.5">任务</th>
                  <th className="text-left p-2.5">状态</th>
                  <th className="text-left p-2.5">结果</th>
                  <th className="text-left p-2.5">邮件</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} className="border-t border-line/60 align-top">
                    <td className="p-2.5 text-slate-400 whitespace-nowrap">{relTime(r.fired_at)}</td>
                    <td className="p-2.5 text-slate-200">{r.task_name || r.task_id}</td>
                    <td className="p-2.5"><StatusBadge status={r.status} /></td>
                    <td className="p-2.5 text-slate-400 max-w-lg">
                      <div className="line-clamp-2 break-words">{r.result_preview || r.prompt_preview || '—'}</div>
                      {r.stream_id && <div className="text-[10px] text-slate-600 font-mono mt-1">{r.stream_id}</div>}
                    </td>
                    <td className="p-2.5 text-xs">
                      {r.email_sent && <span className="text-emerald-400">已发送</span>}
                      {!r.email_sent && r.email_error && <span className="text-rose-400" title={r.email_error}>失败</span>}
                      {!r.email_sent && !r.email_error && <span className="text-slate-600">—</span>}
                    </td>
                  </tr>
                ))}
                {runs.length === 0 && <tr><td colSpan={5} className="p-6 text-center text-slate-500 text-sm">尚无运行历史</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      {editor && <TaskDialog initial={editor} onClose={() => setEditor(null)} />}
    </PageShell>
  )
}

function TaskCard({ s, onEdit, onFire }: { s: TaskSchedule; onEdit: () => void; onFire: () => void }) {
  const qc = useQueryClient()
  const toggle = async () => {
    await api.upsertTaskSchedule({ ...s, enabled: !s.enabled })
    qc.invalidateQueries({ queryKey: ['tasks.schedules'] })
  }
  const remove = async () => {
    const ok = await dialog.confirm('删除该定时任务？', s.name || s.id, {
      confirmText: '删除',
      tone: 'danger',
    })
    if (!ok) return
    await api.deleteTaskSchedule(s.id)
    qc.invalidateQueries({ queryKey: ['tasks.schedules'] })
  }
  return (
    <div className={`rounded-xl border p-4 ${s.enabled ? 'border-accent/60 bg-accent-soft/20' : 'border-line bg-bg-card'}`}>
      <div className="flex items-baseline justify-between mb-2 gap-2">
        <div className="text-sm font-semibold text-slate-200 truncate" title={s.name || s.id}>{s.name || s.id}</div>
        <span className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded ${s.enabled ? 'bg-emerald-900/40 text-emerald-300' : 'bg-slate-700 text-slate-400'}`}>
          {s.enabled ? '启用' : '禁用'}
        </span>
      </div>
      <div className="text-xs text-slate-500 mb-1 font-mono">{s.id}</div>
      <div className="text-sm text-slate-300 mb-2">
        {s.type === 'cron' && <CronCardLine expr={s.cron} />}
        {s.type === 'interval' && `每 ${s.interval_minutes} 分钟`}
      </div>
      <div className="text-xs text-slate-500">邮件 {s.notify_email ? '开启' : '关闭'} · 已触发 {s.fire_count} 次</div>
      <div className="text-xs text-slate-500 mt-1">上次 {s.last_fired_at ? relTime(s.last_fired_at) : '—'}</div>
      <div className="flex gap-2 mt-3 flex-wrap">
        <button onClick={onFire} className="text-xs px-2.5 py-1 rounded bg-accent text-white">立即触发</button>
        <button onClick={toggle} className="text-xs px-2.5 py-1 rounded border border-line text-slate-300 hover:bg-white/5">{s.enabled ? '禁用' : '启用'}</button>
        <button onClick={onEdit} className="text-xs px-2.5 py-1 rounded border border-line text-slate-300 hover:bg-white/5">编辑</button>
        <button onClick={remove} className="text-xs px-2.5 py-1 rounded border border-rose-700/60 text-rose-300 hover:bg-rose-900/20">删除</button>
      </div>
    </div>
  )
}

function TaskDialog({ initial, onClose }: { initial: Partial<TaskSchedule>; onClose: () => void }) {
  const qc = useQueryClient()
  const [s, setS] = useState<Partial<TaskSchedule>>({
    type: 'cron',
    enabled: true,
    name: '',
    prompt: '',
    cron: '0 8 * * *',
    interval_minutes: 60,
    notify_email: false,
    email_to: '',
    email_subject: 'GenericAgent 定时任务结果: {name}',
    ...initial,
  })
  const save = async () => {
    await api.upsertTaskSchedule({ ...s, type: (s.type ?? 'cron') as TaskScheduleType })
    qc.invalidateQueries({ queryKey: ['tasks.schedules'] })
    onClose()
  }
  return (
    <div className="fixed inset-0 z-30 bg-black/60 flex items-center justify-center" onClick={onClose}>
      <div className="bg-bg-soft border border-line rounded-xl p-6 w-[38rem] max-w-[92vw] max-h-[92vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold mb-4">{initial.id ? '编辑定时任务' : '新建定时任务'}</h3>
        <Field label="名称">
          <input value={s.name || ''} onChange={(e) => setS({ ...s, name: e.target.value })} className={inp} placeholder="每日早报" />
        </Field>
        <Field label="类型">
          <select value={s.type} onChange={(e) => setS({ ...s, type: e.target.value as TaskScheduleType })} className={inp}>
            <option value="cron">定时 (Cron)</option>
            <option value="interval">周期 (Interval)</option>
          </select>
        </Field>
        {s.type === 'cron' && (
          <Field label="Cron 表达式">
            <input value={s.cron || ''} onChange={(e) => setS({ ...s, cron: e.target.value })} className={inp + ' font-mono'} placeholder="分 时 日 月 周（本地时区）" />
            <CronPreview expr={s.cron || ''} />
          </Field>
        )}
        {s.type === 'interval' && (
          <Field label="间隔分钟">
            <input type="number" min={1} value={s.interval_minutes ?? 60}
                   onChange={(e) => setS({ ...s, interval_minutes: +e.target.value })} className={inp} />
          </Field>
        )}
        <Field label="任务 Prompt">
          <textarea
            value={s.prompt || ''}
            onChange={(e) => setS({ ...s, prompt: e.target.value })}
            rows={5}
            className={inp + ' resize-none text-xs leading-relaxed'}
            placeholder="每天抓取 XXX 信息，总结成 5 条要点；如果有重要变化，请在结论里标出。"
          />
        </Field>
        <Field label="邮件通知">
          <label className="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!s.notify_email} onChange={(e) => setS({ ...s, notify_email: e.target.checked })} />
            <span className="text-slate-300">任务完成后发送邮件</span>
          </label>
        </Field>
        {s.notify_email && (
          <>
            <Field label="收件人（留空使用默认收件人）">
              <input value={s.email_to || ''} onChange={(e) => setS({ ...s, email_to: e.target.value })} className={inp} placeholder="you@example.com" />
            </Field>
            <Field label="邮件标题">
              <input value={s.email_subject || ''} onChange={(e) => setS({ ...s, email_subject: e.target.value })} className={inp} placeholder="GenericAgent 定时任务结果: {name}" />
            </Field>
          </>
        )}
        <Field label="启用">
          <label className="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!s.enabled} onChange={(e) => setS({ ...s, enabled: e.target.checked })} />
            <span className="text-slate-300">保存后立即生效</span>
          </label>
        </Field>
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-3 py-1.5 rounded-lg border border-line text-slate-300">取消</button>
          <button onClick={save} className="px-3 py-1.5 rounded-lg bg-accent text-white">保存</button>
        </div>
      </div>
    </div>
  )
}

function EmailSettings() {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ['tasks.emailConfig'], queryFn: api.taskEmailConfig })
  const [editing, setEditing] = useState(false)
  const [cfg, setCfg] = useState<Partial<EmailConfig> & { password?: string }>({})
  const [testTo, setTestTo] = useState('')
  const [result, setResult] = useState('')
  const [testing, setTesting] = useState(false)

  const startEdit = () => {
    setCfg({
      host: data?.host || '',
      port: data?.port || 587,
      username: data?.username || '',
      password: '',
      from_addr: data?.from_addr || '',
      default_to: data?.default_to || '',
      use_tls: data?.use_tls ?? true,
      use_ssl: data?.use_ssl ?? false,
    })
    setEditing(true)
    setResult('')
  }
  const save = async () => {
    await api.saveTaskEmailConfig(cfg)
    await qc.invalidateQueries({ queryKey: ['tasks.emailConfig'] })
    setEditing(false)
    setResult('已保存')
  }
  const test = async () => {
    if (testing) return
    const testRecipient = testTo.trim()
    const recipient = testRecipient || (cfg.default_to || '').trim()
    const ok = await dialog.confirm(
      '发送测试邮件？',
      `将先保存当前 SMTP 配置，然后发送一封测试邮件到：\n${recipient || '默认收件人（当前未填写，可能发送失败）'}`,
      { confirmText: '发送测试' },
    )
    if (!ok) return

    setTesting(true)
    setResult('')
    try {
      await api.saveTaskEmailConfig(cfg)
      await qc.invalidateQueries({ queryKey: ['tasks.emailConfig'] })
      const r = await api.testTaskEmail(testRecipient, 'GenericAgent 邮件测试', '这是一封来自 GenericAgent-Admin 的测试邮件。')
      const msg = r.ok ? `测试邮件已发送到 ${r.to}` : `发送失败: ${r.error || 'unknown'}`
      setResult(msg)
      if (r.ok) {
        await dialog.alert('测试邮件已发送', `收件人：${r.to}`)
      } else {
        await dialog.alert('测试邮件发送失败', r.error || 'unknown')
      }
    } catch (e: any) {
      const msg = e?.body?.detail || e?.message || String(e)
      setResult(`发送失败: ${msg}`)
      await dialog.alert('测试邮件发送失败', msg)
    } finally {
      setTesting(false)
    }
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3 gap-3">
        <h2 className="text-sm font-semibold text-slate-300">邮件设置</h2>
        <button onClick={startEdit} className="text-xs px-2.5 py-1 rounded border border-line text-slate-300 hover:bg-white/5">配置 SMTP</button>
      </div>
      <div className="rounded-xl border border-line bg-bg-card p-4 text-sm text-slate-400">
        {data?.host
          ? <div>{data.host}:{data.port} · {data.username || '无需登录'} · 默认收件人 {data.default_to || '未设置'} · 密码 {data.password_set ? '已保存' : '未设置'}</div>
          : <div>未配置 SMTP。任务仍会执行，但邮件通知会失败。</div>}
        {result && <div className="mt-2 text-xs text-accent">{result}</div>}
      </div>
      {editing && (
        <div className="fixed inset-0 z-30 bg-black/60 flex items-center justify-center" onClick={() => setEditing(false)}>
          <div className="bg-bg-soft border border-line rounded-xl p-6 w-[34rem] max-w-[92vw]" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-base font-semibold mb-4">SMTP 邮件配置</h3>
            <Field label="SMTP Host"><input value={cfg.host || ''} onChange={(e) => setCfg({ ...cfg, host: e.target.value })} className={inp} placeholder="smtp.example.com" /></Field>
            <Field label="端口"><input type="number" value={cfg.port ?? 587} onChange={(e) => setCfg({ ...cfg, port: +e.target.value })} className={inp} /></Field>
            <Field label="用户名"><input value={cfg.username || ''} onChange={(e) => setCfg({ ...cfg, username: e.target.value })} className={inp} /></Field>
            <Field label="密码 / 授权码（留空保留旧密码）"><input type="password" value={cfg.password || ''} onChange={(e) => setCfg({ ...cfg, password: e.target.value })} className={inp} /></Field>
            <Field label="发件人"><input value={cfg.from_addr || ''} onChange={(e) => setCfg({ ...cfg, from_addr: e.target.value })} className={inp} placeholder="bot@example.com" /></Field>
            <Field label="默认收件人"><input value={cfg.default_to || ''} onChange={(e) => setCfg({ ...cfg, default_to: e.target.value })} className={inp} placeholder="you@example.com" /></Field>
            <div className="flex gap-4 mb-3 text-sm text-slate-300">
              <label className="inline-flex items-center gap-2"><input type="checkbox" checked={!!cfg.use_tls} onChange={(e) => setCfg({ ...cfg, use_tls: e.target.checked })} />STARTTLS</label>
              <label className="inline-flex items-center gap-2"><input type="checkbox" checked={!!cfg.use_ssl} onChange={(e) => setCfg({ ...cfg, use_ssl: e.target.checked })} />SSL</label>
            </div>
            <Field label="测试收件人（留空使用默认）"><input value={testTo} onChange={(e) => setTestTo(e.target.value)} className={inp} /></Field>
            <div className="flex justify-between gap-2 mt-4">
              <button
                onClick={test}
                disabled={testing}
                className="px-3 py-1.5 rounded-lg border border-line text-slate-300 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {testing ? '发送中…' : '发送测试'}
              </button>
              <div className="flex gap-2">
                <button onClick={() => setEditing(false)} className="px-3 py-1.5 rounded-lg border border-line text-slate-300">取消</button>
                <button onClick={save} className="px-3 py-1.5 rounded-lg bg-accent text-white">保存</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

function StatusBadge({ status }: { status: string }) {
  const cls = status === 'done'
    ? 'bg-emerald-900/40 text-emerald-300'
    : status === 'running'
      ? 'bg-amber-900/40 text-amber-300'
      : 'bg-rose-900/40 text-rose-300'
  return <span className={`text-[10px] px-1.5 py-0.5 rounded ${cls}`}>{status}</span>
}

const inp = 'w-full bg-bg-card border border-line rounded-lg px-3 py-1.5 text-sm outline-none focus:border-accent text-slate-200'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      {children}
    </div>
  )
}

function CronPreview({ expr }: { expr: string }) {
  const lang = navigator.language?.toLowerCase().startsWith('zh') ? 'zh_CN' : 'en'
  const result = useMemo(() => {
    const e = expr.trim()
    if (!e) return { ok: true as const, desc: '', next: [] as Date[] }
    try {
      const parsed = CronExpressionParser.parse(e)
      const desc = cronstrue.toString(e, { locale: lang })
      const next: Date[] = []
      for (let i = 0; i < 3; i++) next.push(parsed.next().toDate())
      return { ok: true as const, desc, next }
    } catch (err: any) {
      return { ok: false as const, error: String(err?.message || err) }
    }
  }, [expr, lang])

  if (!expr.trim()) return null

  if (!result.ok) {
    return (
      <div className="mt-1.5 text-xs text-rose-400 bg-rose-900/20 border border-rose-700/40 rounded px-2 py-1">
        ✗ {result.error}
      </div>
    )
  }

  return (
    <div className="mt-1.5 text-xs text-slate-400 space-y-0.5">
      <div className="text-emerald-400">✓ {result.desc}</div>
      {result.next.length > 0 && (
        <div className="text-slate-500">下次触发：{result.next.map(formatLocal).join(' · ')}</div>
      )}
    </div>
  )
}

function formatLocal(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function CronCardLine({ expr }: { expr: string }) {
  const next = useMemo(() => {
    try { return CronExpressionParser.parse(expr).next().toDate() }
    catch { return null }
  }, [expr])
  return (
    <div>
      <span className="font-mono text-xs">{expr}</span>
      {next && <span className="text-xs text-slate-500 ml-2">→ {formatLocal(next)}</span>}
    </div>
  )
}
