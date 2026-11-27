import { useEffect, useState } from 'react'
import { ArrowDownToLine, ArrowLeft, ArrowUpRight, FileSearch, LoaderCircle, RotateCcw, Trash2 } from 'lucide-react'
import { describeError, formatDate, formatSize, json, request, safeHref, statusLabel } from './api'
import type { Audit, AuditSummary, Extraction, Registry, Source } from './api'
import { Notice, PageTitle, Risk } from './ui'
import { AuditConsent, localOptions } from './NewAudit'

function ExternalLink({ url, children }: { url: string, children: React.ReactNode }) {
  const href = safeHref(url)
  return href ? <a className="text-link external-link" href={href} target="_blank" rel="noreferrer">{children}<ArrowUpRight size={15} /></a> : <span>{children}</span>
}

function BulletList({ items }: { items?: string[] }) {
  return items?.length ? <ul className="bullet-list">{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : null
}

function Warnings({ lines }: { lines?: string[] }) {
  return lines?.length ? <div className="evidence-warnings"><BulletList items={lines} /></div> : null
}

function displayValue(value: unknown) {
  if (value == null) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return typeof value === 'boolean' ? value ? 'Да' : 'Нет' : String(value)
}

const metadataNames: Record<string, string> = {
  title: 'Заголовок', author: 'Автор в метаданных', creator: 'Создатель в метаданных',
  subject: 'Тема', language: 'Язык', description: 'Описание', creation_date: 'Создан',
  modification_date: 'Изменён', keywords: 'Ключевые слова', producer: 'Программа',
  width: 'Ширина', height: 'Высота', pages: 'Страницы', slides: 'Слайды',
  copyright: 'Строка об авторском праве', license: 'Лицензия в метаданных',
  file_count: 'Файлов в архиве', expanded_bytes: 'Объём распаковки в байтах', frames: 'Кадры', encoding: 'Кодировка',
}

function Extracted({ data }: { data: Extraction }) {
  const metadata = Object.entries(data.metadata ?? {})
  return <section className="panel result-panel">
    <div className="section-heading"><h2>Извлечённые данные</h2>{data.format && <span className="code-pill">{data.format}</span>}</div>
    <Warnings lines={data.warnings} />
    <div className="evidence-group">
      <h3>Текст</h3>
      {data.text ? <pre className="extract-text" tabIndex={0}>{data.text}</pre> : <p className="muted">Текст не извлечён.</p>}
    </div>
    <div className="evidence-group">
      <h3>Ссылки <span className="counter">{data.links?.length ?? 0}</span></h3>
      {data.links?.length ? <ul className="link-list">
        {data.links.map((link, index) => <li key={index}><ExternalLink url={link}>{link}</ExternalLink></li>)}
      </ul> : <p className="muted">Ссылки не обнаружены.</p>}
    </div>
    <div className="evidence-group">
      <h3>Метаданные</h3>
      {metadata.length ? <dl className="fact-grid">{metadata.map(([key, value]) => <div key={key}><dt>{metadataNames[key] ?? key}</dt><dd>{displayValue(value)}</dd></div>)}</dl> : <p className="muted">Метаданные не обнаружены.</p>}
    </div>
    <div className="evidence-group">
      <h3>Изображения <span className="counter">{data.images?.length ?? 0}</span></h3>
      {data.images?.length ? <ul className="image-list">
        {data.images.map((image, index) => <li key={index}>
          <strong>{image.name}</strong>
          {image.location && <span>{image.location}</span>}
          {image.width && image.height && <span>{image.width} × {image.height} пикселей</span>}
          <code>SHA-256 {image.sha256}</code>
          {image.sha1 && <code>SHA-1 {image.sha1}</code>}
          {image.phash && <code title="Перцептивный хеш">pHash {image.phash}</code>}
        </li>)}
      </ul> : <p className="muted">Изображения не извлечены.</p>}
    </div>
    {data.entries && <details className="evidence-group archive-inventory">
      <summary>Файлы в архиве <span className="counter">{data.entries.length}</span></summary>
      {data.entries.length ? <ul className="image-list">
        {data.entries.map(item => <li key={item.name}>
          <strong>{item.name}</strong>
          <span>{[item.format, formatSize(item.size), statusLabel(item.status)].filter(Boolean).join(' · ')}</span>
          {(item.message || item.reason) && <span>{item.message || statusLabel(item.reason)}</span>}
          {item.sha256 && <code>SHA-256 {item.sha256}</code>}
        </li>)}
      </ul> : <p>В архиве нет файлов.</p>}
    </details>}
  </section>
}

function SourceItem({ item }: { item: Source }) {
  const license = item.license
  const rights = [item.creator ? ['Автор по данным источника', item.creator] : null, item.rightsholder ? ['Правообладатель', displayValue(item.rightsholder)] : null]
  return <article className="evidence-item">
    <h3><ExternalLink url={item.url}>{item.title || item.url}</ExternalLink></h3>
    {item.match && <p>{item.match}</p>}
    <div className="evidence-meta">
      {item.confidence && <span>Уверенность: {statusLabel(item.confidence)}</span>}
      {item.checked_at && <span>{formatDate(item.checked_at)}</span>}
    </div>
    <dl className="fact-grid">
      <div><dt>Лицензия</dt><dd>{!license ? 'Не установлена' : typeof license === 'string' ? license :
        <>{license.url ? <ExternalLink url={license.url}>{license.name}</ExternalLink> : license.name}{license.status && <small>{statusLabel(license.status)}</small>}</>}</dd></div>
      {rights.map(pair => pair && <div key={pair[0]}><dt>{pair[0]}</dt><dd>{pair[1]}</dd></div>)}
      {typeof license === 'object' && license && <>
        <div><dt>Коммерческое использование</dt><dd>{statusLabel(license.commercial_use)}</dd></div>
        <div><dt>Изменение</dt><dd>{statusLabel(license.modification)}</dd></div>
        <div><dt>Атрибуция</dt><dd>{statusLabel(license.attribution)}</dd></div>
      </>}
    </dl>
    {typeof license === 'object' && license && <Warnings lines={license.restrictions} />}
  </article>
}

function RegistryItem({ item }: { item: Registry }) {
  return <article className="evidence-item">
    <h3><ExternalLink url={item.url}>{item.title}</ExternalLink></h3>
    <div className="evidence-meta">
      <span>{statusLabel(item.status)}</span>
      {item.checked_at && <span>{formatDate(item.checked_at)}</span>}
      {item.snapshot_date && <span>Дата выгрузки {formatDate(item.snapshot_date)}</span>}
      {item.confidence && <span>{statusLabel(item.confidence)}</span>}
    </div>
    {item.match && <p>{item.match}</p>}
  </article>
}

export function AuditDetail({ id, onRerun, onDelete }: { id: number, onRerun: (id: number) => void, onDelete: () => void }) {
  const [audit, setAudit] = useState<Audit | null>(null)
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [options, setOptions] = useState(localOptions)
  const [activeAction, setActiveAction] = useState('')
  const [confirmDeletion, setConfirmDeletion] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    request<Audit>(`/audits/${id}`, { signal: controller.signal }).then(setAudit).catch(error => {
      if (!controller.signal.aborted) setLoadError(describeError(error))
    })
    return () => controller.abort()
  }, [id])

  async function rerun() {
    if (activeAction) return
    setActiveAction('rerun')
    setActionError('')
    try {
      const result = await request<AuditSummary>(`/audits/${id}/rerun`, json('POST', options))
      onRerun(result.id)
    } catch (error) {
      setActionError(describeError(error))
    } finally {
      setActiveAction('')
    }
  }

  async function remove() {
    if (activeAction || !confirmDeletion) return
    setActiveAction('delete')
    setActionError('')
    try {
      await request(`/audits/${id}`, { method: 'DELETE' })
      onDelete()
    } catch (error) {
      setActionError(describeError(error))
    } finally {
      setActiveAction('')
    }
  }

  const result = audit?.result
  const assessment = result?.assessment
  const sources = result?.sources
  const legal = result?.legal
  const ai = result?.ai
  const downloadButtons = audit?.status === 'completed' && <>
    <a className="button button-primary" href={`/api/audits/${id}/report.pdf`} download><ArrowDownToLine size={17} /> PDF</a>
    <a className="button button-secondary" href={`/api/audits/${id}/report.json`} download><ArrowDownToLine size={17} /> JSON</a>
  </>

  const back = <a className="subnav text-link" href="#/history"><ArrowLeft size={16} /> История проверок</a>
  if (loadError) return <>{back}<PageTitle title={`Проверка №${id}`} /><Notice error>{loadError}</Notice></>
  if (!audit) return <>{back}<PageTitle title={`Проверка №${id}`} /><Notice>Получаем результаты…</Notice></>

  return <>
    {back}
    <PageTitle title={audit.filename} description={`Проверка №${audit.id} · ${formatDate(audit.created_at)} · ${statusLabel(audit.status)}`} actions={downloadButtons} />
    <section className="panel result-panel verdict">
      <div className="section-heading"><h2>Оценка риска</h2><Risk value={assessment?.level ?? audit.risk_level} /></div>
      <p>{assessment?.reason ?? 'По этой проверке оценка риска отсутствует.'}</p>
      {assessment?.confidence && <p className="assessment-confidence">Уверенность оценки — {statusLabel(assessment.confidence)}</p>}
      <dl className="fact-grid verdict-facts">
        <div><dt>Формат</dt><dd>{audit.format ?? result?.extraction?.format ?? '—'}</dd></div>
        <div><dt>Размер</dt><dd>{formatSize(audit.size_bytes)}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{audit.sha256 ?? '—'}</code></dd></div>
      </dl>
      {Boolean(assessment?.risks?.length) && <div className="evidence-group"><h3>Обнаруженные риски</h3><BulletList items={assessment?.risks?.map(displayValue)} /></div>}
      {Boolean(assessment?.recommendations?.length) && <div className="evidence-group"><h3>Рекомендации</h3><BulletList items={assessment?.recommendations} /></div>}
      {Boolean(assessment?.limitations?.length) && <div className="evidence-group"><h3>Ограничения проверки</h3><BulletList items={assessment?.limitations} /></div>}
    </section>

    <div className="results-grid">
      {result?.extraction && <Extracted data={result.extraction} />}
      <div className="results-stack">
        <section className="panel result-panel">
          <div className="section-heading"><h2>Источники и лицензии</h2>{sources && <span className="code-pill">{statusLabel(sources.status)}</span>}</div>
          <Warnings lines={sources?.warnings} />
          {sources?.items?.length ? sources.items.map((item, index) => <SourceItem key={index} item={item} />) : <p className="muted">В отчёте нет источников.</p>}
        </section>
        <section className="panel result-panel">
          <div className="section-heading"><h2>Правовые сведения</h2>{legal && <span className="code-pill">{statusLabel(legal.status)}</span>}</div>
          <Warnings lines={legal?.warnings} />
          {legal?.items?.length ? legal.items.map((item, index) => <RegistryItem key={index} item={item} />) : <p className="muted">В отчёте нет сведений из реестров.</p>}
        </section>
        <section className="panel result-panel">
          <div className="section-heading"><h2>Анализ DeepSeek</h2>{ai && <span className="code-pill">{statusLabel(ai.status)}</span>}</div>
          {ai?.summary && <p>{ai.summary}</p>}
          <BulletList items={ai?.observations?.map(displayValue)} />
          <Warnings lines={ai?.warnings} />
          {!ai && <p className="muted">В отчёте нет анализа DeepSeek.</p>}
        </section>
      </div>
    </div>

    <section className="panel result-panel manage-panel">
      <details>
        <summary><RotateCcw size={18} /> Повторить проверку</summary>
        <AuditConsent value={options} onChange={setOptions} disabled={Boolean(activeAction)} />
        <button className="button button-secondary" type="button" onClick={rerun} disabled={Boolean(activeAction)}>{activeAction === 'rerun' ? <LoaderCircle className="spinning" size={18} /> : <FileSearch size={17} />} Запустить повторно</button>
      </details>
      <button className="button button-danger" type="button" onClick={() => setConfirmDeletion(!confirmDeletion)} disabled={Boolean(activeAction)}><Trash2 size={17} /> Удалить проверку</button>
      {confirmDeletion && <div className="confirmation">
        <p>Удалить запись №{id} и её отчёт?</p>
        <div className="button-group"><button className="button button-danger" type="button" onClick={remove} disabled={Boolean(activeAction)}>Удалить</button>
          <button className="button button-quiet" type="button" onClick={() => setConfirmDeletion(false)} disabled={Boolean(activeAction)}>Отменить</button></div>
      </div>}
      {actionError && <Notice error>{actionError}</Notice>}
    </section>
  </>
}
