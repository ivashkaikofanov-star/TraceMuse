import { useState } from 'react'
import { Copy, ExternalLink } from 'lucide-react'
import { safeHref } from './api'
import type { ImageReview, LicensedImage } from './api'

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

const rightsTitles = {
  keep: 'Можно оставить без подписи',
  credit: 'Можно оставить с подписью',
  conditional: 'Нужно соблюсти дополнительные условия',
  restricted: 'Коммерческое использование не разрешено',
  unverified: 'Разрешение не установлено',
}

function Photo({ preview, label }: { preview?: string, label: string }) {
  return preview
    ? <img className="report-photo" src={`data:image/jpeg;base64,${preview}`} alt={label} loading="lazy" />
    : <p className="photo-unavailable muted">Предпросмотр отсутствует.</p>
}

function Replacement({ material, comparable }: { material: LicensedImage, comparable: boolean }) {
  const title = material.kind === 'no_credit' ? 'Замена без подписи' :
    comparable ? 'Более похожая замена с подписью' : material.kind ? 'Замена с подписью' : 'Вариант замены'
  return <div className="image-replacement">
    <h4>{title}</h4>
    <Photo preview={material.preview} label={material.title} />
    {material.kind && <p>{material.kind === 'no_credit'
      ? 'Подходит для коммерческого использования по указанным условиям. Атрибуция не обязательна.'
      : 'Для коммерческого использования добавьте готовую подпись.'}</p>}
    {material.license.usage_note && <p className="muted">{material.license.usage_note}</p>}
    <div className="image-action-links">
      <Link url={material.url}>{material.title}</Link>
      {material.download_url && <Link url={material.download_url}>Открыть файл</Link>}
      {material.license.url && <Link url={material.license.url}>{material.license.name}</Link>}
    </div>
    {material.credit && <Credit text={material.credit} />}
  </div>
}

export function ImageReviewSection({ review }: { review: ImageReview }) {
  if (review.status === 'no_images') return null
  const actionable = review.items.filter(item => item.rights?.action === 'keep' || item.credit || item.replacement).length
  return <section className="panel result-panel image-review">
    <div className="section-heading">
      <h2>Права на изображения</h2>
      {review.checked > 0 && <span className="code-pill">Готовых решений · {actionable}</span>}
    </div>
    {review.status === 'local_only'
      ? <p>Найдено изображений · {review.total}. Чтобы получить выводы и замены, повторите с включённым онлайн-поиском.</p>
      : <p className="muted">Поиск выполнен для {review.checked} из {review.total} уникальных изображений. Повторы объединены.</p>}
    {review.reverse_status && reverseLabels[review.reverse_status] && <p className="muted">{reverseLabels[review.reverse_status]}</p>}
    {review.items.map(item => {
      const choices = item.replacements ?? (item.replacement ? [item.replacement] : [])
      const rights = item.rights
      const exactSource = item.source && (!rights || item.source.match_kind === 'bytes' || item.source.match_kind === 'pixels')
      return <article key={item.id} className="evidence-item image-action">
        <div className="image-action-heading">
          <h3>{rights ? rightsTitles[rights.action] : actions[item.status]}</h3>
          {rights && <span className="code-pill">{rights.label}</span>}
        </div>
        <p><strong>{item.locations.join(' · ')}</strong> · {item.name}</p>
        <div className="image-options">
          <div className="image-original">
            <h4>Исходное изображение</h4>
            <Photo preview={item.original_preview} label={`Исходное изображение · ${item.name}`} />
            <p>{rights?.detail ?? item.reason}</p>
            {rights?.warning && <p className="photo-warning">{rights.warning}</p>}
            {rights?.changes && <p className="muted">{rights.changes}</p>}
            {item.source && <div className="image-action-links">
              <Link url={item.source.url}>{exactSource ? 'Страница исходника' : 'Похожая публикация'}</Link>
              {exactSource && item.source.license.url && <Link url={item.source.license.url}>{item.source.license.name}</Link>}
            </div>}
            {item.credit && rights?.action !== 'keep' && <Credit text={item.credit} />}
          </div>
          {choices.map(material => <Replacement key={material.url} material={material} comparable={choices.length > 1} />)}
        </div>
        {rights && rights.action !== 'keep' && !choices.some(candidate => candidate.kind === 'no_credit') && review.status !== 'local_only' &&
          <p className="muted">Подходящая замена без подписи не найдена.</p>}
        {item.web_matches.length > 0 && <details>
          <summary>Совпадения в интернете · {item.web_matches.length}</summary>
          {item.web_matches.map(match => <p key={match.url}><Link url={match.url}>{match.title}</Link> · {match.match}</p>)}
        </details>}
      </article>
    })}
  </section>
}
