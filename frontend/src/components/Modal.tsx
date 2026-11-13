import type { ReactNode } from 'react'

/** 轻量弹窗：维度时间线、版本编辑、引用选择都复用这一个容器。 */
export default function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  return (
    <div className="modal-mask" role="presentation" onMouseDown={onClose}>
      <div
        className={wide ? 'modal-panel modal-wide' : 'modal-panel'}
        role="dialog"
        aria-modal="true"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h3>{title}</h3>
          <button className="link" type="button" onClick={onClose}>关闭</button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}
