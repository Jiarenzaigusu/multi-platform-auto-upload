const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

let apiBase = ''
let unauthorizedHandler = null

/** Configure the shared browser client once when the Vue application starts. */
export function configureApiClient({ baseUrl = '', onUnauthorized = null } = {}) {
  apiBase = baseUrl.replace(/\/$/, '')
  unauthorizedHandler = onUnauthorized
}

/** Return a named Cookie value without exposing the HttpOnly session Cookie. */
function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`
  const entry = document.cookie
    .split(';')
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix))
  return entry ? decodeURIComponent(entry.slice(prefix.length)) : ''
}

/** Convert FastAPI validation and application errors into one readable message. */
function errorMessage(body) {
  if (typeof body.detail === 'string') return body.detail
  if (Array.isArray(body.detail)) {
    return body.detail
      .map((item) => (item.msg || '输入内容不符合要求').replace(/^Value error,\s*/i, ''))
      .join('；')
  }
  return '请求失败，请稍后重试'
}

/** Send an authenticated API request and attach CSRF proof to every mutation. */
export async function apiFetch(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const headers = new Headers(options.headers || {})
  if (!SAFE_METHODS.has(method)) {
    const csrfToken = cookieValue('mpau_csrf_v2')
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  }

  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    method,
    headers,
    credentials: 'include',
  })
  return response
}

export async function apiRequest(path, options = {}) {
  const response = await apiFetch(path, options)
  const body = response.status === 204
    ? null
    : await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401 && unauthorizedHandler && !options.skipUnauthorizedHandler) {
      unauthorizedHandler()
    }
    const error = new Error(errorMessage(body || {}))
    error.status = response.status
    error.details = body || {}
    throw error
  }
  return body
}

/** Build an API URL for browser-native downloads such as Excel templates. */
export function apiUrl(path) {
  return `${apiBase}${path}`
}
