export class ApiError extends Error {
  constructor(public status: number, public detail: string) { super(detail); }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    let message = res.statusText;
    if (typeof detail === 'string') {
      message = detail;
    } else if (Array.isArray(detail) && detail.length) {
      // FastAPI 422: detail is an array of {loc, msg, type}
      message = detail
        .map((d: { loc?: unknown[]; msg?: string }) => {
          const field = Array.isArray(d.loc) ? d.loc.slice(1).join('.') : '';
          return field ? `${field}: ${d.msg}` : d.msg || '';
        })
        .filter(Boolean)
        .join('; ');
    }
    throw new ApiError(res.status, message);
  }
  return res.json();
}
