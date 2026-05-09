// Setup / Settings — pick or change the GenericAgent project directory.
//
// In setup mode (backend has no GA_ROOT), the SPA forces this page.
// In normal mode, it's reachable at /settings via the sidebar.

import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { ChatRetryConfig } from '@/api/types'
import { PageShell } from '@/components/PageShell'
import { useNotifyStore } from '@/utils/notify'

export function Settings({ initialMode = 'settings' }: { initialMode?: 'settings' | 'setup' }) {
  const qc = useQueryClient()
  const { data: setup, refetch } = useQuery({ queryKey: ['setup'], queryFn: api.setupStatus })
  const [input, setInput] = useState('')
  const [pythonInput, setPythonInput] = useState('')
  const [validating, setValidating] = useState(false)
  const [validResult, setValidResult] = useState<{ valid: boolean; resolved: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  useEffect(() => {
    if (!setup) return
    if (!input) setInput(setup.ga_root || '')
    if (!pythonInput) setPythonInput(setup.python_path || '')
  }, [setup, input, pythonInput])

  const validate = async (path: string) => {
    if (!path.trim()) { setValidResult(null); return }
    setValidating(true)
    try {
      const r = await api.setupValidate(path)
      setValidResult(r)
    } catch {
      setValidResult({ valid: false, resolved: path })
    } finally {
      setValidating(false)
    }
  }

  const save = async () => {
    setSaveMsg(null); setSaveErr(null); setSaving(true)
    try {
      const r = await api.setupSave(input.trim(), pythonInput.trim())
      setSaveMsg(`✓ 已保存：${r.ga_root}\n请重启后端（关闭并重开窗口）以加载新配置。`)
      qc.invalidateQueries({ queryKey: ['setup'] })
      qc.invalidateQueries({ queryKey: ['status'] })
      refetch()
    } catch (e: any) {
      const msg = e?.body?.detail || e?.message || String(e)
      setSaveErr(msg)
    } finally {
      setSaving(false)
    }
  }

  const inSetup = initialMode === 'setup' || !setup?.configured

  return (
    <PageShell
      title={inSetup ? '初始设置' : '设置 · GenericAgent 路径'}
      description={inSetup
        ? '请选择本机上的 GenericAgent 项目目录。这是一次性配置；后续每次启动会自动读取。'
        : '修改后需要重启后端以生效。配置保存在 ~/.genericagent-admin/config.json，独立于 GenericAgent 主项目，git pull 不会覆盖。'}
    >
      <div className="p-6 max-w-3xl mx-auto space-y-6">
        {/* current */}
        <div className="rounded-xl border border-line bg-bg-card p-4">
          <div className="text-xs text-slate-500 mb-1">当前 GenericAgent 路径</div>
          <div className="font-mono text-sm break-all">
            {setup?.ga_root
              ? <span className="text-emerald-300">{setup.ga_root}</span>
              : <span className="text-rose-400">尚未配置</span>}
          </div>
          <div className="text-xs text-slate-500 mt-2">
            Admin 数据目录：<span className="font-mono">{setup?.admin_data}</span>
          </div>
          <div className="text-xs text-slate-500 mt-2">
            Python 执行环境：
            <span className="font-mono text-slate-300 break-all">
              {setup?.resolved_python || '未找到，将回退当前进程'}
            </span>
            {setup?.resolved_python_source && (
              <span className="ml-1 text-slate-600">({setup.resolved_python_source})</span>
            )}
          </div>
        </div>

        {/* picker */}
        <div className="rounded-xl border border-line bg-bg-card p-4">
          <div className="text-sm font-semibold mb-3">选择目录与执行环境</div>
          <div className="text-xs text-slate-500 mb-3">
            必须包含 <code>agentmain.py</code> 与 <code>memory/</code>。
          </div>

          <div className="flex gap-2">
            <input
              value={input}
              onChange={(e) => { setInput(e.target.value); setValidResult(null) }}
              onBlur={(e) => validate(e.target.value)}
              placeholder="/path/to/GenericAgent"
              className="flex-1 bg-bg-soft border border-line rounded-lg px-3 py-2 text-sm font-mono outline-none focus:border-accent"
            />
            <button
              onClick={() => validate(input)}
              disabled={validating}
              className="px-3 py-2 rounded-lg border border-line text-slate-300 hover:bg-white/5 text-sm"
            >{validating ? '检测中…' : '检测'}</button>
          </div>

          {validResult && (
            <div className={`mt-2 text-sm ${validResult.valid ? 'text-emerald-400' : 'text-rose-400'}`}>
              {validResult.valid
                ? `✓ 这是一个有效的 GenericAgent 目录（解析为 ${validResult.resolved}）`
                : '✗ 该目录不是 GenericAgent 项目（缺少 agentmain.py 或 memory/）'}
            </div>
          )}

          <div className="mt-4">
            <div className="text-xs text-slate-500 mb-1">Python 解释器（可选）</div>
            <input
              value={pythonInput}
              onChange={(e) => setPythonInput(e.target.value)}
              placeholder="/path/to/python；留空则自动使用 GA 虚拟环境或本机 Python"
              className="w-full bg-bg-soft border border-line rounded-lg px-3 py-2 text-sm font-mono outline-none focus:border-accent"
            />
            <div className="text-xs text-slate-500 mt-1">
              用于 SOP / code_run 执行 Python。优先级：GA_PYTHON 环境变量 → 此处配置 → GA 项目虚拟环境 → 本机 Python。
            </div>
          </div>

          <div className="mt-4 flex items-center gap-2">
            <button
              onClick={save}
              disabled={saving || !input.trim()}
              className="px-4 py-2 rounded-lg bg-accent text-white text-sm disabled:opacity-40"
            >{saving ? '保存中…' : '保存配置'}</button>
            {saveMsg && <span className="text-sm text-emerald-400 whitespace-pre-line">{saveMsg}</span>}
            {saveErr && <span className="text-sm text-rose-400 whitespace-pre-line">{saveErr}</span>}
          </div>
        </div>

        {/* candidates */}
        {(setup?.candidates?.length ?? 0) > 0 && (
          <div className="rounded-xl border border-line bg-bg-card p-4">
            <div className="text-sm font-semibold mb-3">本机检测到的候选位置</div>
            <ul className="space-y-1.5">
              {setup!.candidates.map((c) => (
                <li key={c.path} className="flex items-center justify-between gap-2 text-sm">
                  <code className={`font-mono break-all ${c.valid ? 'text-slate-200' : 'text-slate-500'}`}>
                    {c.path}
                  </code>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className={`text-xs ${c.valid ? 'text-emerald-400' : 'text-slate-500'}`}>
                      {c.valid ? '有效' : '不可用'}
                    </span>
                    {c.valid && (
                      <button
                        onClick={() => { setInput(c.path); validate(c.path) }}
                        className="text-xs px-2 py-1 rounded border border-line text-slate-300 hover:bg-white/5"
                      >选用</button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}

        {inSetup && setup?.configured && (
          <div className="rounded-xl border border-amber-700/60 bg-amber-900/20 p-4 text-sm text-amber-200">
            ✓ 配置已保存。请关闭本窗口后重新双击 start.command（或 start.bat）以进入正常模式。
          </div>
        )}

        {!inSetup && <ChatRetryPanel />}

        {!inSetup && <NotifyPanel />}
      </div>
    </PageShell>
  )
}

function ChatRetryPanel() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['agent.chatRetryConfig'], queryFn: api.chatRetryConfig })
  const [cfg, setCfg] = useState<ChatRetryConfig>({ enabled: true, max_attempts: 2 })
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    if (data) setCfg(data)
  }, [data])

  const save = async () => {
    setSaving(true)
    setMsg('')
    try {
      const saved = await api.saveChatRetryConfig({
        enabled: !!cfg.enabled,
        max_attempts: Math.max(0, Math.min(5, Math.floor(Number(cfg.max_attempts) || 0))),
      })
      setCfg(saved)
      await qc.invalidateQueries({ queryKey: ['agent.chatRetryConfig'] })
      setMsg('已保存')
    } catch (e: any) {
      setMsg(`保存失败：${e?.body?.detail || e?.message || String(e)}`)
    } finally {
      setSaving(false)
      window.setTimeout(() => setMsg(''), 3500)
    }
  }

  return (
    <div className="rounded-xl border border-line bg-bg-card p-4">
      <div className="text-sm font-semibold mb-1">实时聊天自动重试</div>
      <div className="text-xs text-slate-500 mb-3">
        当 Agent 回复末尾出现可恢复的网络/传输错误（例如 !!!Error: SSLError）时，后端会自动发起一次继续/重试请求。
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <label className="inline-flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={!!cfg.enabled}
            onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
            disabled={isLoading || saving}
          />
          <span>{cfg.enabled ? '已开启' : '已关闭'}</span>
        </label>

        <label className="inline-flex items-center gap-2 text-sm text-slate-300">
          <span className="text-slate-500">最大重试次数</span>
          <input
            type="number"
            min={0}
            max={5}
            value={cfg.max_attempts}
            onChange={(e) => setCfg({ ...cfg, max_attempts: Number(e.target.value) })}
            disabled={isLoading || saving || !cfg.enabled}
            className="w-20 bg-bg-soft border border-line rounded-lg px-2 py-1 text-sm outline-none focus:border-accent disabled:opacity-50"
          />
        </label>

        <button
          onClick={save}
          disabled={saving || isLoading}
          className="px-3 py-1.5 rounded-lg bg-accent text-white text-sm disabled:opacity-40"
        >
          {saving ? '保存中…' : '保存'}
        </button>

        {msg && <span className={`text-xs ${msg.startsWith('保存失败') ? 'text-rose-400' : 'text-emerald-400'}`}>{msg}</span>}
      </div>
      <div className="text-[11px] text-slate-600 mt-2">
        计数按同一次逻辑提交累加，达到上限后会停止并保留错误回复；非可恢复错误不会重试。
      </div>
    </div>
  )
}

function NotifyPanel() {
  const optedIn = useNotifyStore((s) => s.optedIn)
  const backendName = useNotifyStore((s) => s.backendName)
  const setOptIn = useNotifyStore((s) => s.setOptIn)
  const refresh = useNotifyStore((s) => s.refresh)
  const [testing, setTesting] = useState<null | string>(null)

  useEffect(() => {
    refresh()
  }, [refresh])

  const toggle = () => {
    setOptIn(!optedIn)
  }

  const test = async () => {
    setTesting('发送中…')
    try {
      const r = await api.notify('🔔 测试通知', '通知通道工作正常。Agent 完成回复或微信新消息时会主动提醒。')
      setTesting(r.ok ? `✅ 已发送（${r.backend}）` : `⚠ 发送失败：${r.error || r.backend}`)
    } catch (e) {
      setTesting(`⚠ 网络异常：${(e as Error).message}`)
    } finally {
      window.setTimeout(() => setTesting(null), 3500)
    }
  }

  // No platform support — backend reports an unsupported OS. Show a hint
  // and disable the toggle. This shouldn't happen in practice (we cover
  // macOS / Windows / Linux), but be honest if it does.
  const unsupported = backendName.startsWith('unsupported')

  return (
    <div className="rounded-xl border border-line bg-bg-card p-4">
      <div className="text-sm font-semibold mb-1">桌面通知</div>
      <div className="text-xs text-slate-500 mb-3">
        Agent 完成长任务、微信收到新消息时弹系统通知。仅在窗口失焦/最小化时触发，不会打断当前查看。
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={toggle}
          disabled={unsupported}
          className={`px-3 py-1.5 rounded-lg text-sm transition ${
            optedIn ? 'bg-accent text-white' : 'border border-line text-slate-300 hover:bg-white/5'
          } disabled:opacity-40`}
        >
          {optedIn ? '已开启 · 点击关闭' : '开启桌面通知'}
        </button>
        {optedIn && !unsupported && (
          <button onClick={test} className="text-xs px-2.5 py-1 rounded border border-line text-slate-300 hover:bg-white/5">
            发一条测试
          </button>
        )}
        {backendName && (
          <span className="text-xs text-slate-500">通道：{backendName}</span>
        )}
      </div>

      {testing && <div className="mt-2 text-xs text-slate-400">{testing}</div>}

      {unsupported && (
        <div className="mt-2 text-xs text-amber-400">
          ⚠ 当前系统暂不支持（{backendName}）。仅 macOS / Windows / Linux 已实现。
        </div>
      )}
    </div>
  )
}
