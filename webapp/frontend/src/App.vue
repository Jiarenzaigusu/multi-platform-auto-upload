<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { apiRequest as request, apiUrl, configureApiClient } from './api-client.js'
import AuthGate from './components/AuthGate.vue'
import AiCopyView from './features/ai-copy/AiCopyView.vue'
import LlmAdapterView from './features/llm-adapter/LlmAdapterView.vue'
import UserManagementView from './features/users/UserManagementView.vue'

const apiBase = import.meta.env.VITE_API_BASE_URL || ''
const currentUser = ref(null)
configureApiClient({ baseUrl: apiBase, onUnauthorized: endAuthenticatedSession })

// === 发布草稿持久化（localStorage 文本 + IndexedDB 视频） ===
const DRAFT_DB_NAME = 'mpau_publish_drafts'
const DRAFT_DB_VERSION = 1
const DRAFT_VIDEO_STORE = 'videos'
const DRAFT_VIDEO_MAX_BYTES = 100 * 1024 * 1024
const creatorDeclarationOptions = [
  '内容无需标注',
  '内容含营销广告',
  '含AI生成内容',
  '含虚构演绎内容',
  '内容为转载',
  '个人观点，仅供参考',
]
const draftRestoredVideoName = ref('')
const draftRestoredAt = ref('')
const isRestoringDraft = ref(true)
let persistTimer = null

/** Namespace browser drafts by immutable user ID to prevent cross-login leakage. */
function formDraftStorageKey() {
  return currentUser.value ? `mpau_publish_form_draft_v2:${currentUser.value.id}` : ''
}

/** Namespace the IndexedDB video record by immutable user ID. */
function draftVideoKey() {
  return currentUser.value ? `last_publish_video:${currentUser.value.id}` : ''
}

const PLATFORM_DRAFT_KEYS = [
  'account',
  'title',
  'description',
  'tags',
  'goodsId',
  'activityTopic',
  'musicName',
  'creatorDeclaration',
  'schedule',
  'original',
]

function createEmptyPlatformDraft() {
  return {
    account: '',
    title: '',
    description: '',
    tags: '',
    goodsId: '',
    activityTopic: '',
    musicName: '',
    creatorDeclaration: '',
    schedule: '',
    original: false,
  }
}

function normalizePlatformDraft(saved) {
  const draft = createEmptyPlatformDraft()
  if (!saved || typeof saved !== 'object') return draft
  for (const key of PLATFORM_DRAFT_KEYS) {
    if (key === 'original') {
      if (typeof saved[key] === 'boolean') draft[key] = saved[key]
    } else if (typeof saved[key] === 'string') {
      draft[key] = saved[key]
    }
  }
  if (!creatorDeclarationOptions.includes(draft.creatorDeclaration)) {
    draft.creatorDeclaration = ''
  }
  return draft
}

function openDraftDatabase() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new Error('浏览器不支持 IndexedDB'))
      return
    }
    const request = indexedDB.open(DRAFT_DB_NAME, DRAFT_DB_VERSION)
    request.onupgradeneeded = (event) => {
      const db = event.target.result
      if (!db.objectStoreNames.contains(DRAFT_VIDEO_STORE)) {
        db.createObjectStore(DRAFT_VIDEO_STORE)
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('无法打开草稿数据库'))
  })
}

async function persistDraftVideo(file) {
  if (!draftVideoKey()) return
  if (!file) return deleteDraftVideo()
  let db
  try {
    db = await openDraftDatabase()
    await new Promise((resolve, reject) => {
      const tx = db.transaction(DRAFT_VIDEO_STORE, 'readwrite')
      tx.objectStore(DRAFT_VIDEO_STORE).put({
        blob: file,
        name: file.name,
        size: file.size,
        type: file.type || 'video/mp4',
        lastModified: file.lastModified || Date.now(),
        savedAt: new Date().toISOString(),
      }, draftVideoKey())
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.warn('保存视频草稿失败：', error)
  } finally {
    if (db) db.close()
  }
}

async function restoreDraftVideo() {
  if (!draftVideoKey()) return null
  let db
  try {
    db = await openDraftDatabase()
    const result = await new Promise((resolve, reject) => {
      const tx = db.transaction(DRAFT_VIDEO_STORE, 'readonly')
      const req = tx.objectStore(DRAFT_VIDEO_STORE).get(draftVideoKey())
      req.onsuccess = () => resolve(req.result || null)
      req.onerror = () => reject(req.error)
    })
    if (result && result.blob instanceof Blob) {
      return {
        file: new File([result.blob], result.name || 'video.mp4', {
          type: result.type || 'video/mp4',
          lastModified: result.lastModified || Date.now(),
        }),
        savedAt: result.savedAt || '',
      }
    }
  } catch (error) {
    console.warn('读取视频草稿失败：', error)
  } finally {
    if (db) db.close()
  }
  return null
}

async function deleteDraftVideo() {
  if (!draftVideoKey()) return
  let db
  try {
    db = await openDraftDatabase()
    await new Promise((resolve, reject) => {
      const tx = db.transaction(DRAFT_VIDEO_STORE, 'readwrite')
      tx.objectStore(DRAFT_VIDEO_STORE).delete(draftVideoKey())
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.warn('删除视频草稿失败：', error)
  } finally {
    if (db) db.close()
  }
}

function readSavedFormDraft() {
  if (!formDraftStorageKey()) return null
  try {
    const raw = localStorage.getItem(formDraftStorageKey())
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch (error) {
    console.warn('读取发布草稿失败：', error)
    return null
  }
}

function persistFormDraft() {
  if (isRestoringDraft.value || !formDraftStorageKey()) return
  snapshotPlatformDraft(form.platform)
  if (persistTimer) clearTimeout(persistTimer)
  persistTimer = setTimeout(() => {
    try {
      const payload = {
        version: 2,
        platform: form.platform,
        platformDrafts: {
          tmall: { ...platformDrafts.tmall },
          jd: { ...platformDrafts.jd },
        },
        dryRun: form.dryRun,
        headed: form.headed,
        savedAt: new Date().toISOString(),
      }
      localStorage.setItem(formDraftStorageKey(), JSON.stringify(payload))
    } catch (error) {
      console.warn('保存发布草稿失败：', error)
    }
  }, 200)
}

function applySavedFormDraft(saved) {
  if (
    !saved
    || saved.version !== 2
    || !saved.platformDrafts
    || typeof saved.platformDrafts !== 'object'
  ) return
  const savedPlatform = saved.platform === 'tmall' || saved.platform === 'jd'
    ? saved.platform
    : form.platform
  for (const platform of ['tmall', 'jd']) {
    Object.assign(platformDrafts[platform], normalizePlatformDraft(saved.platformDrafts[platform]))
  }
  form.platform = savedPlatform
  applyPlatformDraft(form.platform)
  if (typeof saved.dryRun === 'boolean') form.dryRun = saved.dryRun
  if (typeof saved.headed === 'boolean') form.headed = saved.headed
}

async function restorePublishDraft() {
  isRestoringDraft.value = true
  draftRestoredVideoName.value = ''
  draftRestoredAt.value = ''
  try {
    applySavedFormDraft(readSavedFormDraft())
    const videoDraft = await restoreDraftVideo()
    if (videoDraft?.file) {
      form.video = videoDraft.file
      draftRestoredVideoName.value = videoDraft.file.name
      draftRestoredAt.value = videoDraft.savedAt || new Date().toISOString()
      showNotice(`已自动恢复上次发布配置（含视频：${videoDraft.file.name}）`, 'info')
    } else {
      const textDraft = readSavedFormDraft()
      if (textDraft && (textDraft.title || textDraft.account)) {
        showNotice('已自动恢复上次发布配置', 'info')
      }
    }
  } finally {
    await nextTick()
    isRestoringDraft.value = false
  }
}

async function clearPublishDraft() {
  if (!window.confirm('确定清空发布页面的所有配置信息和已选素材吗？此操作也会删除当前已选的视频文件。')) {
    return
  }
  clearPublishContent()
  try {
    localStorage.removeItem(formDraftStorageKey())
  } catch (error) { /* ignore */ }
  await deleteDraftVideo()
  form.dryRun = true
  form.headed = true
  form.original = false
  draftRestoredVideoName.value = ''
  draftRestoredAt.value = ''
  showNotice('已一键清空发布配置和素材', 'success')
}

const jobs = ref([])
const jobSummary = ref({ total: 0, statuses: {} })
const jobsOffset = ref(0)
const jobsPageSize = 500
const accounts = ref([])
const agentStatus = ref({ execution_mode: 'local_agent', online: false, agents: [] })
const pairingCode = ref('')
const pairingExpiresAt = ref('')
const pairingBusy = ref(false)
const pairingError = ref('')
const activeView = ref('publish')
const selectedJob = ref(null)
const jobLogs = ref([])
const videoInput = ref(null)
const scheduleInput = ref(null)
const batchWorkbookInput = ref(null)
const batchMediaInput = ref(null)
const submitting = ref(false)
const publishError = ref('')
const batchSubmitting = ref(false)
const batchSubmitError = ref('')
const notice = ref('')
const noticeType = ref('info')
const batchErrors = ref([])
const batchResult = ref(null)
const mediaFiles = ref([])
const selectedMediaUploads = ref([])
const mediaUploading = ref(false)
const mediaError = ref('')
let refreshTimer
let noticeTimer
let dashboardRefreshPromise = null

const NOTICE_DISMISS_MS = 3000

const form = reactive({
  platform: 'tmall',
  account: '',
  video: null,
  title: '',
  description: '',
  tags: '',
  goodsId: '',
  activityTopic: '',
  musicName: '',
  creatorDeclaration: '',
  schedule: '',
  original: false,
  dryRun: true,
  headed: true,
})

const platformDrafts = reactive({
  tmall: createEmptyPlatformDraft(),
  jd: createEmptyPlatformDraft(),
})

function snapshotPlatformDraft(platform = form.platform) {
  const draft = platformDrafts[platform]
  if (!draft) return
  for (const key of PLATFORM_DRAFT_KEYS) {
    draft[key] = form[key]
  }
}

function applyPlatformDraft(platform) {
  const draft = platformDrafts[platform] || createEmptyPlatformDraft()
  for (const key of PLATFORM_DRAFT_KEYS) {
    form[key] = draft[key]
  }
}

const batchForm = reactive({
  platform: 'tmall',
  account: '',
  workbook: null,
  dryRun: true,
  headed: true,
})

const isTmall = computed(() => form.platform === 'tmall')
const isTmallBatch = computed(() => batchForm.platform === 'tmall')
const isAdmin = computed(() => currentUser.value?.role === 'admin')
const localAgentOnline = computed(() => Boolean(agentStatus.value.online))
const agentInstallerAvailable = computed(() => Boolean(agentStatus.value.installer?.available))
const pairingExpiryLabel = computed(() => {
  if (!pairingExpiresAt.value) return ''
  const value = new Date(pairingExpiresAt.value)
  return Number.isNaN(value.getTime()) ? '' : value.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
})
const platformLabel = (platform) => (platform === 'tmall' ? '天猫光合' : '京东京麦')
const batchPlatformLabel = computed(() => platformLabel(batchForm.platform))
const jobLabel = (kind) => ({ publish: '发布', login: '登录', check: '校验', delete_account: '删除本地账号' }[kind] || kind)
const statusLabel = (status) => ({ queued: '排队中', running: '执行中', cancelling: '正在中断', cancelled: '已中断', succeeded: '已完成', failed: '失败', uncertain: '结果待核对' }[status] || status)
const statusClass = (status) => `status status-${status}`
const terminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'uncertain'])
const canDeleteJob = (job) => terminalStatuses.has(job.status)
const canCancelJob = (job) => ['queued', 'running'].includes(job.status)
const visibleAccounts = computed(() => accounts.value.filter((item) => item.platform === form.platform))
const batchAccounts = computed(() => accounts.value.filter((item) => item.platform === batchForm.platform))
const viewTitle = computed(() => ({
  publish: '新建发布任务',
  'ai-copy': 'AI 文案工坊',
  'llm-adapter': 'LLM 适配器',
  users: '用户与权限',
  batch: '批量发布任务',
  jobs: '任务追踪中心',
}[activeView.value] || '商家发布台'))
const viewEyebrow = computed(() => ({
  'ai-copy': 'AI COPY STUDIO',
  'llm-adapter': 'LLM ROUTING DESK',
  users: 'ACCESS DIRECTORY',
}[activeView.value] || 'COMMERCE PUBLISHING'))
const uploaderTags = computed(() => form.tags
  .split(',')
  .map((tag) => tag.trim().replace(/^#+/, ''))
  .filter(Boolean))
const enteredGoodsIds = computed(() => form.goodsId
  .split(/[,，\s]+/)
  .map((goodsId) => goodsId.trim())
  .filter(Boolean))
const uniqueGoodsIds = computed(() => [...new Set(enteredGoodsIds.value)])
const tagTextLength = computed(() => uploaderTags.value.reduce((total, tag) => total + ` #${tag}`.length, 0))
const contentTextLength = computed(() => form.description.trim().length + tagTextLength.value)
const descriptionLimit = computed(() => Math.max(0, 1000 - tagTextLength.value))
const formatLocalDateTime = (date) => {
  const offsetMilliseconds = date.getTimezoneOffset() * 60 * 1000
  return new Date(date.getTime() - offsetMilliseconds).toISOString().slice(0, 16)
}
const minimumScheduleDate = () => new Date(Date.now() + 2 * 60 * 60 * 1000)
const scheduleMinimum = ref(formatLocalDateTime(minimumScheduleDate()))
const scheduleDisplay = computed(() => {
  if (!form.schedule) return '年/月/日 --:--'
  const [date, time] = form.schedule.split('T')
  return `${date.replaceAll('-', '/')} ${time}`
})
const counts = computed(() => ({
  total: jobSummary.value.total,
  running: jobSummary.value.statuses.running || 0,
  failed: (jobSummary.value.statuses.failed || 0) + (jobSummary.value.statuses.uncertain || 0),
  done: jobSummary.value.statuses.succeeded || 0,
}))
const jobsPageStart = computed(() => (jobSummary.value.total ? jobsOffset.value + 1 : 0))
const jobsPageEnd = computed(() => Math.min(
  jobsOffset.value + jobs.value.length,
  jobSummary.value.total,
))
const hasPreviousJobs = computed(() => jobsOffset.value > 0)
const hasMoreJobs = computed(() => jobsOffset.value + jobs.value.length < jobSummary.value.total)

watch(() => form.platform, (platform, previousPlatform) => {
  if (isRestoringDraft.value) return
  if (previousPlatform && previousPlatform !== platform) {
    snapshotPlatformDraft(previousPlatform)
    applyPlatformDraft(platform)
  }
  publishError.value = ''
  persistFormDraft()
})

watch(() => batchForm.platform, () => {
  batchSubmitError.value = ''
  clearBatchWorkbook()
})

watch(activeView, (view) => {
  if (view === 'batch') refreshMediaFiles()
})

watch(form, () => {
  if (isRestoringDraft.value) return
  persistFormDraft()
}, { deep: true })

function showNotice(message, type = 'info') {
  if (noticeTimer) {
    clearTimeout(noticeTimer)
    noticeTimer = null
  }
  notice.value = message
  noticeType.value = type
  noticeTimer = setTimeout(() => {
    notice.value = ''
    noticeTimer = null
  }, NOTICE_DISMISS_MS)
}

async function generatePairingCode() {
  pairingBusy.value = true
  pairingError.value = ''
  try {
    const result = await request('/api/agent/pairing-code', { method: 'POST' })
    pairingCode.value = result.pairing_code
    pairingExpiresAt.value = result.expires_at
  } catch (error) {
    pairingError.value = error.message
  } finally {
    pairingBusy.value = false
  }
}

async function copyPairingCode() {
  if (!pairingCode.value) return
  try {
    await navigator.clipboard.writeText(pairingCode.value)
    showNotice('配对码已复制', 'success')
  } catch (error) {
    showNotice('无法自动复制，请手动输入配对码', 'error')
  }
}

function importAiCopyToWorkbench(draft) {
  if (!draft || typeof draft.title !== 'string' || typeof draft.body !== 'string') return

  form.title = draft.title
  if (isTmall.value) {
    form.description = draft.body
    showNotice('生成的标题和文案已导入发布工作台，原内容已覆盖', 'success')
  } else {
    showNotice('生成的标题已导入；京东不支持独立文案，正文未导入', 'info')
  }
  activeView.value = 'publish'
}

async function refreshDashboard() {
  const userId = currentUser.value?.id
  if (!userId) return
  if (dashboardRefreshPromise) return dashboardRefreshPromise
  dashboardRefreshPromise = (async () => {
    try {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const [jobsResult, accountsResult, agentResult] = await Promise.all([
          request(`/api/jobs?limit=${jobsPageSize}&offset=${jobsOffset.value}`),
          request('/api/accounts'),
          request('/api/agent/status'),
        ])
        if (currentUser.value?.id !== userId) return
        if (jobsResult.total > 0 && jobsOffset.value >= jobsResult.total) {
          jobsOffset.value = Math.floor((jobsResult.total - 1) / jobsPageSize) * jobsPageSize
          continue
        }
        jobs.value = jobsResult.jobs
        jobSummary.value = {
          total: jobsResult.total ?? jobsResult.jobs.length,
          statuses: jobsResult.status_counts || {},
        }
        accounts.value = accountsResult.accounts
        agentStatus.value = agentResult
        if (agentResult.online) {
          pairingCode.value = ''
          pairingExpiresAt.value = ''
          pairingError.value = ''
        }
        if (selectedJob.value) await loadJob(selectedJob.value.id, false)
        break
      }
    } catch (error) {
      if (!notice.value) showNotice(`无法连接发布服务：${error.message}`, 'error')
    }
  })()
  try {
    return await dashboardRefreshPromise
  } finally {
    dashboardRefreshPromise = null
  }
}

async function changeJobsPage(direction) {
  jobsOffset.value = Math.max(0, jobsOffset.value + direction * jobsPageSize)
  await refreshDashboard()
}

async function loadJob(jobId, openPanel = true) {
  const result = await request(`/api/jobs/${jobId}`)
  selectedJob.value = result.job
  jobLogs.value = result.logs
  if (openPanel) activeView.value = 'jobs'
}

async function deleteJob(job) {
  if (!canDeleteJob(job)) return
  if (!window.confirm(`确定删除“${jobLabel(job.kind)} · ${job.account}”任务记录及其独立日志吗？此操作不会删除 Cookie、截图或平台总日志。`)) return

  try {
    await request(`/api/jobs/${job.id}`, { method: 'DELETE' })
    if (selectedJob.value?.id === job.id) {
      selectedJob.value = null
      jobLogs.value = []
    }
    showNotice('任务记录已删除', 'success')
    await refreshDashboard()
  } catch (error) {
    showNotice(error.message, 'error')
  }
}

async function cancelJobAndDeleteAccount(job) {
  if (!canCancelJob(job)) return
  if (!localAgentOnline.value) {
    showNotice('本地执行代理未在线，无法安全中断本机浏览器或删除 Cookie', 'error')
    return
  }
  if (!window.confirm(`确定中断“${jobLabel(job.kind)} · ${job.account}”任务，并在浏览器退出后删除该店铺的 Cookie 和账号建议吗？历史任务、截图和平台日志会保留。`)) return

  try {
    const result = await request(`/api/jobs/${job.id}/cancel-and-delete-account`, { method: 'POST' })
    if (selectedJob.value?.id === job.id) selectedJob.value = result.job
    showNotice(result.account_deletion === 'completed' ? '任务已中断，Cookie 和店铺账号建议已删除' : '正在中断浏览器任务；停止后将自动删除 Cookie 和店铺账号建议', 'success')
    await refreshDashboard()
  } catch (error) {
    showNotice(error.message, 'error')
  }
}

async function deleteAccount(platform = form.platform, account = form.account) {
  if (!account.trim()) {
    showNotice('请先填写店铺账号标识', 'error')
    return
  }
  if (!localAgentOnline.value) {
    showNotice('请先安装并打开本地执行助手，再删除本地 Cookie', 'error')
    return
  }
  const label = platformLabel(platform)
  if (!window.confirm(`确定删除“${label} · ${account}”的 Cookie 和账号建议吗？任务记录、截图和平台日志不会删除。`)) return

  try {
    const result = await request(`/api/accounts/${platform}/${encodeURIComponent(account)}`, { method: 'DELETE' })
    if (form.platform === platform && form.account === account) form.account = ''
    if (batchForm.platform === platform && batchForm.account === account) batchForm.account = ''
    showNotice(result.deletion_pending ? '删除任务已发送到当前电脑，本地代理处理后会移除 Cookie' : result.cookie_deleted ? 'Cookie 和店铺账号建议已删除' : '店铺账号建议已删除；未发现本地 Cookie 文件', 'success')
    await refreshDashboard()
  } catch (error) {
    showNotice(error.message, 'error')
  }
}

async function onFileChange(event) {
  const file = event.target.files?.[0] || null
  form.video = file
  publishError.value = ''
  if (file) {
    if (file.size <= DRAFT_VIDEO_MAX_BYTES) {
      await persistDraftVideo(file)
    } else {
      await deleteDraftVideo()
      showNotice('视频超过 100 MiB，仅保留文字配置，不复制到浏览器草稿库', 'info')
    }
  } else {
    await deleteDraftVideo()
  }
  draftRestoredVideoName.value = ''
}

function clearVideo() {
  form.video = null
  if (videoInput.value) videoInput.value.value = ''
  deleteDraftVideo()
  draftRestoredVideoName.value = ''
}

function clearPublishContent() {
  clearVideo()
  for (const platform of ['tmall', 'jd']) {
    Object.assign(platformDrafts[platform], createEmptyPlatformDraft())
  }
  form.title = ''
  form.description = ''
  form.tags = ''
  form.goodsId = ''
  form.activityTopic = ''
  form.musicName = ''
  form.creatorDeclaration = ''
  form.schedule = ''
  form.original = false
}

function onBatchWorkbookChange(event) {
  batchForm.workbook = event.target.files?.[0] || null
  batchSubmitError.value = ''
  batchErrors.value = []
  batchResult.value = null
}

function clearBatchWorkbook() {
  batchForm.workbook = null
  batchSubmitError.value = ''
  batchErrors.value = []
  batchResult.value = null
  if (batchWorkbookInput.value) batchWorkbookInput.value.value = ''
}

/** Refresh the current user's server-side batch video library. */
async function refreshMediaFiles() {
  mediaError.value = ''
  try {
    const result = await request('/api/media')
    mediaFiles.value = result.files || []
  } catch (requestError) {
    mediaError.value = requestError.message
  }
}

/** Keep the browser selection in memory until the user confirms the upload. */
function onBatchMediaChange(event) {
  selectedMediaUploads.value = Array.from(event.target.files || [])
  mediaError.value = ''
}

/** Upload selected videos into only the authenticated user's media directory. */
async function uploadBatchMedia() {
  if (!selectedMediaUploads.value.length) {
    mediaError.value = '请先选择一个或多个批量发布视频。'
    return
  }
  mediaUploading.value = true
  mediaError.value = ''
  const data = new FormData()
  for (const file of selectedMediaUploads.value) data.append('files', file)
  try {
    const result = await request('/api/media', { method: 'POST', body: data })
    mediaFiles.value = result.files || []
    selectedMediaUploads.value = []
    if (batchMediaInput.value) batchMediaInput.value.value = ''
    showNotice('批量视频已上传到你的独立素材目录', 'success')
  } catch (requestError) {
    mediaError.value = requestError.message
  } finally {
    mediaUploading.value = false
  }
}

/** Remove an unused media file after explicit user confirmation. */
async function deleteMediaFile(file) {
  if (!window.confirm(`确定删除批量素材“${file.name}”吗？`)) return
  mediaError.value = ''
  try {
    await request(`/api/media/${encodeURIComponent(file.name)}`, { method: 'DELETE' })
    await refreshMediaFiles()
  } catch (requestError) {
    mediaError.value = requestError.message
  }
}

const formatFileSize = (bytes) => `${(bytes / 1024 / 1024).toFixed(1)} MB`

function openSchedulePicker() {
  const input = scheduleInput.value
  if (!input) return

  try {
    input.showPicker()
  } catch {
    input.focus()
  }
}

async function submitPublish() {
  publishError.value = ''
  if (!localAgentOnline.value) {
    publishError.value = '本地执行助手未在线。请先安装并打开助手，再创建发布任务。'
    return
  }
  if (!form.account.trim()) {
    publishError.value = '请先选择或填写店铺账号标识'
    return
  }
  if (!form.video) {
    publishError.value = '请先重新选择一个视频文件'
    return
  }
  if (!form.title.trim()) {
    publishError.value = '请先填写视频标题'
    return
  }
  if (!creatorDeclarationOptions.includes(form.creatorDeclaration)) {
    publishError.value = '请选择与实际内容相符的创作者声明'
    return
  }
  if (isTmall.value && uploaderTags.value.length > 4) {
    publishError.value = '天猫光合最多支持 4 个标签'
    return
  }
  if (isTmall.value && contentTextLength.value > 1000) {
    publishError.value = '天猫发布文案与标签合计最多 1000 个字符'
    return
  }
  if (enteredGoodsIds.value.some((goodsId) => !/^\d+$/.test(goodsId))) {
    publishError.value = '商品 ID 必须为纯数字，多个 ID 请使用逗号或换行分隔'
    return
  }
  if (isTmall.value && uniqueGoodsIds.value.length > 6) {
    publishError.value = '天猫一次最多关联 6 个商品 ID'
    return
  }
  if (!isTmall.value && uniqueGoodsIds.value.length > 1) {
    publishError.value = '京东一次只能关联 1 个商品 ID'
    return
  }
  submitting.value = true
  const data = new FormData()
  data.append('platform', form.platform)
  data.append('account', form.account)
  data.append('video', form.video)
  data.append('title', form.title)
  data.append('description', isTmall.value ? form.description : '')
  data.append('tags', isTmall.value ? form.tags : '')
  data.append('goods_id', form.goodsId)
  data.append('activity_topic', isTmall.value ? form.activityTopic : '')
  data.append('music_name', isTmall.value ? form.musicName : '')
  data.append('creator_declaration', form.creatorDeclaration)
  data.append('schedule', form.schedule.replace('T', ' '))
  data.append('original', String(isTmall.value ? false : form.original))
  data.append('dry_run', String(form.dryRun))
  data.append('headed', String(form.headed))

  try {
    const result = await request('/api/jobs/publish', { method: 'POST', body: data })
    showNotice(`${platformLabel(form.platform)}${form.dryRun ? '流程验证' : '发布'}任务已创建，配置已保留可继续修改后再次发布`, 'success')
    await refreshDashboard()
    await loadJob(result.job.id)
  } catch (error) {
    publishError.value = error.message
    showNotice(error.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function submitBatch() {
  batchSubmitError.value = ''
  if (!localAgentOnline.value) {
    batchSubmitError.value = '本地执行助手未在线。请先安装并打开助手。'
    return
  }
  if (!batchForm.account.trim()) {
    batchSubmitError.value = `请先选择或填写${batchPlatformLabel.value}店铺账号标识`
    return
  }
  if (!batchForm.workbook) {
    batchSubmitError.value = `请先重新选择${batchPlatformLabel.value}批量发布 Excel 文件`
    return
  }

  batchSubmitting.value = true
  batchErrors.value = []
  batchResult.value = null
  const data = new FormData()
  data.append('account', batchForm.account)
  data.append('workbook', batchForm.workbook)
  data.append('dry_run', String(batchForm.dryRun))
  data.append('headed', String(batchForm.headed))

  try {
    const result = await request(`/api/jobs/batch/${batchForm.platform}`, { method: 'POST', body: data })
    clearBatchWorkbook()
    batchResult.value = result
    showNotice(`已创建 ${result.created_count} 条${batchPlatformLabel.value}${batchForm.dryRun ? '流程验证' : '发布'}任务`, 'success')
    await refreshDashboard()
  } catch (error) {
    batchSubmitError.value = error.message
    batchErrors.value = error.details?.errors || []
    showNotice(error.message, 'error')
  } finally {
    batchSubmitting.value = false
  }
}

async function accountAction(action, platform = form.platform, account = form.account) {
  if (!account.trim()) {
    showNotice('请先填写店铺账号标识', 'error')
    return
  }
  if (!localAgentOnline.value) {
    showNotice('请先安装并打开本地执行助手；登录、Cookie 和 Edge 都保存在当前电脑', 'error')
    return
  }
  try {
    const query = action === 'login' ? '?headed=true' : ''
    const result = await request(`/api/accounts/${platform}/${encodeURIComponent(account)}/${action}${query}`, { method: 'POST' })
    showNotice(action === 'login' ? '登录任务已发送到当前电脑，请在本机 Microsoft Edge 完成登录' : '账号校验任务已发送到当前电脑', 'success')
    await refreshDashboard()
    await loadJob(result.job.id)
  } catch (error) {
    showNotice(error.message, 'error')
  }
}

/** Clear in-memory state when a session changes without deleting either user's drafts. */
function resetUserInterface() {
  if (persistTimer) window.clearTimeout(persistTimer)
  persistTimer = null
  isRestoringDraft.value = true
  Object.assign(form, {
    platform: 'tmall',
    account: '',
    video: null,
    title: '',
    description: '',
    tags: '',
    goodsId: '',
    activityTopic: '',
    musicName: '',
    creatorDeclaration: '',
    schedule: '',
    original: false,
    dryRun: true,
    headed: true,
  })
  for (const platform of ['tmall', 'jd']) {
    Object.assign(platformDrafts[platform], createEmptyPlatformDraft())
  }
  Object.assign(batchForm, {
    platform: 'tmall',
    account: '',
    workbook: null,
    dryRun: true,
    headed: true,
  })
  jobs.value = []
  accounts.value = []
  agentStatus.value = { execution_mode: 'local_agent', online: false, agents: [] }
  pairingCode.value = ''
  pairingExpiresAt.value = ''
  pairingError.value = ''
  pairingBusy.value = false
  jobSummary.value = { total: 0, statuses: {} }
  jobsOffset.value = 0
  selectedJob.value = null
  jobLogs.value = []
  batchErrors.value = []
  batchResult.value = null
  mediaFiles.value = []
  selectedMediaUploads.value = []
  mediaError.value = ''
  draftRestoredVideoName.value = ''
  draftRestoredAt.value = ''
  dashboardRefreshPromise = null
  isRestoringDraft.value = false
}

/** Stop polling and hide all prior-user state immediately after logout or HTTP 401. */
function endAuthenticatedSession() {
  window.clearInterval(refreshTimer)
  refreshTimer = null
  currentUser.value = null
  activeView.value = 'publish'
  resetUserInterface()
}

/** Initialize only the authenticated user's drafts, data, and refresh loop. */
async function beginAuthenticatedSession(user) {
  endAuthenticatedSession()
  currentUser.value = user
  activeView.value = 'publish'
  scheduleMinimum.value = formatLocalDateTime(minimumScheduleDate())
  await restorePublishDraft()
  await refreshDashboard()
  window.clearInterval(refreshTimer)
  refreshTimer = window.setInterval(refreshDashboard, 4000)
}

/** Revoke the server session; running background jobs intentionally continue. */
async function logout() {
  try {
    await request('/api/auth/logout', { method: 'POST' })
  } catch (requestError) {
    if (requestError.status !== 401) showNotice(requestError.message, 'error')
  } finally {
    endAuthenticatedSession()
  }
}

onMounted(async () => {
  scheduleMinimum.value = formatLocalDateTime(minimumScheduleDate())
})

onBeforeUnmount(() => {
  window.clearInterval(refreshTimer)
  if (noticeTimer) clearTimeout(noticeTimer)
})
</script>

<template>
  <AuthGate v-if="!currentUser" @authenticated="beginAuthenticatedSession" />
  <main v-else class="shell">
    <aside class="rail">
      <div class="brand">
        <span class="brand-mark">M</span>
        <div><strong>商家发布台</strong><small>Tmall · JD</small></div>
      </div>

      <nav>
        <button class="feature-nav-entry" :class="{ active: activeView === 'publish' }" @click="activeView = 'publish'">
          <span>发布工作台</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 15V4m0 0L8 8m4-4 4 4" /><path d="M5 14v5h14v-5" /></svg></span>
        </button>
        <button class="feature-nav-entry" :class="{ active: activeView === 'ai-copy' }" @click="activeView = 'ai-copy'">
          <span>AI 文案工坊</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3c.6 3.3 2.7 5.4 6 6-3.3.6-5.4 2.7-6 6-.6-3.3-2.7-5.4-6-6 3.3-.6 5.4-2.7 6-6Z" /><path d="M18.5 15.5c.2 1.4 1.1 2.3 2.5 2.5-1.4.2-2.3 1.1-2.5 2.5-.2-1.4-1.1-2.3-2.5-2.5 1.4-.2 2.3-1.1 2.5-2.5Z" /></svg></span>
        </button>
        <button class="feature-nav-entry" :class="{ active: activeView === 'batch' }" @click="activeView = 'batch'">
          <span>批量发布任务</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="7" y="4" width="12" height="14" rx="2" /><path d="M15 18v2H5a2 2 0 0 1-2-2V8h4M10 9h6m-6 4h6" /></svg></span>
        </button>
        <button class="feature-nav-entry" :class="{ active: activeView === 'llm-adapter' }" @click="activeView = 'llm-adapter'">
          <span>LLM 适配器</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="m8 7.5 2.7 7.8m5.3-7.8-2.7 7.8M8.5 6h7" /></svg></span>
        </button>
        <button class="feature-nav-entry" :class="{ active: activeView === 'jobs' }" @click="activeView = 'jobs'">
          <span>任务与日志</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v5h4M9 12h7m-7 4h7" /></svg></span>
        </button>
        <button v-if="isAdmin" class="feature-nav-entry" :class="{ active: activeView === 'users' }" @click="activeView = 'users'">
          <span>用户与权限</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3" /><path d="M3.5 19c.5-4 2.3-6 5.5-6s5 2 5.5 6M16 8h5m-2.5-2.5v5M16 15h5m-5 4h5" /></svg></span>
        </button>
      </nav>

      <div class="rail-note">
        <span>本地执行代理</span>
        <strong>{{ localAgentOnline ? '当前电脑已连接' : '当前电脑未连接' }}</strong>
        <p>云端只保存任务与素材；Edge、Cookie、登录、短信和风控验证全部在你的电脑上完成。</p>
      </div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">{{ viewEyebrow }}</p>
          <h1>{{ viewTitle }}</h1>
        </div>
        <div class="session-actions">
          <div :class="['agent-status', { online: localAgentOnline }]">
            <i></i>{{ localAgentOnline ? '本地代理在线' : '本地代理离线' }}
          </div>
          <span><strong>{{ currentUser.display_name }}</strong><small>{{ currentUser.username }} · {{ currentUser.role }}</small></span>
          <button v-if="!['ai-copy', 'llm-adapter', 'users'].includes(activeView)" class="refresh" @click="refreshDashboard">刷新状态</button>
          <button class="refresh logout" type="button" @click="logout">退出</button>
        </div>
      </header>

      <p v-if="notice && !['ai-copy', 'llm-adapter'].includes(activeView)" :class="['notice', `notice-${noticeType}`]">{{ notice }}</p>

      <section v-if="!localAgentOnline" class="agent-onboarding" aria-live="polite">
        <div class="agent-onboarding-copy">
          <p class="eyebrow">ONE-TIME DEVICE SETUP</p>
          <h2>连接这台电脑</h2>
          <p>普通功能已经在云端可用。首次使用浏览器自动化时，只需安装一次本地执行助手并完成配对；以后打开本网页即可发布。</p>
          <ol>
            <li>安装并打开“MPAU 本地执行助手”</li>
            <li>在下方生成配对码，输入助手窗口</li>
            <li>配对成功后助手随 Windows 登录自动启动</li>
          </ol>
        </div>
        <div class="agent-onboarding-actions">
          <a
            v-if="agentInstallerAvailable"
            class="agent-download"
            :href="apiUrl(agentStatus.installer.download_url)"
          >下载 Windows 执行助手</a>
          <p v-else class="agent-installer-missing">安装包尚未发布，请联系管理员预装本地执行助手。</p>
          <button class="agent-pair-button" type="button" :disabled="pairingBusy" @click="generatePairingCode">
            {{ pairingBusy ? '正在生成...' : pairingCode ? '重新生成配对码' : '生成一次性配对码' }}
          </button>
          <button v-if="pairingCode" class="pairing-code" type="button" title="点击复制" @click="copyPairingCode">
            <strong>{{ pairingCode }}</strong>
            <small>{{ pairingExpiryLabel ? `${pairingExpiryLabel} 前有效 · 点击复制` : '5 分钟内有效 · 点击复制' }}</small>
          </button>
          <p v-if="pairingError" class="pairing-error">{{ pairingError }}</p>
        </div>
      </section>

      <AiCopyView
        v-if="activeView === 'ai-copy'"
        :active="activeView === 'ai-copy'"
        @import-to-workbench="importAiCopyToWorkbench"
      />

      <section v-else-if="activeView === 'publish'" class="publish-layout">
        <form class="editor-card" novalidate @submit.prevent="submitPublish">
          <div class="section-heading"><span>01</span><div><h2>选择平台与店铺</h2><p>同一平台同一店铺会自动串行执行，避免 Cookie 状态冲突。</p></div></div>
          <div class="platform-choice">
            <label :class="{ selected: form.platform === 'tmall' }"><input v-model="form.platform" type="radio" value="tmall" /><span>天猫光合</span><small>视频、文案、标签、活动话题、音乐</small></label>
            <label :class="{ selected: form.platform === 'jd' }"><input v-model="form.platform" type="radio" value="jd" /><span>京东京麦</span><small>视频、标题、商品、原创声明</small></label>
          </div>
          <p v-if="isTmall" class="workflow-tip"><strong>天猫实际步骤：</strong>上传视频 → 填写标题、文案和标签 → 参与话题 → 可选添加音乐 → 关联商品 → 设置定时 → 选择创作者声明 → 提交发布。</p>
          <p v-else class="workflow-tip"><strong>京东实际步骤：</strong>上传视频 → 填写标题 → 关联商品 → 选择创作声明与自主原创 → 设置定时 → 提交发布；出现验证码时需要在 Edge 中手动完成验证。</p>

          <div class="field-row">
            <label class="field"><span>店铺账号标识</span><input v-model="form.account" list="account-list" required placeholder="例如 shop1" /><datalist id="account-list"><option v-for="item in visibleAccounts" :key="`${item.platform}-${item.account}`" :value="item.account" /></datalist></label>
            <div class="account-actions"><span>账号状态</span><div><button type="button" class="quiet" @click="accountAction('check')">校验 Cookie</button><button type="button" class="quiet" @click="accountAction('login')">登录 / 重新登录</button><button type="button" class="quiet" @click="deleteAccount()">删除账号</button></div></div>
          </div>

          <div class="section-heading section-heading-with-action">
            <span>02</span>
            <div><h2>内容素材</h2><p>视频先通过 HTTPS 上传到云端任务区，再由当前电脑的本地代理下载并交给 Edge 上传。</p></div>
            <button type="button" class="quiet danger section-heading-action" @click="clearPublishDraft">一键清空发布配置与素材</button>
          </div>
          <div class="dropzone">
            <input id="video-file" ref="videoInput" type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/x-m4v,video/x-msvideo,video/webm,.m4v,.avi" @change="onFileChange" />
            <label for="video-file"><strong>{{ form.video ? form.video.name : '选择视频文件' }}</strong><small>{{ form.video ? `${(form.video.size / 1024 / 1024).toFixed(1)} MB` : '支持 MP4、MOV、MKV、M4V、AVI、WebM' }}</small></label>
            <button v-if="form.video" class="clear-file" type="button" @click="clearVideo">移除视频</button>
          </div>
          <p v-if="draftRestoredAt" class="draft-restored-info" role="status">
            <span class="draft-pill">已保留上次发布配置</span>
            <small v-if="draftRestoredVideoName">含上次视频：<b>{{ draftRestoredVideoName }}</b></small>
            <small v-else>可在修改后直接再次发布。</small>
          </p>
          <label class="field"><span>标题</span><input v-model="form.title" required :maxlength="isTmall ? 30 : 27" :placeholder="isTmall ? '最多 30 个字符' : '京东要求 5-27 个字符'" /></label>

          <template v-if="isTmall">
            <label class="field"><span>发布文案 <em>可选</em></span><textarea v-model="form.description" :maxlength="descriptionLimit" placeholder="填写视频描述与种草文案" /><small class="field-hint">文案与标签会写入同一富文本字段：{{ contentTextLength }} / 1000</small></label>
            <div class="field-row">
              <label class="field"><span>标签 <em>最多 4 个</em></span><input v-model="form.tags" placeholder="女鞋,夏季穿搭,通勤鞋" /><small class="field-hint">用英文逗号分隔；上传器会在文案中逐个添加话题。</small></label>
              <label class="field"><span>活动话题</span><input v-model="form.activityTopic" placeholder="例如：夏日上新" /><small class="field-hint">留空表示不参加话题活动；填写后会搜索并选择匹配活动。</small></label>
            </div>
            <label class="field"><span>音乐名称 <em>可选</em></span><input v-model="form.musicName" maxlength="100" placeholder="例如：默契" /><small class="field-hint">留空即不添加音乐；填写后会在天猫音乐库中每次输入两个字符，选择第一个同名结果并确认。</small></label>
          </template>
          <p v-else class="platform-tip">京东京麦当前上传器不支持独立文案与标签字段；标题会写入平台正文标题。</p>

          <div class="section-heading"><span>03</span><div><h2>发布设置</h2><p>先用流程验证确认页面字段和商品匹配无误，再执行正式发布。</p></div></div>
          <div class="field-row">
            <label v-if="isTmall" class="field"><span>商品 ID <em>可选，最多 6 个</em></span><textarea v-model="form.goodsId" maxlength="256" placeholder="每行一个商品 ID，或用逗号分隔" /><small class="field-hint">系统会按填写顺序逐个搜索并勾选，全部完成后统一确认；重复 ID 会自动去重。</small></label>
            <label v-else class="field"><span>商品 ID <em>可选</em></span><input v-model="form.goodsId" inputmode="numeric" placeholder="纯数字商品 ID" /><small class="field-hint">京东一次只能关联一个商品。</small></label>
            <div class="field"><span>定时发布 <em>可选</em></span><div class="schedule-input-wrap"><input ref="scheduleInput" v-model="form.schedule" :min="scheduleMinimum" aria-hidden="true" class="schedule-input" tabindex="-1" type="datetime-local" /><button aria-label="选择定时发布时间" class="schedule-display" type="button" @click="openSchedulePicker"><span>{{ scheduleDisplay }}</span><svg aria-hidden="true" viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15.5" rx="2" /><path d="M7.5 3.5v3M16.5 3.5v3M3.5 9h17M7.5 12h.01M12 12h.01M16.5 12h.01M7.5 16h.01M12 16h.01M16.5 16h.01" /></svg></button></div><small class="field-hint">至少提前 2 小时；排队过久导致不足 2 小时时，任务会在打开发布页前停止。</small></div>
          </div>
          <label class="field"><span>创作者声明 <em>必选</em></span><select v-model="form.creatorDeclaration" required><option disabled value="">请选择与实际内容相符的声明</option><option v-for="item in creatorDeclarationOptions" :key="item" :value="item">{{ item }}</option></select><small class="field-hint">系统会按此选项精确匹配平台声明，不再自动选择“内容无需标注”。</small></label>
          <div class="toggles">
            <label><input v-model="form.dryRun" type="checkbox" /><span><strong>流程验证</strong><small>填写并上传，但不点击正式发布</small></span></label>
            <label><input v-model="form.headed" type="checkbox" /><span><strong>显示 Microsoft Edge</strong><small>登录、短信和京东验证码需要在可见浏览器中手动完成</small></span></label>
            <label v-if="!isTmall"><input v-model="form.original" type="checkbox" /><span><strong>自主原创</strong><small>仅账号已开通该能力时可用</small></span></label>
          </div>
          <p class="uploader-note"><strong>创作者声明：</strong>必须根据视频和发布内容实际情况选择；如平台账号没有对应选项，任务会在点击发布前停止。</p>
          <p v-if="publishError" class="publish-error" role="alert">{{ publishError }}</p>
          <button class="primary" :disabled="submitting" type="submit">{{ submitting ? '正在创建任务…' : form.dryRun ? '创建流程验证任务' : '创建正式发布任务' }}</button>
        </form>

        <aside class="summary-panel">
          <p class="eyebrow">TODAY'S PULSE</p>
          <div class="metric"><strong>{{ counts.total }}</strong><span>全部任务</span></div>
          <div class="metrics"><div><strong>{{ counts.running }}</strong><span>执行中</span></div><div><strong>{{ counts.done }}</strong><span>已完成</span></div><div><strong>{{ counts.failed }}</strong><span>需处理</span></div></div>
          <div class="checklist"><h3>每次发布前</h3><p><b>1</b> 确认本地代理在线</p><p><b>2</b> 先校验本机 Cookie</p><p><b>3</b> 首次建议使用流程验证</p><p><b>4</b> 任务期间不要关闭代理或 Edge</p></div>
        </aside>
      </section>

      <LlmAdapterView v-else-if="activeView === 'llm-adapter'" />

      <UserManagementView v-else-if="activeView === 'users'" :current-user-id="currentUser.id" />

      <section v-else-if="activeView === 'batch'" class="batch-layout">
        <form class="editor-card batch-card" novalidate @submit.prevent="submitBatch">
          <div class="section-heading"><span>01</span><div><h2>选择平台与店铺</h2><p>Excel 中不需要重复填写平台和店铺；切换平台后请使用对应的模板。</p></div></div>
          <div class="platform-choice batch-platform-choice">
            <label :class="{ selected: batchForm.platform === 'tmall' }"><input v-model="batchForm.platform" type="radio" value="tmall" /><span>天猫光合</span><small>视频、标题、文案、标签、话题、音乐、商品</small></label>
            <label :class="{ selected: batchForm.platform === 'jd' }"><input v-model="batchForm.platform" type="radio" value="jd" /><span>京东京麦</span><small>视频、标题、商品、定时发布、自主原创</small></label>
          </div>
          <div class="field-row">
            <label class="field"><span>店铺账号标识</span><input v-model="batchForm.account" list="batch-account-list" required placeholder="例如 shop1" /><datalist id="batch-account-list"><option v-for="item in batchAccounts" :key="`${batchForm.platform}-batch-${item.account}`" :value="item.account" /></datalist></label>
            <div class="account-actions"><span>账号状态</span><div><button type="button" class="quiet" @click="accountAction('check', batchForm.platform, batchForm.account)">校验 Cookie</button><button type="button" class="quiet" @click="accountAction('login', batchForm.platform, batchForm.account)">登录 / 重新登录</button><button type="button" class="quiet" @click="deleteAccount(batchForm.platform, batchForm.account)">删除账号</button></div></div>
          </div>

          <div class="section-heading"><span>02</span><div><h2>上传批量视频素材</h2><p>文件保存到当前用户的云端素材目录，执行时由当前电脑的代理逐个下载；Excel 填写下方文件名。</p></div></div>
          <div class="media-library">
            <div class="media-upload-row">
              <input ref="batchMediaInput" multiple type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/x-m4v,video/x-msvideo,video/webm,.m4v,.avi" @change="onBatchMediaChange" />
              <button class="quiet" :disabled="mediaUploading" type="button" @click="uploadBatchMedia">{{ mediaUploading ? '正在上传…' : `上传所选视频${selectedMediaUploads.length ? `（${selectedMediaUploads.length}）` : ''}` }}</button>
              <button class="quiet" type="button" @click="refreshMediaFiles">刷新素材</button>
            </div>
            <p v-if="mediaError" class="publish-error" role="alert">{{ mediaError }}</p>
            <div v-if="mediaFiles.length" class="media-file-list">
              <article v-for="file in mediaFiles" :key="file.name"><code>{{ file.name }}</code><span>{{ formatFileSize(file.size) }}</span><button type="button" @click="deleteMediaFile(file)">删除</button></article>
            </div>
            <p v-else class="field-hint">素材目录为空。上传后，将这里显示的文件名原样填入 Excel。</p>
          </div>

          <div class="section-heading"><span>03</span><div><h2>导入{{ batchPlatformLabel }}内容表</h2><p>每个非空行都会生成一条任务；所有行先通过校验，才会一次性进入队列。</p></div></div>
          <div class="batch-guide">
            <div><strong>Excel 列</strong><span>{{ isTmallBatch ? '视频路径、标题、文案、标签、商品ID、活动话题、音乐名称、定时发布、创作者声明' : '视频路径、标题、商品ID、定时发布、自主原创、创作者声明' }}</span></div>
            <div><strong>视频路径</strong><span>填写当前用户素材目录内的相对路径，例如 <code>video.mp4</code>。</span></div>
            <div v-if="isTmallBatch"><strong>天猫规则</strong><span>标题最多 30 字；标签最多 4 个；文案最多 1000 字；商品 ID 最多 6 个，以逗号或空格分隔；音乐名称可选，最多 100 字。</span></div>
            <div v-else><strong>京东规则</strong><span>标题为 5-27 字；“自主原创”填写“是”或“否”；当前上传器不支持文案、标签和活动话题。</span></div>
            <a class="template-link" :href="apiUrl(`/api/batch-templates/${batchForm.platform}`)">下载{{ batchPlatformLabel }} Excel 模板</a>
          </div>
          <div class="dropzone batch-dropzone">
            <input id="batch-workbook" ref="batchWorkbookInput" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" @change="onBatchWorkbookChange" />
            <label for="batch-workbook"><strong>{{ batchForm.workbook ? batchForm.workbook.name : `选择${batchPlatformLabel}批量发布 Excel` }}</strong><small>{{ batchForm.workbook ? `${(batchForm.workbook.size / 1024).toFixed(0)} KB` : '仅支持 .xlsx；包含“视频路径”和“标题”表头，单次最多 200 行' }}</small></label>
            <button v-if="batchForm.workbook" class="clear-file" type="button" @click="clearBatchWorkbook">移除表格</button>
          </div>
          <div v-if="batchErrors.length" class="batch-errors"><strong>以下内容未通过校验，未创建任何任务：</strong><p v-for="error in batchErrors" :key="`${error.row}-${error.field}-${error.message}`">第 {{ error.row }} 行 · {{ error.field }}：{{ error.message }}</p></div>
          <div v-if="batchResult" class="batch-result"><strong>已创建 {{ batchResult.created_count }} 条任务</strong><span>批次编号：{{ batchResult.batch_id }}</span><button type="button" class="quiet" @click="activeView = 'jobs'">前往任务与日志</button></div>

          <div class="section-heading"><span>04</span><div><h2>执行方式</h2><p>同一店铺会严格串行执行；某一行失败不会阻止后续任务继续运行。</p></div></div>
          <div class="toggles">
            <label><input v-model="batchForm.dryRun" type="checkbox" /><span><strong>流程验证</strong><small>填写并上传每一行内容，但不点击正式发布</small></span></label>
            <label><input v-model="batchForm.headed" type="checkbox" /><span><strong>显示 Microsoft Edge</strong><small>登录、短信和风控验证需要在可见浏览器中手动完成</small></span></label>
          </div>
          <p v-if="isTmallBatch" class="uploader-note"><strong>导入规则：</strong>若任一行的标题、视频路径、标签、商品 ID 或定时发布时间不符合天猫上传器要求，整份表格不会创建任务；请修正后重新导入。</p>
          <p v-else class="uploader-note"><strong>导入规则：</strong>若任一行的标题、视频路径、商品 ID、定时发布时间或自主原创字段不符合京东上传器要求，整份表格不会创建任务；请修正后重新导入。</p>
          <p v-if="batchSubmitError" class="publish-error" role="alert">{{ batchSubmitError }}</p>
          <button class="primary" :disabled="batchSubmitting" type="submit">{{ batchSubmitting ? '正在校验并创建任务…' : batchForm.dryRun ? `创建${batchPlatformLabel}流程验证任务` : `创建${batchPlatformLabel}正式发布任务` }}</button>
        </form>

        <aside class="summary-panel batch-summary-panel">
          <p class="eyebrow">{{ batchForm.platform.toUpperCase() }} BATCH</p>
          <div class="metric"><strong>200</strong><span>单次最多内容行</span></div>
          <div class="checklist"><h3>导入前检查</h3><p><b>1</b> 确认本地代理在线</p><p><b>2</b> 先校验本机 Cookie</p><p><b>3</b> 首次建议整表流程验证</p><p><b>4</b> 任务期间不要关闭代理或 Edge</p></div>
        </aside>
      </section>

      <section v-else-if="activeView === 'jobs'" class="jobs-layout">
        <div class="jobs-card"><div class="section-heading"><span>LIVE</span><div><h2>任务记录</h2><p>每页最多 500 条并自动刷新；点击条目可查看该任务的独立日志。</p></div></div>
          <article v-for="job in jobs" :key="job.id" class="job-row">
            <button class="job-details" :class="{ current: selectedJob?.id === job.id }" type="button" @click="loadJob(job.id)"><span class="job-platform">{{ platformLabel(job.platform) }}</span><span class="job-title"><strong>{{ jobLabel(job.kind) }}<template v-if="job.source_row"> · Excel 第 {{ job.source_row }} 行</template> · {{ job.account }}</strong><small>{{ job.message }}</small></span><span :class="statusClass(job.status)">{{ statusLabel(job.status) }}</span></button>
            <div v-if="canCancelJob(job) || canDeleteJob(job)" class="job-actions"><button v-if="canCancelJob(job)" class="delete-job" type="button" @click="cancelJobAndDeleteAccount(job)">中断并删除账号</button><button v-if="canDeleteJob(job)" class="delete-job" type="button" @click="deleteJob(job)">删除</button></div>
          </article>
          <p v-if="!jobs.length" class="empty">还没有任务。先从“发布工作台”创建一个流程验证任务。</p>
          <div v-if="jobSummary.total" class="jobs-pagination"><span>第 {{ jobsPageStart }}-{{ jobsPageEnd }} 条，共 {{ jobSummary.total }} 条</span><div><button class="quiet" type="button" :disabled="!hasPreviousJobs" @click="changeJobsPage(-1)">上一页</button><button class="quiet" type="button" :disabled="!hasMoreJobs" @click="changeJobsPage(1)">下一页</button></div></div>
        </div>
        <aside class="log-card"><div v-if="selectedJob"><div class="log-header"><div><p class="eyebrow">TASK DETAIL</p><h2>{{ platformLabel(selectedJob.platform) }} · {{ selectedJob.account }}</h2></div><span :class="statusClass(selectedJob.status)">{{ statusLabel(selectedJob.status) }}</span></div><p class="detail-message">{{ selectedJob.message }}</p><p v-if="selectedJob.error" class="error-message">{{ selectedJob.error }}</p><pre>{{ jobLogs.join('\n') || '暂时没有平台日志。任务启动后会在此显示最近日志。' }}</pre></div><p v-else class="empty">选择左侧任务查看详情与日志。</p></aside>
      </section>
    </section>
  </main>
</template>
