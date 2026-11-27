import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowUpRight, KeyRound, LoaderCircle, RadioTower, ShieldCheck } from 'lucide-react'
import { describeError, json, request, safeHref } from './api'
import type { Settings } from './api'
import { Notice, PageTitle } from './ui'

export function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    request<Settings>('/settings', { signal: controller.signal }).then(setSettings).catch(reason => {
      if (!controller.signal.aborted) setError(describeError(reason))
    })
    return () => controller.abort()
  }, [])

  async function save(event?: FormEvent) {
    event?.preventDefault()
    const removing = event === undefined
    if (busy || (!removing && !apiKey.trim())) return
    setBusy(removing ? 'remove' : 'save')
    setMessage('')
    setError('')
    try {
      await request('/settings/deepseek', json('PUT', { api_key: removing ? '' : apiKey.trim() }))
      setApiKey('')
      const next = await request<Settings>('/settings')
      setSettings(next)
      setMessage(removing ? 'Сохранённый ключ удалён.' : 'Ключ сохранён. Проверьте подключение.')
    } catch (reason) {
      setError(describeError(reason))
    } finally {
      setBusy('')
    }
  }

  async function check() {
    if (busy) return
    setBusy('test')
    setMessage('')
    setError('')
    try {
      const result = await request<{ status: string, message: string }>('/settings/deepseek/test', { method: 'POST' })
      if (['ok', 'success', 'connected'].includes(result.status)) setMessage(result.message)
      else setError(result.message)
    } catch (reason) {
      setError(describeError(reason))
    } finally {
      setBusy('')
    }
  }

  return <>
    <PageTitle title="Настройки" description="Подключение DeepSeek и источники правовых сведений." />
    <div className="settings-grid">
      <section className="panel form-panel">
        <div className="setting-heading"><span className="file-icon"><KeyRound size={19} /></span>
          <div><h2>DeepSeek API</h2><p>{settings ? settings.deepseek_configured ? 'Ключ настроен' : 'Ключ отсутствует' : 'Проверяем настройки…'}{settings?.deepseek_model && ` · ${settings.deepseek_model}`}</p></div>
        </div>
        <p className="setting-copy">Создайте ключ в кабинете DeepSeek, сохраните его здесь и проверьте подключение. Локальная проверка доступна без ключа.</p>
        <a className="text-link" href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">Получить ключ <ArrowUpRight size={16} /></a>
        <form onSubmit={save} className="key-form">
          <label htmlFor="api-key">API-ключ DeepSeek</label>
          <input id="api-key" type="password" value={apiKey} onChange={event => setApiKey(event.target.value)}
            disabled={Boolean(busy) || !settings} placeholder="Введите новый ключ" autoComplete="off" spellCheck={false} maxLength={256} />
          <div className="button-group">
            <button className="button button-primary" type="submit" disabled={!apiKey.trim() || Boolean(busy)}>{busy === 'save' && <LoaderCircle className="spinning" size={16} />}Сохранить</button>
            <button className="button button-secondary" type="button" onClick={check} disabled={!settings?.deepseek_configured || Boolean(busy)}>
              {busy === 'test' ? <LoaderCircle className="spinning" size={16} /> : <RadioTower size={16} />} Проверить подключение
            </button>
            {settings?.deepseek_configured && <button className="button button-quiet" type="button" onClick={() => void save()} disabled={Boolean(busy)}>Удалить ключ</button>}
          </div>
        </form>
        {error && <Notice error>{error}</Notice>}
        {message && <Notice>{message}</Notice>}
      </section>

      <section className="panel form-panel">
        <div className="setting-heading"><span className="file-icon"><ShieldCheck size={19} /></span><div><h2>Официальные реестры</h2><p>Российский правовой контекст</p></div></div>
        <p className="setting-copy">В отчёте указывается охват правовой проверки. Актуальный статус сверяйте с официальным реестром.</p>
        {settings?.legal_registries?.length ? <ul className="registry-directory">
          {settings.legal_registries.map(registry => <li key={registry.url}>
            <a className="text-link" href={safeHref(registry.url)} target="_blank" rel="noreferrer">{registry.title}<ArrowUpRight size={15} /></a>
          </li>)}
        </ul> : settings && <p className="muted">Список источников пока пуст.</p>}
      </section>
    </div>
  </>
}
