import { useRef, useState } from 'react'
import type { DragEvent, FormEvent } from 'react'
import { ArrowRight, FileText, Globe2, LoaderCircle, LockKeyhole, ScanLine, Upload, X } from 'lucide-react'
import { describeError, formatSize, request } from './api'
import type { AuditOptions, AuditSummary, UploadResult } from './api'
import { Notice, PageTitle } from './ui'

export const localOptions: AuditOptions = { online_search: false, ai_analysis: false, intended_use: 'unspecified' }
const allowedExtensions = new Set(['pdf', 'docx', 'pptx', 'html', 'htm', 'png', 'jpg', 'jpeg', 'webp', 'svg', 'zip'])

export function AuditConsent({ value, onChange, disabled = false }: {
  value: AuditOptions
  onChange: (value: AuditOptions) => void
  disabled?: boolean
}) {
  return <fieldset className="consent" disabled={disabled}>
    <legend>Параметры проверки</legend>
    <label className="intended-use">Планируемое использование
      <select value={value.intended_use ?? 'unspecified'} onChange={event => onChange({ ...value, intended_use: event.target.value as AuditOptions['intended_use'] })}>
        <option value="unspecified">Не определено</option>
        <option value="commercial">Коммерческое использование</option>
        <option value="redistribute_modified">Распространение изменённой версии</option>
        <option value="commercial_modified">Коммерческое использование изменённой версии</option>
      </select>
    </label>
    <h3>Внешние сервисы</h3>
    <p>Включайте эти параметры, если согласны передать указанные данные. При выключенных параметрах файл обрабатывается локально.</p>
    <label className="consent-choice">
      <input type="checkbox" checked={value.online_search} onChange={event => onChange({ ...value, online_search: event.target.checked })} />
      <Globe2 size={18} />
      <span><strong>Поиск источников онлайн</strong><small>В Wikimedia Commons передаются короткий фрагмент текста, заголовок из метаданных или имя изображения, SHA-256 файла, SHA-1 до двух изображений и имена файлов из ссылок на Commons. Для локального сравнения скачиваются публичные миниатюры Wikimedia.</small></span>
    </label>
    <label className="consent-choice">
      <input type="checkbox" checked={value.ai_analysis} onChange={event => onChange({ ...value, ai_analysis: event.target.checked })} />
      <ScanLine size={18} />
      <span><strong>Анализ с DeepSeek</strong><small>DeepSeek получит до 4 000 знаков извлечённого текста и краткие сведения о совпадениях и лицензиях. Ключ можно подключить в <a href="#/settings">настройках</a>.</small></span>
    </label>
  </fieldset>
}

export function NewAudit({ maxUploadMb, maxFiles = 10, onOpen }: {
  maxUploadMb?: number
  maxFiles?: number
  onOpen: (id: number) => void
}) {
  const picker = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [over, setOver] = useState(false)
  const [options, setOptions] = useState(localOptions)
  const [busy, setBusy] = useState(false)
  const [issues, setIssues] = useState<string[]>([])
  const [created, setCreated] = useState<AuditSummary[]>([])

  function addFiles(incoming: File[]) {
    if (busy) return
    const problems: string[] = []
    const accepted = incoming.filter(file => {
      const ext = file.name.split('.').at(-1)?.toLowerCase() ?? ''
      if (!allowedExtensions.has(ext)) {
        problems.push(`${file.name} — неподдерживаемый формат.`)
        return false
      }
      if (!file.size) {
        problems.push(`${file.name} — пустой файл.`)
        return false
      }
      if (maxUploadMb && file.size > maxUploadMb * 1048576) {
        problems.push(`${file.name} — размер больше ${maxUploadMb} МБ.`)
        return false
      }
      return true
    })
    setIssues(problems)
    setFiles(current => {
      const next = [...current]
      for (const file of accepted) {
        if (!next.some(item => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) next.push(file)
      }
      return next
    })
  }

  function handleDrop(event: DragEvent) {
    event.preventDefault()
    setOver(false)
    addFiles(Array.from(event.dataTransfer.files))
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!files.length || busy) return
    if (files.length > maxFiles) {
      setIssues([`За один раз можно проверить до ${maxFiles} файлов.`])
      return
    }
    if (maxUploadMb && files.reduce((total, file) => total + file.size, 0) > maxUploadMb * 1048576) {
      setIssues([`Общий размер файлов превышает ${maxUploadMb} МБ. Уберите часть файлов и запустите отдельную проверку.`])
      return
    }
    const data = new FormData()
    files.forEach(file => data.append('files', file))
    data.append('online_search', String(options.online_search))
    data.append('ai_analysis', String(options.ai_analysis))
    if (options.intended_use) data.append('intended_use', options.intended_use)
    setBusy(true)
    setIssues([])
    setCreated([])
    try {
      const response = await request<UploadResult>('/audits', { method: 'POST', body: data }, (status, body) => {
        if (status !== 422 || !body || typeof body !== 'object') return false
        return 'items' in body && Array.isArray(body.items) && 'errors' in body && Array.isArray(body.errors)
      })
      const errors = response.errors ?? []
      setIssues(errors.map(item => `${item.filename} — ${item.message}`))
      setCreated(response.items)
      if (response.items.length) {
        if (!errors.length) setFiles([])
        else setFiles(current => current.filter(file => errors.some(error => file.name === error.filename)))
      }
      if (!errors.length && response.items.length === 1 && window.location.hash === '#/new') onOpen(response.items[0].id)
    } catch (error) {
      setIssues([describeError(error)])
    } finally {
      setBusy(false)
    }
  }

  return <>
    <PageTitle title="Новая проверка" description="Добавьте файлы, чтобы извлечь факты о материале и собрать отчёт." />
    <form className="form-grid" onSubmit={submit} aria-busy={busy}>
      <section className="panel form-panel" aria-label="Файлы для проверки">
        <div className="section-heading"><h2>Материалы</h2>{files.length > 0 && <span className="muted">Выбрано {files.length}</span>}</div>
        <div className={`dropzone ${over ? 'dragging' : ''}`} onDragEnter={event => { event.preventDefault(); setOver(true) }}
          onDragOver={event => event.preventDefault()}
          onDragLeave={event => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setOver(false) }}
          onDrop={handleDrop}>
          <span className="drop-icon"><Upload size={25} strokeWidth={1.7} /></span>
          <strong>Перетащите файлы сюда</strong>
          <p>PDF, DOCX, PPTX, HTML, PNG, JPG, WEBP, SVG и ZIP</p>
          <button className="button button-secondary" type="button" onClick={() => picker.current?.click()} disabled={busy}>Выбрать файлы</button>
          <input ref={picker} id="audit-files" aria-label="Выбрать файлы" type="file" hidden multiple
            accept=".pdf,.docx,.pptx,.html,.htm,.png,.jpg,.jpeg,.webp,.svg,.zip" onChange={event => {
              addFiles(Array.from(event.currentTarget.files ?? []))
              event.currentTarget.value = ''
            }} disabled={busy} />
          <small>До {maxFiles} файлов{maxUploadMb ? ` · До ${maxUploadMb} МБ суммарно` : ''}</small>
        </div>

        {files.length > 0 && <ul className="selection-list">
          {files.map((file, index) => <li key={`${file.name}-${file.lastModified}-${file.size}`}>
            <span className="file-icon"><FileText size={18} /></span>
            <span className="file-label"><strong>{file.name}</strong><small>{formatSize(file.size)}</small></span>
            <button className="icon-button" type="button" aria-label={`Убрать ${file.name}`} onClick={() => setFiles(current => current.filter((_, position) => position !== index))} disabled={busy}><X size={18} /></button>
          </li>)}
        </ul>}
        {issues.length > 0 && <Notice error><ul className="messages">{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul></Notice>}
        {created.length > 0 && <div className="upload-results">
          <h3>Проверки готовы</h3>
          <ul>{created.map(audit => <li key={audit.id}><a className="text-link" href={`#/audits/${audit.id}`}>{audit.filename}<ArrowRight size={15} /></a></li>)}</ul>
          <a className="button button-secondary" href="#/history">Открыть историю</a>
        </div>}
      </section>

      <section className="panel form-panel audit-options" aria-label="Условия проверки">
        <AuditConsent value={options} onChange={setOptions} disabled={busy} />
        <p className="privacy"><LockKeyhole size={17} /> Исходные файлы сохраняются локально, чтобы проверку можно было повторить.</p>
        <button className="button button-primary submit-button" type="submit" disabled={busy || !files.length}>
          {busy ? <LoaderCircle className="spinning" size={18} /> : <ArrowRight size={18} />}
          {busy ? 'Выполняется проверка…' : 'Проверить материалы'}
        </button>
      </section>
    </form>
  </>
}
