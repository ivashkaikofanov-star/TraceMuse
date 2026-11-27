import { useState } from 'react'
import { Copy, ExternalLink } from 'lucide-react'
import { safeHref } from './api'
import type { ImageReview } from './api'

const actions = {
  credit: 'Добавьте подпись',
  reuse: 'Можно использовать по указанным условиям',
  replace: 'Предложена замена',
  review: 'Разрешение не подтверждено',
  no_query: 'Недостаточно признаков для поиска',
}

const reverseLabels: Record<string, string> = {
  not_configured: 'Ключ Google Cloud Vision отсутствует.',
  unavailable: 'Google Cloud Vision не ответил или отклонил запрос.',
  partial: 'Google Cloud Vision обработал часть изображений.',
  not_available: 'Не удалось подготовить растровые копии для Google Cloud Vision.',
}

function Link({ url, children }: { url: string, children: string }) {
  const href = safeHref(url)
  return href ? <a className="text-link" href={href} target="_blank" rel="noreferrer">{children} <ExternalLink size={13} /></a> : null
}

function Credit({ text }: { text: string }) {
  const [copied, setCopied] = useState('')
  return <div className="image-credit">
    <p>{text}</p>
    <button type="button" className="button button-secondary" onClick={async () => {
      try {
        await navigator.clipboard.writeText(text)
        setCopied('Подпись скопирована.')
      } catch {
        setCopied('Выделите текст подписи и скопируйте его.')
      }
    }}><Copy size={14} /> Скопировать подпись</button>
    {copied && <span className="muted" role="status">{copied}</span>}
  </div>
}

export function ImageReviewSection({ review }: { review: ImageReview }) {
  if (review.status === 'no_images') return null
  const actionable = review.items.filter(item => item.credit || item.replacement).length
  return <section className="panel result-panel image-review">
    <div className="section-heading">
      <h2>Что сделать с изображениями</h2>
      {review.checked > 0 && <span className="code-pill">Готовых действий · {actionable}</span>}
    </div>
    {review.status === 'local_only' ? <p>Извлечено уникальных изображений · {review.total}. Чтобы получить подписи и альтернативы, повторите проверку с онлайн-поиском.</p> :
      <>
        <p className="muted">Карточек · {review.checked} из {review.total} уникальных изображений. Повторяющиеся изображения проверены вместе.</p>
        {review.checked < review.total && <p className="muted">В этом запуске разобраны первые {review.checked} изображений. Остальные можно проверить отдельным файлом.</p>}
        {review.model_status === 'not_configured' && <p className="muted">Ключ ИИ отсутствует. Запросы построены из доступных сведений о файле.</p>}
        {review.model_status === 'unavailable' && <p className="muted">Модель не вернула поисковые запросы. Использованы доступные сведения о файле.</p>}
        {review.reverse_status && reverseLabels[review.reverse_status] && <p className="muted">{reverseLabels[review.reverse_status]}</p>}
        {Boolean(review.reverse_checked) && <p className="muted">Обратным поиском обработано · {review.reverse_checked} изображений.</p>}
      </>}
    {review.items.map(item => <article key={item.id} className="evidence-item image-action">
      <div className="image-action-heading"><h3>{actions[item.status]}</h3><span className="code-pill">{item.id}</span></div>
      <p><strong>{item.locations.join(' · ')}</strong> · {item.name}</p>
      <p>{item.reason}</p>
      {item.source && <div className="image-action-links">
        <Link url={item.source.url}>{item.source.title}</Link>
        {item.source.match && <span className="muted">{item.source.match}</span>}
        {item.source.license.url && <Link url={item.source.license.url}>{'Условия источника · ' + item.source.license.name}</Link>}
      </div>}
      {item.credit && <Credit text={item.credit} />}
      {item.replacement && <div className="image-replacement">
        <h4>Вариант на замену</h4>
        <div className="image-action-links">
          <Link url={item.replacement.url}>{item.replacement.title}</Link>
          {item.replacement.download_url && <Link url={item.replacement.download_url}>Открыть файл</Link>}
          {item.replacement.license.url && <Link url={item.replacement.license.url}>{item.replacement.license.name}</Link>}
        </div>
        {item.replacement.credit && <Credit text={item.replacement.credit} />}
      </div>}
      {item.web_matches.length > 0 && <details>
        <summary>Совпадения в интернете · {item.web_matches.length}</summary>
        {item.web_matches.map(match => <p key={match.url}><Link url={match.url}>{match.title}</Link> · {match.match}</p>)}
      </details>}
    </article>)}
  </section>
}
