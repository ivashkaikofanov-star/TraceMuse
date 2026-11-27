import { useEffect, useState } from 'react'
import { Activity, ArrowUpRight, BookOpen, CheckCircle2, Clock3, FileSearch, FolderOpen, Gauge, HelpCircle, History, LayoutDashboard, LockKeyhole, Plus, Search, Settings, ShieldCheck, Sparkles, UploadCloud } from 'lucide-react'

const nav = [
  { label: 'Рабочий кабинет', icon: LayoutDashboard },
  { label: 'Новая проверка', icon: FileSearch },
  { label: 'История проверок', icon: History },
  { label: 'Отчёты', icon: BookOpen },
  { label: 'Источники и лицензии', icon: FolderOpen },
  { label: 'Российский правовой контекст', icon: ShieldCheck },
]
export default function App() {
  const [active, setActive] = useState('Рабочий кабинет')
  const [apiState, setApiState] = useState('Проверяем локальный API…')
  useEffect(() => { fetch('http://127.0.0.1:8000/api/health').then(r => r.ok ? r.json() : Promise.reject()).then(() => setApiState('Локальный API доступен')).catch(() => setApiState('API пока не запущен')) }, [])
  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Sparkles size={19}/></div><div><b>TraceMuse</b><span>CONTENT INTELLIGENCE</span></div></div>
      <div className="workspace"><span className="workspace-dot"/> Локальное рабочее пространство <span className="chevron">⌄</span></div>
      <div className="nav-caption">РАБОЧАЯ ОБЛАСТЬ</div>
      <nav>{nav.map(({label,icon:Icon})=><button key={label} className={active===label?'nav-item active':'nav-item'} onClick={()=>setActive(label)}><Icon size={17}/><span>{label}</span>{label==='Новая проверка'&&<span className="nav-plus">+</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="help-card"><div className="help-icon"><HelpCircle size={17}/></div><b>Нужна помощь?</b><p>Узнайте, как TraceMuse анализирует ваши материалы.</p><a>О проекте <ArrowUpRight size={13}/></a></div><button className="nav-item"><Settings size={17}/>Настройки</button><div className="profile"><div className="avatar">TM</div><div><b>Локальный пользователь</b><span>Персональное пространство</span></div><span className="dots">···</span></div></div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="crumb">Рабочая область <span>/</span> <b>{active}</b></div><div className="top-actions"><div className="status"><span className="green-dot"/>{apiState}</div><button className="icon-btn"><HelpCircle size={18}/></button><div className="avatar small">TM</div></div></header>
      <div className="content">
        <div className="welcome-row"><div><div className="eyebrow">СРЕДА КОНТРОЛЯ ПРОИСХОЖДЕНИЯ КОНТЕНТА</div><h1>Добро пожаловать в TraceMuse</h1><p className="subtitle">Проверяйте происхождение ресурсов и выявляйте потенциальные риски до публикации.</p></div><button className="primary" onClick={()=>setActive('Новая проверка')}><Plus size={17}/> Новая проверка</button></div>
        <section className="hero"><div className="hero-copy"><div className="hero-tag"><Sparkles size={13}/> ВАШ ЦИФРОВОЙ АУДИТОР</div><h2>Создавайте с уверенностью.<br/><em>Публикуйте осознанно.</em></h2><p>TraceMuse помогает разобраться, откуда взялись элементы вашего проекта, на каких условиях их можно использовать и что требует дополнительной проверки.</p><button className="hero-btn" onClick={()=>setActive('Новая проверка')}>Начать проверку <ArrowUpRight size={16}/></button></div><div className="hero-art"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="art-card card-back"><div className="mini-line"/><div className="mini-line short"/><div className="mini-pill"/></div><div className="art-card card-front"><div className="scan-icon"><FileSearch size={27}/></div><div className="art-title">Анализ материалов</div><div className="art-sub">Источники · Лицензии · Контекст</div><div className="progress"><i/></div><div className="art-foot"><span>TraceMuse engine</span><CheckCircle2 size={15}/></div></div><div className="floating f-one"><ShieldCheck size={17}/><span>Контроль рисков</span></div><div className="floating f-two"><LockKeyhole size={16}/><span>Локальная обработка</span></div></div></section>
        <div className="section-heading"><div><h3>Обзор пространства</h3><p>Сводная информация о ваших проверках</p></div><span className="period">За всё время <span>⌄</span></span></div>
        <div className="stats"><div className="stat"><div className="stat-top"><span>Всего проверок</span><span className="stat-icon violet"><FileSearch size={17}/></span></div><strong>—</strong><small>Пока нет завершённых проверок</small></div><div className="stat"><div className="stat-top"><span>Найдено ресурсов</span><span className="stat-icon blue"><FolderOpen size={17}/></span></div><strong>—</strong><small>Появятся после первого анализа</small></div><div className="stat"><div className="stat-top"><span>Требуют внимания</span><span className="stat-icon amber"><Activity size={17}/></span></div><strong>—</strong><small>Риски ещё не оценивались</small></div><div className="stat"><div className="stat-top"><span>Последняя активность</span><span className="stat-icon green"><Clock3 size={17}/></span></div><strong className="stat-empty">Нет данных</strong><small>История появится здесь</small></div></div>
        <div className="lower-grid"><section className="panel recent"><div className="panel-head"><div><h3>Последние проверки</h3><p>Ваши недавние проекты и результаты</p></div><button className="text-btn" onClick={()=>setActive('История проверок')}>Вся история <ArrowUpRight size={14}/></button></div><div className="empty"><div className="empty-icon"><FileSearch size={24}/></div><b>Здесь пока пусто</b><p>Загрузите первый файл или укажите сайт, чтобы начать аудит ресурсов.</p><button className="outline" onClick={()=>setActive('Новая проверка')}><Plus size={15}/> Создать проверку</button></div></section><section className="panel quick"><div className="panel-head"><div><h3>Быстрый старт</h3><p>Что можно проверить</p></div></div><div className="quick-list"><div><span className="q-icon"><UploadCloud size={17}/></span><div><b>Документы и презентации</b><small>PDF, DOCX, PPTX</small></div><ArrowUpRight size={15}/></div><div><span className="q-icon"><FolderOpen size={17}/></span><div><b>Сайты и веб-страницы</b><small>HTML, ZIP, URL</small></div><ArrowUpRight size={15}/></div><div><span className="q-icon"><Gauge size={17}/></span><div><b>Изображения и графика</b><small>PNG, JPG, SVG, WEBP</small></div><ArrowUpRight size={15}/></div></div></section></div>
        <footer><span>© 2026 TraceMuse</span><span>Локальная среда · Данные остаются на вашем устройстве</span><span className="build">BUILD 0.1.0</span></footer>
      </div>
    </main>
  </div>
}
