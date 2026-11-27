export interface AuditSummary {
  id: number
  filename: string
  status: string
  created_at: string
  sha256?: string | null
  size_bytes?: number | null
  risk_level?: string | null
  format?: string | null
}

export interface Source {
  title: string
  url: string
  checked_at?: string
  match?: string
  confidence?: string
  license?: string | {
    name: string
    url?: string
    status?: string
    commercial_use?: string
    modification?: string
    attribution?: string
    restrictions?: string[]
  } | null
  creator?: string | null
  [key: string]: unknown
}

export interface Registry {
  title: string
  url: string
  status?: string
  checked_at?: string
  match?: string
  snapshot_date?: string
  confidence?: string
  [key: string]: unknown
}

export interface Extraction {
  format: string | null
  text: string
  links: string[]
  images: Array<{ name: string, sha256: string, sha1?: string, phash?: string, width?: number, height?: number, location?: string }>
  entries?: Array<{ name: string, size: number, status: string, format?: string, reason?: string, message?: string, sha256?: string }>
  metadata: Record<string, unknown>
  warnings: string[]
  [key: string]: unknown
}

interface EvidenceSection {
  status: string
  warnings: string[]
  [key: string]: unknown
}

export interface Audit extends AuditSummary {
  result: {
    extraction?: Extraction
    sources?: EvidenceSection & { items: Source[] }
    legal?: EvidenceSection & { items: Registry[] }
    ai?: EvidenceSection & { summary?: string, observations?: unknown[] }
    assessment?: { level: string, reason: string, recommendations: string[], limitations: string[], confidence?: string, risks?: unknown[] }
    [key: string]: unknown
  } | null
}

export interface AuditOptions {
  online_search: boolean
  ai_analysis: boolean
  intended_use?: 'unspecified' | 'commercial' | 'redistribute_modified' | 'commercial_modified'
}

export interface UploadResult {
  items: AuditSummary[]
  errors: Array<{ filename: string, message: string }>
}

export interface Settings {
  deepseek_configured: boolean
  deepseek_model: string
  legal_registries: Registry[]
}

export interface Health {
  status: string
  max_upload_mb: number
  max_files?: number
}

export async function request<T>(path: string, init?: RequestInit, accept?: (status: number, body: unknown) => boolean): Promise<T> {
  const response = await fetch('/api' + path, init)
  if (response.status === 204) return undefined as T

  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: unknown } | null
    if (accept?.(response.status, body)) return body as T
    const message = typeof body?.detail === 'string' ? body.detail : undefined
    throw new Error(message ?? `Сервис вернул ошибку (HTTP ${response.status}).`)
  }

  try {
    return await response.json() as T
  } catch {
    throw new Error('Сервис вернул ответ в неожиданном формате.')
  }
}

export function json(method: string, data: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }
}

export function safeHref(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined
  } catch {
    return undefined
  }
}

export function describeError(error: unknown): string {
  if (error instanceof TypeError) return 'Нет связи с TraceMuse. Проверьте, запущен ли локальный сервер.'
  return error instanceof Error ? error.message : 'Не удалось выполнить действие.'
}

export function formatDate(value?: string | null): string {
  if (!value) return '—'
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(value))
  }
  const iso = value.includes('T') ? value : value.replace(' ', 'T')
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : iso + 'Z')
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

export function formatSize(bytes?: number | null): string {
  if (bytes == null) return '—'
  const unit = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })
  if (bytes >= 1048576) return `${unit.format(bytes / 1048576)} МБ`
  if (bytes >= 1024) return `${unit.format(bytes / 1024)} КБ`
  return `${bytes} Б`
}

const statusLabels: Record<string, string> = {
  completed: 'Завершена', created: 'Создана', failed: 'Ошибка', error: 'Ошибка',
  partial: 'Частично', available: 'Доступно', unavailable: 'Недоступно',
  matched: 'Найдены совпадения', no_matches: 'Совпадений не найдено',
  checked: 'Выполнено', ok: 'Подключено', success: 'Выполнено',
  not_checked: 'Не проверялось', not_requested: 'Не запрашивалось', disabled: 'Отключено',
  skipped: 'Пропущено', manual_review: 'Требует ручной проверки',
  not_configured: 'Требуется настройка', insufficient_data: 'Недостаточно данных',
  unknown: 'Не установлено', limited: 'Ограниченная', high: 'Высокая', medium: 'Средняя', low: 'Низкая',
  local_only: 'Локальная проверка', no_match: 'Совпадений не найдено', complete: 'Проверено',
  local_snapshot: 'Сверка с локальной выгрузкой',
  local_snapshot_match: 'Совпадение в локальной выгрузке',
  local_snapshot_no_match: 'В локальной выгрузке совпадений нет',
  catalog_declaration: 'Заявлено в каталоге', unestablished: 'Не установлено',
  allowed: 'Разрешено', prohibited: 'Не разрешено', required: 'Требуется',
  yes: 'Да', no: 'Нет', conditional: 'При соблюдении условий',
  listed: 'В списке архива', extracted: 'Извлечено',
  nested_zip: 'Вложенный ZIP', unsupported: 'Формат не поддерживается',
}

export function statusLabel(value?: string): string {
  return value ? statusLabels[value] ?? value : 'Нет данных'
}

export function riskLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    low: 'Низкий риск', medium: 'Средний риск', high: 'Высокий риск',
    insufficient_data: 'Недостаточно данных', manual_review: 'Ручная проверка',
  }
  return value ? labels[value] ?? statusLabel(value) : 'Нет оценки'
}
