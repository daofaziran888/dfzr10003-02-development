import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import Modal from '@/components/Modal'

const ENDPOINT = '/api/factdef'
const DIM_ENDPOINT = '/api/dimdef'

interface ResolvedRef {
  维度编号: string
  维度名称?: string
  version_no: number
  version_status?: string
  生效日期?: string
  失效日期?: string
  自然键列?: string
  缓慢变化策略?: string
  covers_target: boolean
  effective_version_no?: number | null
  ok: boolean
  reason: string
  conclusion: string
}

interface FactRow extends Record<string, unknown> {
  id: number
  事实编号: string
  事实名称: string
  源表名称: string
  粒度说明: string
  度量字段: string
  分区字段: string
  最近刷新: string
  事实批次: string
  status: string
  引用可发布: boolean
  阻断原因: string[]
  维度版本状态: ResolvedRef[]
}

interface DimVersion {
  version_no: number
  status: string
  自然键列: string
  生效日期: string
  失效日期: string
}

interface DimHeader extends Record<string, unknown> {
  id: number
  维度编号: string
  维度名称: string
  versions: DimVersion[]
}

interface Conflict {
  自然键列: string
  目标日期: string
  winner: { 事实编号: string; 维度编号: string; version_no: number; 事实批次: string }
  candidates: {
    事实编号: string
    事实批次: string
    维度编号: string
    version_no: number
    version_status: string
    covers_target: boolean
    completeness: number
    生效日期: string
  }[]
  rule: string
  reason: string
}

export default function FactdefView() {
  const [rows, setRows] = useState<FactRow[]>([])
  const [total, setTotal] = useState(0)
  const [targetDate, setTargetDate] = useState('2026-11-13')
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [conflicts, setConflicts] = useState<Conflict[]>([])
  const [picker, setPicker] = useState<{ fact: FactRow; dims: DimHeader[] } | null>(null)
  const [busy, setBusy] = useState(false)

  const filterFields = useMemo(() => ['事实编号', '事实名称', '源表名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams({ ...filters, target: targetDate }).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('事实定义列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      const conflictResp = await request(`${ENDPOINT}/conflicts?target=${encodeURIComponent(targetDate)}`)
      if (conflictResp.ok) setConflicts((await conflictResp.json()).conflicts ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '事实表管理列表读取失败')
    }
  }, [filters, targetDate])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: FactRow) {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, target: targetDate } }),
      })
      const payload = await response.json()
      if (!payload.ok) throw new Error(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '事实表管理操作失败')
    } finally {
      setBusy(false)
    }
  }

  async function openPicker(fact: FactRow) {
    setErrorMessage('')
    try {
      const response = await request(`${DIM_ENDPOINT}?size=200`)
      if (!response.ok) throw new Error('维度清单读取失败')
      const payload = await response.json()
      setPicker({ fact, dims: payload.items ?? [] })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度版本选择器打开失败')
    }
  }

  async function lockReference(dim: DimHeader, version: DimVersion) {
    if (!picker) return
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${picker.fact.id}/references`, {
        method: 'PUT',
        body: JSON.stringify({ values: { 维度编号: dim.维度编号, version_no: version.version_no } }),
      })
      const payload = await response.json()
      if (!payload.ok) throw new Error(payload.message)
      setPicker(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度版本锁定失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="page" data-module="factdef">
      <header className="page-head">
        <div>
          <h2>事实表管理 · 维度版本引用</h2>
          <p className="page-desc">
            每个关联维度都显示事实实际锁定的版本与统一失效结论；待发布事实引用未生效或区间冲突的维度版本时
            直接阻断发布。维度状态由版本链实时解析，事实清单与维度定义页始终同版同结论。
          </p>
        </div>
        <div className="page-actions">
          <label className="filter-item target-picker">
            <span>目标日期</span>
            <input type="date" value={targetDate} onChange={(e) => setTargetDate(e.target.value)} />
          </label>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export?target=${encodeURIComponent(targetDate)}`, '_blank')}>
            导出事实清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">事实定义总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">引用校验通过</span>
          <strong className="stat-value">{rows.filter((r) => r.引用可发布).length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">同自然键冲突待裁决</span>
          <strong className="stat-value">{conflicts.length}</strong>
        </article>
      </div>

      {conflicts.length > 0 && (
        <div className="conflict-panel">
          <h4>同自然键不同版本引用 · 优先级裁决</h4>
          {conflicts.map((conflict) => (
            <div key={conflict.自然键列} className="conflict-block">
              <p>
                自然键 <strong>{conflict.自然键列}</strong> · 目标日期 {conflict.目标日期} ·
                获胜方 <strong className="win-text">{conflict.winner.事实编号}</strong>
                （{conflict.winner.维度编号} v{conflict.winner.version_no}，批次 {conflict.winner.事实批次}）
              </p>
              <table className="data-table inner-table">
                <thead>
                  <tr>
                    <th>事实编号</th><th>事实批次</th><th>引用版本</th><th>版本状态</th>
                    <th>覆盖目标日期</th><th>版本完整度</th><th>版本生效日期</th>
                  </tr>
                </thead>
                <tbody>
                  {conflict.candidates.map((candidate) => (
                    <tr key={`${candidate.事实编号}-${candidate.维度编号}-${candidate.version_no}`}
                        className={candidate.事实编号 === conflict.winner.事实编号 ? 'winner-row' : ''}>
                      <td>{candidate.事实编号}{candidate.事实编号 === conflict.winner.事实编号 && ' 🏆'}</td>
                      <td>{candidate.事实批次}</td>
                      <td>{candidate.维度编号} v{candidate.version_no}</td>
                      <td>{candidate.version_status}</td>
                      <td>{candidate.covers_target ? '是' : '否'}</td>
                      <td>{candidate.completeness}/6</td>
                      <td>{candidate.生效日期 || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted rule-text">裁决规则：{conflict.rule}。{conflict.reason}</p>
            </div>
          ))}
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
            <th>事实编号</th>
            <th>事实名称</th>
            <th>事实批次</th>
            <th>状态</th>
            <th>关联维度实际选中版本（同源结论）</th>
            <th>发布校验</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className={row.引用可发布 ? '' : 'row-blocked'}>
              <td>{row.事实编号}</td>
              <td>{row.事实名称}</td>
              <td>{row.事实批次 || '—'}</td>
              <td><span className={`tag tag-${row.status === '已生效' ? 'active' : row.status === '待审批' ? 'pending' : 'draft'}`}>{row.status}</span></td>
              <td>
                <ul className="ref-list">
                  {row.维度版本状态.map((ref) => (
                    <li key={`${ref.维度编号}-${ref.version_no}`} className={ref.ok ? 'ref-ok' : 'ref-bad'}>
                      <span className={`tag tag-${ref.ok ? 'active' : 'expired'}`}>{ref.conclusion}</span>
                      <strong>{ref.维度编号} v{ref.version_no}</strong>
                      <span className="muted">
                        {ref.version_status} · {ref.自然键列} · {ref.生效日期 || '…'} ~ {ref.失效日期 || '至今'}
                      </span>
                      <span className={ref.ok ? 'reason-ok' : 'error-text'}>{ref.reason}</span>
                    </li>
                  ))}
                  {row.维度版本状态.length === 0 && <li className="muted">尚未关联维度版本</li>}
                </ul>
              </td>
              <td>
                {row.引用可发布
                  ? <span className="reason-ok">可发布</span>
                  : <span className="error-text">阻断：{row.阻断原因.join('；')}</span>}
              </td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openPicker(row)}>选择维度版本</button>
                {row.status === '草稿' && (
                  <button className="link" type="button" disabled={busy} onClick={() => void runAction('提交审批', row)}>提交审批</button>
                )}
                {row.status === '待审批' && (
                  <button className="link" type="button" disabled={busy} onClick={() => void runAction('发布事实', row)}>发布事实</button>
                )}
                {row.status !== '已废弃' && (
                  <button className="link danger" type="button" disabled={busy} onClick={() => void runAction('废弃事实', row)}>废弃事实</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={7}>暂无事实表管理数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条事实定义 · 目标日期 {targetDate}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {picker && (
        <Modal wide title={`选择维度版本 · ${picker.fact.事实编号}`} onClose={() => setPicker(null)}>
          <p className="form-tip">
            引用以「维度编号 + 版本号」锁定，事实发布后随事实留档；选择后页面立即按版本链重新解析状态与失效结论。
          </p>
          {picker.dims.map((dim) => (
            <div key={dim.id} className="dim-picker-block">
              <h5>{dim.维度编号} · {dim.维度名称}</h5>
              <table className="data-table inner-table">
                <thead>
                  <tr><th>版本</th><th>状态</th><th>自然键列</th><th>生效区间</th><th></th></tr>
                </thead>
                <tbody>
                  {dim.versions.map((version) => (
                    <tr key={version.version_no}>
                      <td>v{version.version_no}</td>
                      <td><span className={`tag tag-${version.status === '已生效' ? 'active' : version.status === '待审批' ? 'pending' : version.status === '草稿' ? 'draft' : 'expired'}`}>{version.status}</span></td>
                      <td>{version.自然键列}</td>
                      <td>{version.生效日期 || '…'} ~ {version.失效日期 || '至今'}</td>
                      <td>
                        <button className="link" type="button" disabled={busy} onClick={() => void lockReference(dim, version)}>
                          锁定此版本
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </Modal>
      )}
    </section>
  )
}
