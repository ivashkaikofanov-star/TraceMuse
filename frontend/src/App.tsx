import { useCallback, useEffect, useState } from 'react'
import {
  ArrowDownToLine, ArrowLeft, ArrowRight, Archive,
  ChevronRight, CircleAlert, FileSearch, FileText, FolderOpen, History, Image,
  LayoutGrid, LockKeyhole, Plus, Settings2, ShieldCheck,
} from 'lucide-react'
import { describeError, formatDate, formatSize, request } from './api'
import type { AuditSummary, Health } from './api'
import { NewAudit } from './NewAudit'
import { AuditDetail } from './AuditDetail'
import { SettingsPage } from './SettingsPage'
import { Notice, PageTitle, Risk } from './ui'

const nav = [
  { route: '/', label: 'Обзор', icon: LayoutGrid },
  { route: '/new', label: 'Новая проверка', icon: Plus },
  { route: '/history', label: 'История', icon: History },
  { route: '/reports', label: 'Отчёты', icon: FolderOpen },
] as const

function readRoute() {
  return window.location.hash.slice(1) || '/'
}

function navigate(path: string) {
  window.location.hash = path
}

function Brand() {
  return <a className="brand" href="#/" aria-label="TraceMuse, обзор">
    <span className="brand-icon"><FileSearch size={21} strokeWidth={2.3} /></span>
    <span>Trace<span>Muse</span></span>
  </a>
}

function Empty({ title, text }: { title: string, text: string }) {
  return <div className="empty-state">
    <span className="empty-icon"><FileSearch size={25} strokeWidth={1.55} /></span>
    <strong>{title}</strong>
    <p>{text}</p>
  </div>
}

function AuditTable({ items, reports = false }: { items: AuditSummary[], reports?: boolean }) {
  if (!items.length) return <Empty title={reports ? 'Отчётов пока нет' : 'Проверок пока нет'}
    text="После проверки здесь появятся результаты." />

  return <div className="audit-list">
    {items.map(item => <article className="audit-row" key={item.id}>
      <a className="audit-file" href={`#/audits/${item.id}`}>
        <span className="file-icon"><FileText size={18} /></span>
        <span className="file-label"><strong>{item.filename}</strong>
          <small>{[formatDate(item.created_at), item.format, item.size_bytes != null && formatSize(item.size_bytes)].filter(Boolean).join(' · ')}</small>
        </span>
      </a>
      <Risk value={item.risk_level} />
      {reports ? <div className="report-actions">
        <a className="button button-quiet button-compact" href={`/api/audits/${item.id}/report.pdf`} title={`Скачать PDF для ${item.filename}`} download><ArrowDownToLine size={15} />PDF</a>
        <a className="button button-quiet button-compact" href={`/api/audits/${item.id}/report.json`} title={`Скачать JSON для ${item.filename}`} download><ArrowDownToLine size={15} />JSON</a>
      </div> : <a className="icon-button row-arrow" href={`#/audits/${item.id}`} aria-label={`Открыть проверку ${item.filename}`}><ChevronRight size={19} /></a>}
    </article>)}
  </div>
}

function Dashboard({ items, loading, error }: { items: AuditSummary[], loading: boolean, error: string }) {
  const stat = (value: number | string) => loading || error ? '—' : value
  const stats = [
    { label: 'Всего проверок', value: stat(items.length), icon: FileSearch },
    { label: 'Требуют внимания', value: stat(items.filter(item => ['high', 'medium', 'manual_review', 'insufficient_data'].includes(item.risk_level ?? '')).length), icon: CircleAlert },
    { label: 'Последняя проверка', value: stat(items.length ? formatDate(items[0].created_at) : '—'), icon: History },
  ]

  return <>
    <section className="hero" aria-labelledby="hero-heading">
      <div className="hero-image" aria-hidden="true" />
      <div className="hero-content">
        <div className="hero-eyebrow"><span /> АУДИТ ЦИФРОВЫХ МАТЕРИАЛОВ</div>
        <h1 id="hero-heading">Проверьте материал<br /> <em>до публикации.</em></h1>
        <p>Извлекайте содержимое файлов, ищите источники и проверяйте условия использования.</p>
        <div className="button-group"><a className="button button-primary" href="#/new"><Plus size={19} /> Новая проверка</a></div>
      </div>
      <span className="hero-corner hero-corner-top" aria-hidden="true" />
      <span className="hero-corner hero-corner-bottom" aria-hidden="true" />
    </section>

    <section className="overview" aria-labelledby="overview-heading">
      <div className="section-heading"><h2 id="overview-heading">Обзор пространства</h2><span className="muted">За всё время</span></div>
      <div className="stats">
        {stats.map(({ label, value, icon: Icon }) => <div className="stat" key={label}>
          <div className="stat-title"><Icon size={18} strokeWidth={1.7} />{label}</div>
          <strong className={typeof value === 'string' && value !== '—' ? 'stat-date' : undefined}>{value}</strong>
        </div>)}
      </div>
    </section>

    <div className="dashboard-grid">
      <section className="panel activity-panel" aria-labelledby="activity-heading">
        <div className="section-heading panel-heading">
          <h2 id="activity-heading">Последние проверки</h2>
          <a className="text-link" href="#/history">Вся история <ArrowRight size={15} /></a>
        </div>
        {error ? <Notice error>{error}</Notice> : loading ? <Notice>Загружаем историю…</Notice> : <AuditTable items={items.slice(0, 5)} />}
      </section>
      <section className="panel formats-panel" aria-labelledby="formats-heading">
        <div className="section-heading panel-heading"><h2 id="formats-heading">Поддерживаемые форматы</h2></div>
        <ul className="format-list">
          <li><FileText size={20} /><div><strong>Документы</strong><span>PDF, DOCX, PPTX</span></div></li>
          <li><Image size={20} /><div><strong>Изображения</strong><span>PNG, JPG, WEBP, SVG</span></div></li>
          <li><Archive size={20} /><div><strong>Веб-страницы и архивы</strong><span>HTML, ZIP</span></div></li>
        </ul>
      </section>
    </div>
  </>
}

export default function App() {
  const [route, setRoute] = useState(readRoute)
  const [health, setHealth] = useState<Health | null>(null)
  const [healthPending, setHealthPending] = useState(true)
  const [items, setItems] = useState<AuditSummary[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState('')
  const detailMatch = /^\/audits\/(\d+)$/.exec(route)
  const needsHistory = route === '/' || route === '/history' || route === '/reports'

  useEffect(() => {
    const update = () => {
      setRoute(readRoute())
      window.scrollTo(0, 0)
    }
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    const check = () => request<Health>('/health', { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setHealth(result)
    }).catch(() => {
      if (!controller.signal.aborted) setHealth(null)
    }).finally(() => {
      if (!controller.signal.aborted) setHealthPending(false)
    })
    void check()
    const timer = window.setInterval(check, 15000)
    return () => {
      window.clearInterval(timer)
      controller.abort()
    }
  }, [])

  const loadHistory = useCallback((signal: AbortSignal) => {
    setHistoryLoading(true)
    setHistoryError('')
    return request<{ items: AuditSummary[] }>('/audits', { signal }).then(result => {
      if (!signal.aborted) setItems(result.items)
    }).catch(error => {
      if (!signal.aborted) setHistoryError(describeError(error))
    }).finally(() => {
      if (!signal.aborted) setHistoryLoading(false)
    })
  }, [])

  useEffect(() => {
    if (!needsHistory) return
    const controller = new AbortController()
    void loadHistory(controller.signal)
    return () => controller.abort()
  }, [needsHistory, health?.status, loadHistory])

  const title = detailMatch ? 'Результаты проверки' : route === '/settings' ? 'Настройки' : nav.find(item => item.route === route)?.label ?? 'Страница не найдена'
  useEffect(() => { document.title = `${title} · TraceMuse` }, [title])

  let page: React.ReactNode
  if (route === '/') page = <Dashboard items={items} loading={historyLoading} error={historyError} />
  else if (route === '/new') page = <NewAudit maxUploadMb={health?.max_upload_mb} maxFiles={health?.max_files} onOpen={(id) => navigate(`/audits/${id}`)} />
  else if (route === '/history' || route === '/reports') {
    const reports = route === '/reports'
    page = <>
      <PageTitle title={reports ? 'Отчёты' : 'История проверок'} description={reports ? 'Скачивайте результаты в PDF для просмотра или в JSON для обработки.' : 'Открывайте результаты сохранённых проверок и запускайте их повторно.'}
        actions={<a className="button button-primary" href="#/new"><Plus size={18} /> Новая проверка</a>} />
      <section className="panel history-panel" aria-label={title}>
        {historyError ? <Notice error>{historyError}</Notice> : historyLoading ? <Notice>Загружаем историю…</Notice> : <AuditTable items={reports ? items.filter(item => item.status === 'completed') : items} reports={reports} />}
      </section>
    </>
  } else if (detailMatch) {
    page = <AuditDetail key={detailMatch[1]} id={Number(detailMatch[1])} onRerun={(id) => navigate(`/audits/${id}`)} onDelete={() => navigate('/history')} />
  } else if (route === '/settings') page = <SettingsPage />
  else page = <><PageTitle title="Страница не найдена" /><a className="button button-secondary" href="#/"><ArrowLeft size={17} /> На обзор</a></>

  return <div className="shell">
    <aside className="sidebar">
      <Brand />
      <nav className="nav" aria-label="Рабочая область">
        {nav.map(({ route: path, label, icon: Icon }) => {
          const active = route === path || (path === '/history' && Boolean(detailMatch))
          return <a key={path} className={active ? 'nav-item active' : 'nav-item'} href={`#${path}`} aria-current={active ? 'page' : undefined}>
            <Icon size={19} strokeWidth={1.75} /><span>{label}</span>
          </a>
        })}
        <a className={route === '/settings' ? 'nav-item nav-settings active' : 'nav-item nav-settings'} href="#/settings" aria-current={route === '/settings' ? 'page' : undefined}><Settings2 size={19} strokeWidth={1.75} /><span>Настройки</span></a>
      </nav>
      <div className="sidebar-footer"><LockKeyhole size={17} /><div><strong>Локальная обработка</strong><span>Внешние сервисы по согласию</span></div></div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <div className="breadcrumbs"><span>Рабочая область</span><ChevronRight size={14} /><b>{title}</b></div>
        <div className={`connection ${health?.status === 'ok' ? 'online' : ''}`} role="status"><span />{healthPending ? 'Подключаемся…' : health?.status === 'ok' ? 'Локальный API доступен' : 'Локальный API недоступен'}</div>
      </header>
      <main className="content" id="content">
        {page}
        <footer className="footer"><span>TraceMuse</span><span><ShieldCheck size={15} /> Результаты предварительной проверки</span></footer>
      </main>
    </div>
  </div>
}
