import { Activity, CircleAlert } from 'lucide-react'
import { riskLabel } from './api'

export function Notice({ children, error = false }: { children: React.ReactNode, error?: boolean }) {
  return <div className={error ? 'notice error' : 'notice'} role={error ? 'alert' : 'status'}>
    {error ? <CircleAlert size={18} /> : <Activity size={18} />}
    <div>{children}</div>
  </div>
}

export function PageTitle({ title, description, actions }: { title: string, description?: string, actions?: React.ReactNode }) {
  return <div className="page-heading">
    <div><h1>{title}</h1>{description && <p>{description}</p>}</div>
    {actions && <div className="button-group">{actions}</div>}
  </div>
}

export function Risk({ value }: { value?: string | null }) {
  return <span className={`risk risk-${value ?? 'none'}`}><span />{riskLabel(value)}</span>
}
