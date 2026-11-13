import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import Modal from '@/components/Modal'

const ENDPOINT = '/api/dimdef'
const STRUCTURAL_FIELDS = ['自然键列', '代理键列', '缓慢变化策略'] as const
const INTERVAL_FIELDS = ['生效日期', '失效日期'] as const
const ALL_FIELDS = ['源表名称', ...STRUCTURAL_FIELDS, ...INTERVAL_FIELDS] as const

type VersionStatus = '草稿' | '待审批' | '已生效' | '已失效' | '已归档'
type Row = Record<string, string | number | null>

interface HistoryEntry {
  action: string
  at: string
  by: string
  note: string
}

interface VersionRecord {
  version_no: number
  status: VersionStatus
  backfill_of: number | null
  源表名称: string
  自然键列: string
  代理键列: string
  缓慢变化策略: string
  生效日期: string
  失效日期: string
  completeness: number
  editable: boolean
  closed: boolean
  change_note: string
  created_at: string
  history: HistoryEntry[]
}

type FormValues = Record<(typeof ALL_FIELDS)[number] | 'change_note', string>

interface Timeline {
  维度编号: string
  维度名称: string
  current_version_no: number
  status: string
  versions: VersionRecord[]
}

interface Todo {
  todo_key: string
  维度编号: string
  维度名称: string
  version_no: number
  缓慢变化策略: string
  生效日期: string
  失效日期: string
  change_note: string
  submitted_at: string
}

const STATUS_CLASS: Record<VersionStatus, string> = {
  草稿: 'tag tag-draft',
  待审批: 'tag tag-pending',
  已生效: 'tag tag-active',
  已失效: 'tag tag-expired',
  已归档: 'tag tag-archived',
}

function emptyForm() {
  return { 源表名称: '', 自然键列: '', 代理键列: '', 缓慢变化策略: '', 生效日期: '', 失效日期: '', change_note: '' }
}

export default function DimdefView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [todos, setTodos] = useState<Todo[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [timeline, setTimeline] = useState<Timeline | null>(null)
  const [editing, setEditing] = useState<{ headerId: number; version: VersionRecord } | null>(null)
  const [backfill, setBackfill] = useState<{ headerId: number; version: VersionRecord } | null>(null)
  const [form, setForm] = useState(emptyForm())
  const [busy, setBusy] = useState(false)

  const filterFields = useMemo(() => ['维度编号', '维度名称', '源表名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('维度定义列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      const todoResp = await request(`${ENDPOINT}/todos`)
      if (todoResp.ok) setTodos((await todoResp.json()).items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度定义管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters])

  async function openTimeline(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/timeline`)
      if (!response.ok) throw new Error('版本链时间线读取失败')
      setTimeline(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '版本链读取失败')
    }
  }

  async function refreshTimeline(headerId: number) {
    const response = await request(`${ENDPOINT}/${headerId}/timeline`)
    if (response.ok) setTimeline(await response.json())
  }

  async function versionAction(headerId: number, versionNo: number, action: string) {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${headerId}/versions/${versionNo}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!payload.ok) throw new Error(payload.message)
      await Promise.all([refreshTimeline(headerId), reload()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '版本动作未生效')
    } finally {
      setBusy(false)
    }
  }

  function openEditor(headerId: number, version: VersionRecord) {
    setEditing({ headerId, version })
    setForm({
      源表名称: String(version.源表名称 ?? ''),
      自然键列: String(version.自然键列 ?? ''),
      代理键列: String(version.代理键列 ?? ''),
      缓慢变化策略: String(version.缓慢变化策略 ?? ''),
      生效日期: String(version.生效日期 ?? ''),
      失效日期: String(version.失效日期 ?? ''),
      change_note: '',
    })
  }

  async function submitEdit() {
    if (!editing) return
    setBusy(true)
    setErrorMessage('')
    try {
      const values: Record<string, string> = { change_note: form.change_note || '管理员修正' }
      for (const field of ALL_FIELDS) {
        if (form[field] !== String(editing.version[field] ?? '')) values[field] = form[field]
      }
      const response = await request(`${ENDPOINT}/${editing.headerId}/versions/${editing.version.version_no}`, {
        method: 'PUT',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!payload.ok) throw new Error(payload.message)
      setEditing(null)
      await Promise.all([refreshTimeline(editing.headerId), reload()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '版本修改失败')
    } finally {
      setBusy(false)
    }
  }

  function openBackfill(headerId: number, version: VersionRecord) {
    setBackfill({ headerId, version })
    setForm({
      ...emptyForm(),
      源表名称: String(version.源表名称 ?? ''),
      自然键列: String(version.自然键列 ?? ''),
      代理键列: String(version.代理键列 ?? ''),
      缓慢变化策略: String(version.缓慢变化策略 ?? ''),
      生效日期: String(version.生效日期 ?? ''),
      失效日期: String(version.失效日期 ?? ''),
    })
  }

  async function submitBackfill() {
    if (!backfill) return
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(
        `${ENDPOINT}/${backfill.headerId}/versions/${backfill.version.version_no}/backfill`,
        {
          method: 'POST',
          body: JSON.stringify({
            values: {
              代理键列: form.代理键列,
              自然键列: form.自然键列,
              缓慢变化策略: form.缓慢变化策略,
              生效日期: form.生效日期,
              失效日期: form.失效日期,
              change_note: form.change_note || '晚到修正回填',
            },
          }),
        },
      )
      const payload = await response.json()
      if (!payload.ok) throw new Error(payload.message)
      setBackfill(null)
      await Promise.all([refreshTimeline(backfill.headerId), reload()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '回填失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="page" data-module="dimdef">
      <header className="page-head">
        <div>
          <h2>维度定义管理 · 版本链</h2>
          <p className="page-desc">
            自然键、代理键或缓慢变化策略的修改自动生成新版本；已生效版本按区间留档不可覆盖，
            版本只能 草稿 → 待审批 → 已生效 顺序推进，晚到修正通过回填进入所属历史区间。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出维度清单（含版本链）
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">维度总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已生效维度</span>
          <strong className="stat-value">{rows.filter((r) => r.status === '已生效').length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">审批待办（版本粒度去重）</span>
          <strong className="stat-value">{todos.length}</strong>
        </article>
      </div>

      {todos.length > 0 && (
        <div className="todo-panel">
          <h4>审批待办</h4>
          <ul>
            {todos.map((todo) => (
              <li key={todo.todo_key}>
                <span className="tag tag-pending">待审批</span>
                <strong>{todo.维度编号} v{todo.version_no}</strong>
                <span className="muted">{todo.维度名称}</span>
                <span className="muted">提交于 {todo.submitted_at.replace('T', ' ')}</span>
                <span className="muted">变更：{todo.change_note}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>维度编号</th>
            <th>维度名称</th>
            <th>当前版本</th>
            <th>自然键列</th>
            <th>代理键列</th>
            <th>缓慢变化策略</th>
            <th>链上版本数</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{row.维度编号 as string}</td>
              <td>{row.维度名称 as string}</td>
              <td>v{String(row.current_version_no)}</td>
              <td>{(row.自然键列 as string) ?? '—'}</td>
              <td>{(row.代理键列 as string) ?? '—'}</td>
              <td>{(row.缓慢变化策略 as string) ?? '—'}</td>
              <td>{Array.isArray(row.versions) ? (row.versions as unknown[]).length : 0}</td>
              <td><span className={STATUS_CLASS[row.status as VersionStatus] ?? 'tag'}>{row.status as string}</span></td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openTimeline(row)}>
                  查看版本时间线
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={9}>暂无维度定义数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条维度定义</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {timeline && (
        <Modal wide title={`版本时间线 · ${timeline.维度编号} ${timeline.维度名称}`} onClose={() => setTimeline(null)}>
          <div className="timeline">
            {timeline.versions.map((version, index) => (
              <article key={version.version_no} className={`tl-item ${version.backfill_of ? 'tl-backfill' : ''}`}>
                <div className="tl-rail">
                  <span className={`tl-dot dot-${version.status}`} />
                  {index < timeline.versions.length - 1 && <span className="tl-line" />}
                </div>
                <div className="tl-card">
                  <header className="tl-card-head">
                    <div>
                      <strong>v{version.version_no}</strong>
                      {version.backfill_of && <span className="tag tag-backfill">回填 v{version.backfill_of}</span>}
                      <span className={STATUS_CLASS[version.status]}>{version.status}</span>
                    </div>
                    <div className="tl-actions">
                      {(version.editable || version.status === '待审批') && (
                        <button className="link" type="button" disabled={busy} onClick={() => openEditor(timeline.versions.length && headerIdOf(timeline, rows), version)}>
                          修改
                        </button>
                      )}
                      {(version.status === '已生效' || version.status === '已失效') && (
                        <button className="link" type="button" disabled={busy} onClick={() => openBackfill(headerIdOf(timeline, rows), version)}>
                          回填修正
                        </button>
                      )}
                      {version.status === '草稿' && (
                        <button className="link" type="button" disabled={busy} onClick={() => void versionAction(headerIdOf(timeline, rows), version.version_no, '提交审批')}>
                          提交审批
                        </button>
                      )}
                      {version.status === '待审批' && (
                        <>
                          <button className="link" type="button" disabled={busy} onClick={() => void versionAction(headerIdOf(timeline, rows), version.version_no, '提交审批')}>
                            重复提交（幂等）
                          </button>
                          <button className="link" type="button" disabled={busy} onClick={() => void versionAction(headerIdOf(timeline, rows), version.version_no, '发布维度')}>
                            发布
                          </button>
                        </>
                      )}
                      {version.status !== '已归档' && (
                        <button className="link danger" type="button" disabled={busy} onClick={() => void versionAction(headerIdOf(timeline, rows), version.version_no, '废弃版本')}>
                          废弃
                        </button>
                      )}
                    </div>
                  </header>
                  <dl className="tl-fields">
                    <div><dt>源表名称</dt><dd>{version.源表名称 || '—'}</dd></div>
                    <div><dt>自然键列</dt><dd>{version.自然键列 || '—'}</dd></div>
                    <div><dt>代理键列</dt><dd>{version.代理键列 || '—'}</dd></div>
                    <div><dt>缓慢变化策略</dt><dd>{version.缓慢变化策略 || '—'}</dd></div>
                    <div><dt>生效区间</dt><dd>{version.生效日期 || '…'} ~ {version.失效日期 || '至今'}</dd></div>
                    <div><dt>完整度</dt><dd>{version.completeness}/6</dd></div>
                  </dl>
                  <p className="tl-note">{version.change_note}</p>
                  <details className="tl-history">
                    <summary>操作履历（{version.history.length}）</summary>
                    <ol>
                      {version.history.map((entry, i) => (
                        <li key={i}>
                          <span className="muted">{entry.at.replace('T', ' ')}</span>
                          <strong>{entry.action}</strong>
                          <span className="muted">{entry.by}</span>
                          <span>{entry.note}</span>
                        </li>
                      ))}
                    </ol>
                  </details>
                </div>
              </article>
            ))}
          </div>
        </Modal>
      )}

      {editing && (
        <Modal title={`修改 v${editing.version.version_no}（${editing.version.status}）`} onClose={() => setEditing(null)}>
          <div className="form-grid">
            {ALL_FIELDS.map((field) => (
              <label key={field} className="form-item">
                <span>{field}{(STRUCTURAL_FIELDS as readonly string[]).includes(field) && <em className="hint">结构字段：在档版本修改将生成新版本</em>}</span>
                <input value={form[field]} onChange={(e) => setForm((prev) => ({ ...prev, [field]: e.target.value }))} />
              </label>
            ))}
            <label className="form-item">
              <span>变更说明</span>
              <input value={form.change_note} onChange={(e) => setForm((prev) => ({ ...prev, change_note: e.target.value }))} />
            </label>
          </div>
          <p className="form-tip">
            草稿/待审批版本可直接修正；对已生效、已失效或已归档版本修改结构字段会派生新草稿，原版本按区间留档，
            仅改日期会被拒绝并提示走回填。
          </p>
          <div className="modal-actions">
            <button className="btn ghost" type="button" onClick={() => setEditing(null)}>取消</button>
            <button className="btn primary" type="button" disabled={busy} onClick={() => void submitEdit()}>提交修改</button>
          </div>
        </Modal>
      )}

      {backfill && (
        <Modal title={`回填修正 · 基于 v${backfill.version.version_no} 的历史区间`} onClose={() => setBackfill(null)}>
          <div className="form-grid">
            {ALL_FIELDS.map((field) => (
              <label key={field} className="form-item">
                <span>{field}</span>
                <input value={form[field]} onChange={(e) => setForm((prev) => ({ ...prev, [field]: e.target.value }))} />
              </label>
            ))}
            <label className="form-item">
              <span>回填说明</span>
              <input value={form.change_note} onChange={(e) => setForm((prev) => ({ ...prev, change_note: e.target.value }))} />
            </label>
          </div>
          <p className="form-tip">
            回填版本独立挂在 v{backfill.version.version_no} 所属区间（{backfill.version.生效日期 || '…'} ~{' '}
            {backfill.version.失效日期 || '至今'}）内，发布后在该区间优先于原版本，当前版本不会被改写。
          </p>
          <div className="modal-actions">
            <button className="btn ghost" type="button" onClick={() => setBackfill(null)}>取消</button>
            <button className="btn primary" type="button" disabled={busy} onClick={() => void submitBackfill()}>生成回填草稿</button>
          </div>
        </Modal>
      )}
    </section>
  )
}

/** 时间线弹窗内通过维度编号反查头部 id（动作接口需要 id）。 */
function headerIdOf(timeline: Timeline, rows: Row[]): number {
  const hit = rows.find((row) => row.维度编号 === timeline.维度编号)
  return hit ? Number(hit.id) : 0
}
