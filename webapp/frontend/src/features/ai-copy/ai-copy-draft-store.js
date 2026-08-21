// Local-only persistence for the AI copy studio. API keys are intentionally excluded.
function draftKey(userId = '') {
  return `mpau_ai_copy_draft_v1:${userId || 'anonymous'}`
}
const DATABASE_NAME = 'mpau_ai_copy_drafts'
const DATABASE_VERSION = 1
const WORKBOOK_STORE = 'selling_point_workbook'
function workbookKey(userId = '') {
  return `current:${userId || 'anonymous'}`
}

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(WORKBOOK_STORE)) db.createObjectStore(WORKBOOK_STORE)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('无法打开本机草稿库'))
  })
}

export function readAiCopyDraft(userId = '') {
  try {
    const raw = localStorage.getItem(draftKey(userId))
    const draft = raw ? JSON.parse(raw) : null
    return draft && typeof draft === 'object' ? draft : null
  } catch {
    return null
  }
}

export function saveAiCopyDraft(draft, userId = '') {
  try {
    localStorage.setItem(draftKey(userId), JSON.stringify(draft))
  } catch (error) {
    console.warn('保存 AI 文案草稿失败：', error)
  }
}

export function clearAiCopyDraft(userId = '') {
  try { localStorage.removeItem(draftKey(userId)) } catch { /* Browser storage is unavailable. */ }
}

export async function saveSellingPointWorkbook(file, userId = '') {
  const db = await openDatabase()
  try {
    await new Promise((resolve, reject) => {
      const transaction = db.transaction(WORKBOOK_STORE, 'readwrite')
      transaction.objectStore(WORKBOOK_STORE).put({
        blob: file,
        name: file.name,
        type: file.type || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        lastModified: file.lastModified || Date.now(),
      }, workbookKey(userId))
      transaction.oncomplete = resolve
      transaction.onerror = () => reject(transaction.error)
    })
  } finally {
    db.close()
  }
}

export async function loadSellingPointWorkbook(userId = '') {
  const db = await openDatabase()
  try {
    const stored = await new Promise((resolve, reject) => {
      const transaction = db.transaction(WORKBOOK_STORE, 'readonly')
      const request = transaction.objectStore(WORKBOOK_STORE).get(workbookKey(userId))
      request.onsuccess = () => resolve(request.result || null)
      request.onerror = () => reject(request.error)
    })
    if (!stored?.blob) return null
    return new File([stored.blob], stored.name || 'selling-points.xlsx', {
      type: stored.type || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      lastModified: stored.lastModified || Date.now(),
    })
  } finally {
    db.close()
  }
}

export async function clearSellingPointWorkbook(userId = '') {
  const db = await openDatabase()
  try {
    await new Promise((resolve, reject) => {
      const transaction = db.transaction(WORKBOOK_STORE, 'readwrite')
      transaction.objectStore(WORKBOOK_STORE).delete(workbookKey(userId))
      transaction.oncomplete = resolve
      transaction.onerror = () => reject(transaction.error)
    })
  } finally {
    db.close()
  }
}
