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
  disabled: 'Обратный поиск по изображению не включён. Работал поиск по каталогам и доступным ссылкам.',
  not_configured: 'Ключ Google Cloud Vision отсутствует.',
  unavailable: 'Google Cloud Vision не ответил или отклонил запрос.',
  partial: 'Google Cloud Vision обработал часть изображений.',
  not_available: 'Не удалось подготовить растровые копии для Google Cloud Vision.',
}

const imageSearchLabels: Record<string, string> = {
  disabled: 'Не запускался', not_configured: 'Google Cloud Vision не настроен',
  unavailable: 'Google Cloud Vision не завершил проверку', not_available: 'Нет растровой копии для поиска',
  limit: 'Достигнут лимит изображений', found: 'Google Cloud Vision нашёл кандидатов',
  no_results: 'Google Cloud Vision не нашёл совпадений',
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
  const actionable = review.items.filter(item => (item.rights && item.rights.action !== 'unverified') || item.credit || item.replacement).length
  const failedLinks = review.linked_check?.pages?.filter(page => page.reason) ?? []
  return <section className="panel result-panel image-review">
    <div className="section-heading">
      <h2>Права на изображения</h2>
      {review.checked > 0 && <span className="code-pill">Готовых решений · {actionable}</span>}
    </div>
    {review.status === 'local_only'
      ? <p>Найдено изображений · {review.total}. Чтобы получить выводы и замены, повторите с включённым онлайн-поиском.</p>
      : <p className="muted">Поиск выполнен для {review.checked} из {review.total} уникальных изображений. Повторы объединены.</p>}
    {review.linked_check && (review.linked_check.eligible > 0 || review.linked_check.reference) && <p className="muted">
      По ссылкам{review.linked_check.reference ? ` из ${review.linked_check.reference}` : ''} проверено {review.linked_check.checked} из {review.linked_check.eligible} страниц и файлов. Совпадения и возможные совпадения найдены для {review.linked_check.matched} из {review.checked} изображений.
    </p>}
    {failedLinks.length > 0 && <details>
      <summary>Что не удалось проверить по ссылкам</summary>
      {failedLinks.map(page => <p key={page.url}><Link url={page.url}>Открыть ссылку</Link> · {page.reason}</p>)}
    </details>}
    {review.reverse_status && reverseLabels[review.reverse_status] && <p className="muted">{reverseLabels[review.reverse_status]}</p>}
    {Boolean(review.reverse_checked) && <p className="muted">Google Cloud Vision обработал {review.reverse_checked} из {review.checked} изображений.</p>}
    {review.items.map(item => {
      const choices = item.replacements ?? (item.replacement ? [item.replacement] : [])
      const rights = item.rights
      const exactSource = item.source && (!rights || ['bytes', 'pixels', 'transformed'].includes(item.source.match_kind ?? ''))
      return <article key={item.id} className="evidence-item image-action">
        <div className="image-action-heading">
          <h3>{rights ? rightsTitles[rights.action] : actions[item.status]}</h3>
          {rights && <span className="code-pill">{rights.label}</span>}
        </div>
        <p><strong>{item.locations.join(' · ')}</strong> · {item.name}</p>
        {item.reverse_search && review.status !== 'local_only' && <p className="muted">
          Поиск по картинке · {imageSearchLabels[item.reverse_search]}
        </p>}
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
        {item.web_matches.length > 0 && <details open>
          <summary>Совпадения в интернете · {item.web_matches.length}</summary>
          {item.web_matches.map(match => <div key={match.url}>
            <p><Link url={match.url}>{match.title}</Link> · {match.match}</p>
            {match.author && <p>Автор · {match.author}</p>}
            {!match.author && match.page_author && <p>Автор публикации · {match.page_author}</p>}
            {match.publisher && <p>Издатель · {match.publisher}</p>}
            {match.reason && <p className="muted">{match.reason}</p>}
            {match.verification === 'not_checked' && <p className="muted">Сайт не проверялся в пределах этой проверки.</p>}
            {match.terms && <p>{match.terms} {match.terms_url && <Link url={match.terms_url}>Условия площадки</Link>}</p>}
          </div>)}
        </details>}
      </article>
    })}
  </section>
}
