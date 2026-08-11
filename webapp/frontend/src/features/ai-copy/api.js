import { apiRequest } from '../../api-client.js'

/** Create the cohesive API facade used by the AI copy feature. */
export function createAiCopyApi() {
  return {
    options: () => apiRequest('/api/ai-copy/options'),
    inspectProduct: (payload) => apiRequest('/api/ai-copy/product-reference', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
    generate: (payload) => apiRequest('/api/ai-copy/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  }
}
