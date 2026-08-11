import { apiRequest } from '../../api-client.js'

/** Create the cohesive API facade used by the LLM adapter feature. */
export function createLlmAdapterApi() {
  return {
    status: () => apiRequest('/api/llm-adapter/status'),
    activate: (payload) => apiRequest('/api/llm-adapter/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
    activateSaved: (provider) => apiRequest(`/api/llm-adapter/activate-saved/${encodeURIComponent(provider)}`, {
      method: 'POST',
    }),
    clear: () => apiRequest('/api/llm-adapter/active', { method: 'DELETE' }),
    deleteCredential: (provider) => apiRequest(`/api/llm-adapter/credentials/${encodeURIComponent(provider)}`, {
      method: 'DELETE',
    }),
  }
}
