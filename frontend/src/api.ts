import { demoApi } from './demo-api';

export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === 'true';
export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/v1';

let csrfToken = '';
const REQUEST_TIMEOUT_MS = 120000;

export function setCsrfToken(value: string) {
  csrfToken = value;
}

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (DEMO_MODE) return demoApi<T>(path, init);
  const method = (init.method ?? 'GET').toUpperCase();
  const write = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  if (write && !csrfToken && path !== '/auth/login') {
    const tokenResponse = await fetchWithTimeout(`${API_BASE}/auth/csrf`, { credentials: 'include' });
    if (tokenResponse.ok) {
      const tokenData = await tokenResponse.json();
      csrfToken = tokenData.csrf_token;
    }
  }
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (write && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetchWithTimeout(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = data.error ?? data.detail ?? {};
    const validationMessage = Array.isArray(data.detail)
      ? data.detail.map((item: { loc?: Array<string | number>; msg?: string }) => {
          const field = (item.loc ?? []).filter(part => part !== 'body').join('.');
          return `${field ? `${field}：` : ''}${item.msg ?? '输入不符合要求'}`;
        }).join('；')
      : '';
    throw new ApiError(
      response.status,
      error.code ?? 'request_failed',
      error.message ?? (validationMessage || `请求失败 (${response.status})`),
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
