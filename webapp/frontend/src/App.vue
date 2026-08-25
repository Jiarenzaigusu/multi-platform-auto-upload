<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { apiRequest as request, apiUrl, configureApiClient } from './api-client.js'
import AgentSetupDialog from './components/AgentSetupDialog.vue'
import AuthGate from './components/AuthGate.vue'
import AiCopyView from './features/ai-copy/AiCopyView.vue'
import LlmAdapterView from './features/llm-adapter/LlmAdapterView.vue'
import UserManagementView from './features/users/UserManagementView.vue'

const apiBase = import.meta.env.VITE_API_BASE_URL || ''
const currentUser = ref(null)
const agentSetupOpen = ref(false)
const agentStatus = reactive({
  checked: false,
  checking: false,
  online: false,
  deviceName: '',
  system: '',
  unavailable: false,
  agentVersion: '',
  latestVersion: '',
})
configureApiClient({ baseUrl: apiBase, onUnauthorized: endAuthenticatedSession })

// === 发布草稿持久化（localStorage 文本 + IndexedDB 视频） ===
const DRAFT_DB_NAME = 'mpau_publish_drafts'
const DRAFT_DB_VERSION = 1
const DRAFT_VIDEO_STORE = 'videos'
const DRAFT_VIDEO_MAX_BYTES = 100 * 1024 * 1024
const MAX_COVER_IMAGE_BYTES = 20 * 1024 * 1024
const tmallCreatorDeclarationOptions = [
  '内容无需标注',
  '内容含营销信息',
  '含AI生成内容',
  '含虚构演绎内容',
  '内容为转载',
  '个人观点，仅供参考',
]
const jdCreatorDeclarationOptions = [
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
let isSwitchingWorkspace = false

/** Namespace browser drafts by immutable user ID to prevent cross-login leakage. */
function formDraftStorageKey() {
  return currentUser.value ? `mpau_publish_form_draft_v4:${currentUser.value.id}` : ''
}

/** Namespace the IndexedDB video record by immutable user ID. */
function draftVideoKey(platform = form.platform, contentType = form.contentType) {
  return currentUser.value
    ? `last_publish_video:${currentUser.value.id}:${platform}:${contentType}`
    : ''
}

const WORKSPACE_DRAFT_KEYS = [
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

function createEmptyWorkspaceDraft() {
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

function workspaceKey(platform, contentType) {
  return `${platform}_${contentType}`
}

function createWorkspaceDrafts() {
  return Object.fromEntries(
    ['tmall', 'jd'].flatMap((platform) => ['video', 'article'].map((contentType) => [
      workspaceKey(platform, contentType), createEmptyWorkspaceDraft(),
    ])),
  )
}

function normalizeWorkspaceDraft(saved) {
  const draft = createEmptyWorkspaceDraft()
  if (!saved || typeof saved !== 'object') return draft
  for (const key of WORKSPACE_DRAFT_KEYS) {
    if (key === 'original') {
      if (typeof saved[key] === 'boolean') draft[key] = saved[key]
    } else if (typeof saved[key] === 'string') {
      draft[key] = saved[key]
    }
  }
  if (![...tmallCreatorDeclarationOptions, ...jdCreatorDeclarationOptions].includes(draft.creatorDeclaration)) {
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
  const key = draftVideoKey()
  if (!key) return
  if (!file) return deleteDraftVideo(key)
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
      }, key)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.warn('保存视频草稿失败：', error)
  } finally {
    if (db) db.close()
  }
}

async function restoreDraftVideo(key = draftVideoKey()) {
  if (!key) return null
  let db
  try {
    db = await openDraftDatabase()
    const result = await new Promise((resolve, reject) => {
      const tx = db.transaction(DRAFT_VIDEO_STORE, 'readonly')
      const req = tx.objectStore(DRAFT_VIDEO_STORE).get(key)
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

async function deleteDraftVideo(key = draftVideoKey()) {
  if (!key) return
  let db
  try {
    db = await openDraftDatabase()
    await new Promise((resolve, reject) => {
      const tx = db.transaction(DRAFT_VIDEO_STORE, 'readwrite')
      tx.objectStore(DRAFT_VIDEO_STORE).delete(key)
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
  snapshotWorkspaceDraft()
  if (persistTimer) clearTimeout(persistTimer)
  persistTimer = setTimeout(() => {
    try {
      const payload = {
        version: 4,
        platform: form.platform,
        contentType: form.contentType,
        workspaceDrafts: Object.fromEntries(
          Object.entries(workspaceDrafts).map(([key, draft]) => [key, { ...draft }]),
        ),
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
  if (!saved || typeof saved !== 'object') return
  const savedPlatform = saved.platform === 'tmall' || saved.platform === 'jd'
    ? saved.platform
    : form.platform
  if (saved.workspaceDrafts && typeof saved.workspaceDrafts === 'object') {
    for (const key of Object.keys(workspaceDrafts)) {
      Object.assign(workspaceDrafts[key], normalizeWorkspaceDraft(saved.workspaceDrafts[key]))
    }
  } else if (saved.platformDrafts && typeof saved.platformDrafts === 'object') {
    const contentType = saved.contentType === 'article' ? 'article' : 'video'
    for (const platform of ['tmall', 'jd']) {
      Object.assign(workspaceDrafts[workspaceKey(platform, contentType)], normalizeWorkspaceDraft(saved.platformDrafts[platform]))
    }
  }
  form.platform = savedPlatform
  if (saved.contentType === 'video' || saved.contentType === 'article') form.contentType = saved.contentType
  applyWorkspaceDraft(form.platform, form.contentType)
  if (typeof saved.dryRun === 'boolean') form.dryRun = saved.dryRun
  if (typeof saved.headed === 'boolean') form.headed = saved.headed
}

async function restorePublishDraft() {
  isRestoringDraft.value = true
  draftRestoredVideoName.value = ''
  draftRestoredAt.value = ''
  try {
    applySavedFormDraft(readSavedFormDraft())
    const videoDraft = await restoreDraftVideo(draftVideoKey(form.platform, form.contentType))
    if (videoDraft?.file && isVideo.value) {
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
  const target = `${platformLabel(form.platform)}${form.contentType === 'article' ? '图文' : '视频'}发布台`
  if (!window.confirm(`确定清空“${target}”的发布草稿和已选素材吗？其他三个发布台不会受影响。`)) {
    return
  }
  clearPublishContent()
  persistFormDraft()
  await deleteDraftVideo(draftVideoKey(form.platform, form.contentType))
  form.dryRun = true
  form.headed = true
  form.original = false
  draftRestoredVideoName.value = ''
  draftRestoredAt.value = ''
  showNotice(`已清空${target}的发布草稿和素材，其他发布台未改动`, 'success')
}

const jobs = ref([])
const jobSummary = ref({ total: 0, statuses: {} })
const jobsOffset = ref(0)
const selectedJobIds = ref([])
const batchDeleting = ref(false)
const jobsPageSize = 500
const accounts = ref([])
const activeView = ref('publish')
const selectedJob = ref(null)
const jobLogs = ref([])
const videoInput = ref(null)
const imageInput = ref(null)
const imageFolderInput = ref(null)
const coverImageInput = ref(null)
const scheduleInput = ref(null)
const batchWorkbookInput = ref(null)
const submitting = ref(false)
const localUploadStatus = ref('')
const publishError = ref('')
const batchSubmitting = ref(false)
const batchSubmitError = ref('')
const notice = ref('')
const noticeType = ref('info')
const batchErrors = ref([])
const batchResult = ref(null)
let refreshTimer
let noticeTimer
let dashboardRefreshPromise = null

const NOTICE_DISMISS_MS = 3000

const form = reactive({
  platform: 'tmall',
  contentType: 'video',
  account: '',
  video: null,
  images: [],
  coverImage: null,
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

const workspaceDrafts = reactive(createWorkspaceDrafts())

function snapshotWorkspaceDraft(platform = form.platform, contentType = form.contentType) {
  const draft = workspaceDrafts[workspaceKey(platform, contentType)]
  if (!draft) return
  for (const key of WORKSPACE_DRAFT_KEYS) {
    draft[key] = form[key]
  }
}

function applyWorkspaceDraft(platform, contentType) {
  const draft = workspaceDrafts[workspaceKey(platform, contentType)] || createEmptyWorkspaceDraft()
  for (const key of WORKSPACE_DRAFT_KEYS) {
    form[key] = draft[key]
  }
  const options = platform === 'tmall' ? tmallCreatorDeclarationOptions : jdCreatorDeclarationOptions
  if (!options.includes(form.creatorDeclaration)) form.creatorDeclaration = ''
}

const batchForm = reactive({
  platform: 'tmall',
  contentType: 'video',
  account: '',
  workbook: null,
  dryRun: true,
  headed: true,
})

const isTmall = computed(() => form.platform === 'tmall')
const creatorDeclarationOptions = computed(() => (
  isTmall.value ? tmallCreatorDeclarationOptions : jdCreatorDeclarationOptions
))
const isVideo = computed(() => form.contentType === 'video')
const isArticle = computed(() => form.contentType === 'article')
const isAdmin = computed(() => currentUser.value?.role === 'admin')
const platformLabel = (platform) => (platform === 'tmall' ? '天猫光合' : '京东京麦')
const batchPlatformLabel = computed(() => platformLabel(batchForm.platform))
const batchContentTypeLabel = computed(() => batchForm.contentType === 'article' ? '图文' : '视频')
const batchTemplateUrl = computed(() => apiUrl(
  `/api/batch-templates-v2/${batchForm.platform}?content_type=${batchForm.contentType}`,
))
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
}[activeView.value] || '智能发布中枢系统'))
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
const agentUpdateAvailable = computed(() => (
  agentStatus.online
    && agentStatus.agentVersion
    && agentStatus.latestVersion
    && compareVersions(agentStatus.agentVersion, agentStatus.latestVersion) < 0
))

const agentStatusLabel = computed(() => {
  if (!agentStatus.checked || agentStatus.checking) return '正在检查代理'
  if (agentStatus.unavailable) return '代理状态未知'
  if (!agentStatus.online) return '代理离线'
  if (agentUpdateAvailable.value) return `代理在线 · 助手有新版本 v${agentStatus.latestVersion}`
  return agentStatus.deviceName ? `代理在线 · ${agentStatus.deviceName}` : '代理在线'
})

const agentStatusDescription = computed(() => {
  if (agentStatus.unavailable) return '无法读取本地执行助手状态，请刷新后重试。'
  if (!agentStatus.online) return '未检测到已配对的本地执行助手；登录和发布任务无法执行。'
  if (agentUpdateAvailable.value) {
    return `Windows 助手有新版本 v${agentStatus.latestVersion}（当前 v${agentStatus.agentVersion || '未知'}）：点击“Windows 助手”打开窗口后可查看进度并一键更新。`
  }
  return agentStatus.system || '本地执行助手已连接，可执行登录和发布任务。'
})

function activateWorkspace(platform, contentType) {
  snapshotWorkspaceDraft()
  isSwitchingWorkspace = true
  form.platform = platform
  form.contentType = contentType
  applyWorkspaceDraft(platform, contentType)
  isSwitchingWorkspace = false
  if (contentType === 'article') clearVideo()
  else clearImages()
}

watch(() => [form.platform, form.contentType], ([platform, contentType], [previousPlatform, previousContentType]) => {
  if (isRestoringDraft.value || isSwitchingWorkspace) return
  if (previousPlatform && previousContentType && (
    previousPlatform !== platform || previousContentType !== contentType
  )) {
    snapshotWorkspaceDraft(previousPlatform, previousContentType)
    applyWorkspaceDraft(platform, contentType)
  }
  if (contentType === 'article') clearVideo()
  else clearImages()
  publishError.value = ''
  persistFormDraft()
}, { flush: 'sync' })

watch(() => batchForm.platform, () => {
  batchSubmitError.value = ''
  clearBatchWorkbook()
})

watch(() => batchForm.contentType, () => {
  batchSubmitError.value = ''
  clearBatchWorkbook()
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

function importAiCopyToWorkbench(draft) {
  if (!draft || typeof draft.title !== 'string' || typeof draft.body !== 'string') return
  if (!['tmall', 'jd'].includes(draft.platform) || !['video', 'article'].includes(draft.contentType)) return
  activateWorkspace(draft.platform, draft.contentType)
  form.title = draft.title
  const supportsDescription = draft.platform === 'tmall' || draft.contentType === 'article'
  if (supportsDescription) form.description = draft.body
  snapshotWorkspaceDraft(draft.platform, draft.contentType)
  persistFormDraft()
  showNotice(`生成的${supportsDescription ? '标题和正文' : '标题'}已导入${platformLabel(draft.platform)}${draft.contentType === 'article' ? '图文' : '视频'}发布台`, 'success')
  activeView.value = 'publish'
}

function compareVersions(a, b) {
  const parse = (value) => String(value || '').replace(/^v/, '').split('.').map((part) => Number.parseInt(part, 10) || 0)
  const left = parse(a)
  const right = parse(b)
  const width = Math.max(left.length, right.length)
  for (let index = 0; index < width; index += 1) {
    const difference = (left[index] || 0) - (right[index] || 0)
    if (difference !== 0) return difference
  }
  return 0
}

function applyAgentStatus(result) {
  const agent = Array.isArray(result?.agents) ? result.agents[0] : null
  agentStatus.checked = true
  agentStatus.checking = false
  agentStatus.unavailable = false
  agentStatus.online = Boolean(result?.online && agent)
  agentStatus.deviceName = agent?.device_name || ''
  agentStatus.system = agent?.system || ''
  agentStatus.agentVersion = agent?.version || ''
  agentStatus.latestVersion = result?.installer?.windows?.version || ''
}

async function refreshAgentStatus() {
  agentStatus.checking = true
  try {
    const result = await request('/api/agent/status')
    applyAgentStatus(result)
    return agentStatus.online
  } catch {
    agentStatus.checked = true
    agentStatus.checking = false
    agentStatus.online = false
    agentStatus.deviceName = ''
    agentStatus.system = ''
    agentStatus.agentVersion = ''
    agentStatus.latestVersion = ''
    agentStatus.unavailable = true
    return false
  }
}

async function requireOnlineAgent(targetError = null) {
  const online = await refreshAgentStatus()
  if (online) return true
  const message = agentStatus.unavailable
    ? '无法确认本地执行助手状态，请刷新页面后重试。'
    : '本地执行助手离线：请先启动并完成 Windows 助手配对。'
  if (targetError) targetError.value = message
  showNotice(message, 'error')
  return false
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
          request('/api/agent/status').catch(() => null),
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
        if (agentResult) applyAgentStatus(agentResult)
        else {
          agentStatus.checked = true
          agentStatus.checking = false
          agentStatus.online = false
          agentStatus.deviceName = ''
          agentStatus.system = ''
          agentStatus.agentVersion = ''
          agentStatus.latestVersion = ''
          agentStatus.unavailable = true
        }
        syncJobSelection()
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
  if (!window.confirm(`确定删除“${jobLabel(job.kind)} · ${job.account}”任务记录及其独立日志吗？此操作不会删除 Cookie 或平台总日志。`)) return

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

function toggleJobSelection(job) {
  if (!canDeleteJob(job)) return
  const index = selectedJobIds.value.indexOf(job.id)
  if (index >= 0) selectedJobIds.value.splice(index, 1)
  else selectedJobIds.value.push(job.id)
}

function toggleSelectAllJobs() {
  const deletableIds = jobs.value.filter(canDeleteJob).map((job) => job.id)
  selectedJobIds.value = selectedJobIds.value.length === deletableIds.length
    ? []
    : deletableIds
}

function syncJobSelection() {
  const deletableIds = new Set(jobs.value.filter(canDeleteJob).map((job) => job.id))
  selectedJobIds.value = selectedJobIds.value.filter((id) => deletableIds.has(id))
}

function removeDeletedJobsFromView(deletedIds) {
  const deletedIdSet = new Set(deletedIds)
  if (!deletedIdSet.size) return 0
  const removedJobs = jobs.value.filter((job) => deletedIdSet.has(job.id))
  if (!removedJobs.length) return 0
  jobs.value = jobs.value.filter((job) => !deletedIdSet.has(job.id))
  const statusCounts = { ...(jobSummary.value.statuses || {}) }
  for (const job of removedJobs) {
    statusCounts[job.status] = Math.max(0, (statusCounts[job.status] || 0) - 1)
  }
  jobSummary.value = {
    ...jobSummary.value,
    total: Math.max(0, (jobSummary.value.total || 0) - removedJobs.length),
    statuses: statusCounts,
  }
  if (selectedJob.value && deletedIdSet.has(selectedJob.value.id)) {
    selectedJob.value = null
    jobLogs.value = []
  }
  syncJobSelection()
  return removedJobs.length
}

async function batchDeleteJobs() {
  const targets = jobs.value.filter((job) => selectedJobIds.value.includes(job.id))
  if (!targets.length || batchDeleting.value) return
  if (!window.confirm(`确定删除已选中的 ${targets.length} 条任务记录及其独立日志吗？此操作不会删除 Cookie 或平台总日志。`)) return
  batchDeleting.value = true
  try {
    const result = await request('/api/jobs/batch-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: targets.map((job) => job.id) }),
    })
    const deletedIds = new Set(result.deleted || [])
    removeDeletedJobsFromView(deletedIds)
    selectedJobIds.value = []
    const skipped = result.skipped || []
    if (deletedIds.size && skipped.length) showNotice(`已删除 ${deletedIds.size} 条；跳过 ${skipped.length} 条（仅已完成或失败的任务可删除）`, 'success')
    else if (deletedIds.size) showNotice(`已删除 ${deletedIds.size} 条任务记录`, 'success')
    else if (skipped.length) showNotice('所选任务均不能删除（仅已完成或失败的任务可删除）', 'error')
    await refreshDashboard()
  } catch (error) {
    try {
      await refreshDashboard()
      const remainingIds = new Set(jobs.value.map((job) => job.id))
      const deletedAfterRetry = targets.filter((job) => !remainingIds.has(job.id))
      if (deletedAfterRetry.length) {
        removeDeletedJobsFromView(deletedAfterRetry.map((job) => job.id))
        selectedJobIds.value = selectedJobIds.value.filter((id) => remainingIds.has(id))
        showNotice(`已删除 ${deletedAfterRetry.length} 条任务记录`, 'success')
        return
      }
    } catch {
      // Keep the original mutation error if the recovery refresh also fails.
    }
    showNotice(error.message, 'error')
  } finally {
    batchDeleting.value = false
  }
}

async function cancelJobAndDeleteAccount(job) {
  if (!canCancelJob(job)) return
  if (!window.confirm(`确定中断“${jobLabel(job.kind)} · ${job.account}”任务，并在浏览器退出后删除该店铺的 Cookie 和账号建议吗？历史任务和平台日志会保留。`)) return

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
  const label = platformLabel(platform)
  if (!window.confirm(`确定删除“${label} · ${account}”的 Cookie 和账号建议吗？任务记录和平台日志不会删除。`)) return

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

function onCoverImageChange(event) {
  const file = event.target.files?.[0] || null
  // Selecting "Cancel" must not discard the previously selected cover.
  if (!file) return
  form.coverImage = file
  publishError.value = ''
}

function clearVideo() {
  form.video = null
  if (videoInput.value) videoInput.value.value = ''
  deleteDraftVideo()
  draftRestoredVideoName.value = ''
}

function onImagesChange(event) {
  form.images = Array.from(event.target.files || [])
  if (imageFolderInput.value) imageFolderInput.value.value = ''
  const maxImages = isTmall.value ? 9 : 20
  publishError.value = form.images.length > maxImages ? `图片超过 ${maxImages} 张，请移除多余图片后重试` : ''
}

function onImageFolderChange(event) {
  const images = Array.from(event.target.files || [])
    .filter((file) => (isTmall.value ? /\.(jpe?g|png|webp)$/i : /\.(jpe?g|png)$/i).test(file.name))
    .sort((left, right) => (left.webkitRelativePath || left.name).localeCompare(
      right.webkitRelativePath || right.name,
      undefined,
      { numeric: true, sensitivity: 'base' },
    ))
  form.images = images
  if (imageInput.value) imageInput.value.value = ''
  const maxImages = isTmall.value ? 9 : 20
  publishError.value = images.length > maxImages ? `文件夹内图片超过 ${maxImages} 张，请移除多余图片后重试` : ''
}

function clearImages() {
  form.images = []
  if (imageInput.value) imageInput.value.value = ''
  if (imageFolderInput.value) imageFolderInput.value.value = ''
}

function moveImage(index, direction) {
  const target = index + direction
  if (target < 0 || target >= form.images.length) return
  const images = [...form.images]
  ;[images[index], images[target]] = [images[target], images[index]]
  form.images = images
}

function removeImage(index) {
  form.images = form.images.filter((_, imageIndex) => imageIndex !== index)
}

function clearCoverImage() {
  form.coverImage = null
  if (coverImageInput.value) coverImageInput.value.value = ''
}

function clearPublishContent() {
  clearVideo()
  clearImages()
  clearCoverImage()
  const emptyDraft = createEmptyWorkspaceDraft()
  Object.assign(workspaceDrafts[workspaceKey(form.platform, form.contentType)], emptyDraft)
  for (const key of WORKSPACE_DRAFT_KEYS) form[key] = emptyDraft[key]
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
  if (!form.account.trim()) {
    publishError.value = '请先选择或填写店铺账号标识'
    return
  }
  if (!await requireOnlineAgent(publishError)) return
  if (isVideo.value && !form.video) {
    publishError.value = '请先重新选择一个视频文件'
    return
  }
  const articleImageLimit = isTmall.value ? 9 : 20
  if (isArticle.value && (!form.images.length || form.images.length > articleImageLimit)) {
    publishError.value = `${isTmall.value ? '天猫' : '京东'}图文必须选择 1-${articleImageLimit} 张图片`
    return
  }
  if (isArticle.value && form.images.some((image) => !(
    isTmall.value ? /\.(jpe?g|png|webp)$/i : /\.(jpe?g|png)$/i
  ).test(image.name))) {
    publishError.value = isTmall.value
      ? '图文图片仅支持 JPG、PNG 或 WebP 格式'
      : '京东图文图片仅支持 JPG 或 PNG 格式'
    return
  }
  if (isVideo.value && form.coverImage && form.coverImage.size > MAX_COVER_IMAGE_BYTES) {
    publishError.value = '封面图片不能超过 20 MiB'
    return
  }
  if (!form.title.trim()) {
    publishError.value = `请先填写${isArticle.value ? '图文' : '视频'}标题`
    return
  }
  if (!creatorDeclarationOptions.value.includes(form.creatorDeclaration)) {
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
  if (!isTmall.value && uniqueGoodsIds.value.length > 10) {
    publishError.value = '京东一次最多关联 10 个商品 ID'
    return
  }
  submitting.value = true
  const data = new FormData()
  data.append('platform', form.platform)
  data.append('account', form.account)
  data.append('content_type', form.contentType)
  data.append('title', form.title)
  data.append('description', isTmall.value || isArticle.value ? form.description : '')
  data.append('tags', isTmall.value ? form.tags : '')
  data.append('goods_id', form.goodsId)
  data.append('activity_topic', form.activityTopic)
  data.append('music_name', isTmall.value ? form.musicName : '')
  data.append('creator_declaration', form.creatorDeclaration)
  data.append('schedule', form.schedule.replace('T', ' '))
  data.append('original', String(isTmall.value ? false : form.original))
  data.append('dry_run', String(form.dryRun))
  data.append('headed', String(form.headed))

  try {
    localUploadStatus.value = '正在将素材传给 Windows 助手…'
    if (isVideo.value) {
      const [videoAsset, coverAsset] = await Promise.all([
        uploadFileToAgent(form.video, 'video'),
        form.coverImage
          ? uploadFileToAgent(form.coverImage, 'cover')
          : Promise.resolve(null),
      ])
      data.append('video_asset_id', videoAsset.asset_id)
      if (coverAsset) data.append('cover_asset_id', coverAsset.asset_id)
    } else {
      const imageAssets = await Promise.all(
        form.images.map((image) => uploadFileToAgent(image, 'article-image')),
      )
      data.append('image_asset_ids', JSON.stringify(imageAssets.map((asset) => asset.asset_id)))
    }
    localUploadStatus.value = '素材已到达 Windows 助手，正在创建任务…'
    const result = await request('/api/jobs/publish', { method: 'POST', body: data })
    showNotice(`${platformLabel(form.platform)}${form.dryRun ? '流程验证' : '发布'}任务已创建，配置已保留可继续修改后再次发布`, 'success')
    await refreshDashboard()
    await loadJob(result.job.id)
  } catch (error) {
    publishError.value = error.message
    showNotice(error.message, 'error')
  } finally {
    submitting.value = false
    localUploadStatus.value = ''
  }
}

async function uploadFileToAgent(file, kind) {
  const ticket = await request('/api/agent/local-upload-ticket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name, size: file.size, kind }),
  })
  const separator = ticket.upload_url.includes('?') ? '&' : '?'
  let response
  try {
    response = await fetch(`${ticket.upload_url}${separator}ticket=${encodeURIComponent(ticket.ticket)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
      body: file,
      mode: 'cors',
      cache: 'no-store',
    })
  } catch (error) {
    throw new Error('无法连接 Windows 助手本机上传服务，请更新并重启助手后重试')
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok || !body.asset) {
    throw new Error(body.detail || 'Windows 助手接收素材失败')
  }
  return body.asset
}

async function submitBatch() {
  batchSubmitError.value = ''
  if (!batchForm.account.trim()) {
    batchSubmitError.value = `请先选择或填写${batchPlatformLabel.value}店铺账号标识`
    return
  }
  if (!batchForm.workbook) {
    batchSubmitError.value = `请先重新选择${batchPlatformLabel.value}批量发布 Excel 文件`
    return
  }
  if (!await requireOnlineAgent(batchSubmitError)) return

  batchSubmitting.value = true
  batchErrors.value = []
  batchResult.value = null
  const data = new FormData()
  data.append('account', batchForm.account)
  data.append('workbook', batchForm.workbook)
  data.append('content_type', batchForm.contentType)
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
  if (!await requireOnlineAgent()) return
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
    contentType: 'video',
    account: '',
    video: null,
    images: [],
    coverImage: null,
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
  for (const key of Object.keys(workspaceDrafts)) {
    Object.assign(workspaceDrafts[key], createEmptyWorkspaceDraft())
  }
  Object.assign(batchForm, {
    platform: 'tmall',
    contentType: 'video',
    account: '',
    workbook: null,
    dryRun: true,
    headed: true,
  })
  jobs.value = []
  accounts.value = []
  jobSummary.value = { total: 0, statuses: {} }
  jobsOffset.value = 0
  selectedJob.value = null
  jobLogs.value = []
  batchErrors.value = []
  batchResult.value = null
  agentStatus.checked = false
  agentStatus.checking = false
  agentStatus.online = false
  agentStatus.deviceName = ''
  agentStatus.system = ''
  agentStatus.unavailable = false
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
    <AgentSetupDialog
      v-if="agentSetupOpen"
      :agent-version="agentStatus.agentVersion"
      :latest-version="agentStatus.latestVersion"
      :online="agentStatus.online"
      @close="agentSetupOpen = false"
    />
    <aside class="rail">
      <div class="brand">
        <span class="brand-mark">M</span>
        <div><strong>智能发布中枢系统</strong></div>
      </div>

      <nav>
        <button class="feature-nav-entry" :class="{ active: activeView === 'ai-copy' }" @click="activeView = 'ai-copy'">
          <span>AI 文案工坊</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3c.6 3.3 2.7 5.4 6 6-3.3.6-5.4 2.7-6 6-.6-3.3-2.7-5.4-6-6 3.3-.6 5.4-2.7 6-6Z" /><path d="M18.5 15.5c.2 1.4 1.1 2.3 2.5 2.5-1.4.2-2.3 1.1-2.5 2.5-.2-1.4-1.1-2.3-2.5-2.5 1.4-.2 2.3-1.1 2.5-2.5Z" /></svg></span>
        </button>
        <button class="feature-nav-entry" :class="{ active: activeView === 'publish' }" @click="activeView = 'publish'">
          <span>发布工作台</span>
          <span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 15V4m0 0L8 8m4-4 4 4" /><path d="M5 14v5h14v-5" /></svg></span>
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
        <span>运行范围</span>
        <strong>仅天猫与京东</strong>
        <p>浏览器自动化在本机运行。登录、短信和风控验证需要你在 Microsoft Edge 中完成。</p>
      </div>
    </aside>

    <section :class="['workspace', { 'workspace-ai-copy': activeView === 'ai-copy' }]">
      <header class="topbar">
        <div>
          <p class="eyebrow">{{ viewEyebrow }}</p>
          <h1>{{ viewTitle }}</h1>
        </div>
        <div class="session-actions">
          <span><strong>{{ currentUser.display_name }}</strong><small>{{ currentUser.username }} · {{ currentUser.role }}</small></span>
          <button
            class="agent-connection-status"
            :class="{ online: agentStatus.online, offline: !agentStatus.online && agentStatus.checked && !agentStatus.unavailable, unknown: agentStatus.unavailable || !agentStatus.checked, updates: agentUpdateAvailable }"
            :title="agentStatusDescription"
            type="button"
            @click="refreshAgentStatus"
          >
            <i aria-hidden="true"></i>{{ agentStatusLabel }}
          </button>
          <button class="refresh agent-windows" :class="{ updates: agentUpdateAvailable }" type="button" @click="agentSetupOpen = true">Windows 助手</button>
          <button v-if="!['ai-copy', 'llm-adapter', 'users'].includes(activeView)" class="refresh" @click="refreshDashboard">刷新状态</button>
          <button class="refresh logout" type="button" @click="logout">退出</button>
        </div>
      </header>

      <p v-if="notice && !['ai-copy', 'llm-adapter'].includes(activeView)" :class="['notice', `notice-${noticeType}`]">{{ notice }}</p>

      <AiCopyView
        v-if="activeView === 'ai-copy'"
        :active="activeView === 'ai-copy'"
        :user-id="currentUser.id"
        @import-to-workbench="importAiCopyToWorkbench"
      />

      <section v-else-if="activeView === 'publish'" class="publish-layout">
        <form class="editor-card" novalidate @submit.prevent="submitPublish">
          <div class="section-heading"><span>01</span><div><h2>选择平台、发布类型与店铺</h2></div></div>
          <p class="choice-label">选择平台</p>
          <div class="platform-choice">
            <label :class="{ selected: form.platform === 'tmall' }"><input v-model="form.platform" type="radio" value="tmall" /><span>天猫光合</span></label>
            <label :class="{ selected: form.platform === 'jd' }"><input v-model="form.platform" type="radio" value="jd" /><span>京东京麦</span></label>
          </div>
          <p class="choice-label">选择发布类型</p>
          <div class="platform-choice content-type-choice">
            <label :class="{ selected: form.contentType === 'video' }"><input v-model="form.contentType" type="radio" value="video" /><span>视频发布</span></label>
            <label :class="{ selected: form.contentType === 'article' }"><input v-model="form.contentType" type="radio" value="article" /><span>图文发布</span></label>
          </div>
          <p v-if="isTmall && isVideo" class="workflow-tip"><strong>天猫视频步骤：</strong>上传视频 → 可选设置自定义封面 → 填写标题、文案和标签 → 参与话题 → 可选添加音乐 → 关联商品 → 设置定时 → 选择创作者声明 → 提交发布。</p>
          <p v-else-if="isTmall" class="workflow-tip"><strong>天猫图文步骤：</strong>按顺序上传 1-9 张图片 → 填写标题、文案和标签 → 参与话题 → 可选添加音乐 → 关联商品 → 设置定时 → 选择创作者声明 → 提交发布。</p>
          <p v-else-if="isVideo" class="workflow-tip"><strong>京东视频步骤：</strong>上传视频 → 可选设置封面 → 填写标题 → 关联商品/参与话题 → 选择创作声明与自主原创 → 设置定时 → 提交发布。</p>
          <p v-else class="workflow-tip"><strong>京东图文步骤：</strong>按顺序上传 1-20 张 JPG/PNG 图片 → 填写标题与正文 → 关联商品/参与话题 → 选择创作声明与自主原创 → 设置定时 → 提交发布。</p>

          <p class="choice-label">选择店铺</p>
          <div class="field-row">
            <label class="field"><input v-model="form.account" list="account-list" required placeholder="例如 shop1" /><datalist id="account-list"><option v-for="item in visibleAccounts" :key="`${item.platform}-${item.account}`" :value="item.account" /></datalist></label>
            <div class="account-actions"><span>账号状态</span><div><button type="button" class="quiet" @click="accountAction('check')">校验 Cookie</button><button type="button" class="quiet" @click="accountAction('login')">登录 / 重新登录</button><button type="button" class="quiet" @click="deleteAccount()">删除账号</button></div></div>
          </div>

          <div class="section-heading section-heading-with-action">
            <span>02</span>
            <div><h2>内容素材</h2></div>
            <button type="button" class="quiet danger section-heading-action" @click="clearPublishDraft">一键清空发布配置与素材</button>
          </div>
          <template v-if="isVideo">
            <div class="dropzone">
              <input id="video-file" ref="videoInput" type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/x-m4v,video/x-msvideo,video/webm,.m4v,.avi" @change="onFileChange" />
              <label for="video-file"><strong>{{ form.video ? form.video.name : '选择视频文件' }}</strong><small>{{ form.video ? `${(form.video.size / 1024 / 1024).toFixed(1)} MB` : '支持 MP4、MOV、MKV、M4V、AVI、WebM' }}</small></label>
              <button v-if="form.video" class="clear-file" type="button" @click="clearVideo">移除视频</button>
            </div>
          </template>
          <div v-else class="field image-upload-field">
            <span>图文图片</span>
            <input id="article-image-files" ref="imageInput" class="native-file-input" type="file" multiple :accept="isTmall ? 'image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp' : 'image/jpeg,image/png,.jpg,.jpeg,.png'" @change="onImagesChange" />
            <label class="cover-file-picker" :class="{ selected: form.images.length }" for="article-image-files"><span class="cover-file-action">{{ form.images.length ? '重新选择图片' : '选择图片文件' }}</span><span class="cover-file-name">{{ form.images.length ? `已选择 ${form.images.length} 张图片` : `按选择顺序上传，最多 ${isTmall ? 9 : 20} 张` }}</span></label>
            <input id="article-image-folder" ref="imageFolderInput" class="native-file-input" type="file" multiple webkitdirectory directory @change="onImageFolderChange" />
            <label class="cover-file-picker" for="article-image-folder"><span class="cover-file-action">选择图片文件夹</span><span class="cover-file-name">读取文件夹第一层图片，并按文件名顺序发布</span></label>
            <ol v-if="form.images.length" class="image-file-list"><li v-for="(image, index) in form.images" :key="`${image.name}-${image.lastModified}-${index}`"><b>{{ index + 1 }}</b><span>{{ image.name }}</span><small>{{ (image.size / 1024 / 1024).toFixed(1) }} MB</small><div class="image-file-actions"><button type="button" :disabled="index === 0" @click="moveImage(index, -1)">上移</button><button type="button" :disabled="index === form.images.length - 1" @click="moveImage(index, 1)">下移</button><button type="button" @click="removeImage(index)">移除</button></div></li></ol>
            <button v-if="form.images.length" class="clear-file" type="button" @click="clearImages">清空图文素材</button>
          </div>
          <div v-if="isVideo" class="field cover-image-field">
            <span>自定义封面图片 <em>可选</em></span>
            <input id="cover-image-file" ref="coverImageInput" class="native-file-input" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" @change="onCoverImageChange" />
            <label class="cover-file-picker" :class="{ selected: form.coverImage }" for="cover-image-file">
              <span class="cover-file-action">{{ form.coverImage ? '更换封面' : '选择封面图片' }}</span>
              <span class="cover-file-name">{{ form.coverImage ? form.coverImage.name : '尚未选择封面图片' }}</span>
            </label>
            <small v-if="form.coverImage" class="cover-file-selected">已选择封面 · {{ form.coverImage.name }} · {{ (form.coverImage.size / 1024 / 1024).toFixed(1) }} MB</small>
            <button v-if="form.coverImage" class="clear-file" type="button" @click="clearCoverImage">移除封面</button>
          </div>
          <p v-if="draftRestoredAt" class="draft-restored-info" role="status">
            <span class="draft-pill">已保留上次发布配置</span>
            <small v-if="draftRestoredVideoName">含上次视频：<b>{{ draftRestoredVideoName }}</b></small>
            <small v-else>可在修改后直接再次发布。</small>
          </p>
          <label class="field"><span>标题</span><input v-model="form.title" required :maxlength="isTmall ? 30 : (isVideo ? 27 : 20)" :placeholder="isTmall ? '最多 30 个字符' : `京东要求 5-${isVideo ? 27 : 20} 个字符`" /></label>

          <template v-if="isTmall">
            <label class="field"><span>发布文案 <em>可选</em></span><textarea v-model="form.description" :maxlength="descriptionLimit" placeholder="填写视频描述与种草文案" /><small class="field-hint">文案与标签会写入同一富文本字段：{{ contentTextLength }} / 1000</small></label>
            <div class="field-row">
              <label class="field"><span>标签 <em>可选</em></span><input v-model="form.tags" placeholder="女鞋,夏季穿搭,通勤鞋" /></label>
              <label class="field"><span>活动话题 <em>可选</em></span><input v-model="form.activityTopic" placeholder="例如：夏日上新" /></label>
            </div>
            <label class="field"><span>音乐名称 <em>可选</em></span><input v-model="form.musicName" maxlength="100" placeholder="例如：默契" /></label>
          </template>
          <template v-else-if="isArticle">
            <label class="field"><span>正文内容 <em>可选</em></span><textarea v-model="form.description" maxlength="1001" placeholder="填写京东图文正文" /></label>
            <label class="field"><span>参与话题 <em>可选</em></span><input v-model="form.activityTopic" placeholder="例如：数码先锋" /></label>
          </template>
          <p v-else class="platform-tip">京东京麦视频不支持独立文案与标签字段；标题会写入平台正文标题。</p>

          <div class="section-heading"><span>03</span><div><h2>发布设置</h2></div></div>
          <div class="field-row">
            <label class="field"><span>商品 ID <em>可选</em></span><textarea v-model="form.goodsId" maxlength="256" placeholder="每行一个商品 ID" /></label>
            <div class="field"><span>定时发布 <em>可选</em></span><div class="schedule-input-wrap"><input ref="scheduleInput" v-model="form.schedule" :min="scheduleMinimum" aria-hidden="true" class="schedule-input" tabindex="-1" type="datetime-local" /><button aria-label="选择定时发布时间" class="schedule-display" type="button" @click="openSchedulePicker"><span>{{ scheduleDisplay }}</span><svg aria-hidden="true" viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15.5" rx="2" /><path d="M7.5 3.5v3M16.5 3.5v3M3.5 9h17M7.5 12h.01M12 12h.01M16.5 12h.01M7.5 16h.01M12 16h.01M16.5 16h.01" /></svg></button></div></div>
          </div>
          <label class="field"><span>创作者声明</span><select v-model="form.creatorDeclaration" required><option disabled value="">请选择与实际内容相符的声明</option><option v-for="item in creatorDeclarationOptions" :key="item" :value="item">{{ item }}</option></select></label>
          <div class="toggles">
            <label><input v-model="form.dryRun" type="checkbox" /><span><strong>流程验证</strong><small>填写并上传，但不点击正式发布</small></span></label>
            <label><input v-model="form.headed" type="checkbox" /><span><strong>显示 Microsoft Edge</strong><small>登录、短信和京东验证码需要在可见浏览器中手动完成</small></span></label>
            <label v-if="!isTmall"><input v-model="form.original" type="checkbox" /><span><strong>自主原创 <em>可选</em></strong><small>仅账号已开通该能力时可用</small></span></label>
          </div>
          <p v-if="publishError" class="publish-error" role="alert">{{ publishError }}</p>
          <button class="primary" :disabled="submitting" type="submit">{{ submitting ? (localUploadStatus || '正在创建任务…') : form.dryRun ? '创建流程验证任务' : '创建正式发布任务' }}</button>
        </form>

        <aside class="summary-panel">
          <p class="eyebrow">TODAY'S PULSE</p>
          <div class="metric"><strong>{{ counts.total }}</strong><span>全部任务</span></div>
          <div class="metrics"><div><strong>{{ counts.running }}</strong><span>执行中</span></div><div><strong>{{ counts.done }}</strong><span>已完成</span></div><div><strong>{{ counts.failed }}</strong><span>需处理</span></div></div>
          <div class="checklist"><h3>每次发布前</h3><p><b>1</b> 先校验店铺 Cookie</p><p><b>2</b> 确认视频、标题和商品 ID</p><p><b>3</b> 首次建议使用流程验证</p><p><b>4</b> 任务期间不要关闭 Microsoft Edge</p></div>
        </aside>
      </section>

      <LlmAdapterView v-else-if="activeView === 'llm-adapter'" />

      <UserManagementView v-else-if="activeView === 'users'" :current-user-id="currentUser.id" />

      <section v-else-if="activeView === 'batch'" class="batch-layout">
        <form class="editor-card batch-card" novalidate @submit.prevent="submitBatch">
          <div class="section-heading"><span>01</span><div><h2>选择平台、发布类型与店铺</h2></div></div>
          <p class="choice-label">选择平台</p>
          <div class="platform-choice batch-platform-choice">
            <label :class="{ selected: batchForm.platform === 'tmall' }"><input v-model="batchForm.platform" type="radio" value="tmall" /><span>天猫光合</span></label>
            <label :class="{ selected: batchForm.platform === 'jd' }"><input v-model="batchForm.platform" type="radio" value="jd" /><span>京东京麦</span></label>
          </div>
          <p class="choice-label">选择发布类型</p>
          <div class="content-choice batch-content-choice">
            <label :class="{ selected: batchForm.contentType === 'video' }"><input v-model="batchForm.contentType" type="radio" value="video" /><span>视频发布</span></label>
            <label :class="{ selected: batchForm.contentType === 'article' }"><input v-model="batchForm.contentType" type="radio" value="article" /><span>图文发布</span></label>
          </div>
          <p class="choice-label">选择店铺</p>
          <div class="field-row">
            <label class="field"><input v-model="batchForm.account" aria-label="店铺账号标识" list="batch-account-list" required placeholder="例如 shop1" /><datalist id="batch-account-list"><option v-for="item in batchAccounts" :key="`${batchForm.platform}-batch-${item.account}`" :value="item.account" /></datalist></label>
            <div class="account-actions"><span>账号状态</span><div><button type="button" class="quiet" @click="accountAction('check', batchForm.platform, batchForm.account)">校验 Cookie</button><button type="button" class="quiet" @click="accountAction('login', batchForm.platform, batchForm.account)">登录 / 重新登录</button><button type="button" class="quiet" @click="deleteAccount(batchForm.platform, batchForm.account)">删除账号</button></div></div>
          </div>

          <div class="section-heading section-heading-with-action batch-import-heading">
            <span>02</span>
            <div><h2>导入{{ batchPlatformLabel }}{{ batchContentTypeLabel }}内容表</h2></div>
            <a class="template-link section-heading-action" :href="batchTemplateUrl">下载{{ batchPlatformLabel }}{{ batchContentTypeLabel }} Excel 模板</a>
          </div>
          <div class="dropzone batch-dropzone">
            <input id="batch-workbook" ref="batchWorkbookInput" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" @change="onBatchWorkbookChange" />
            <label for="batch-workbook"><strong>{{ batchForm.workbook ? batchForm.workbook.name : `选择${batchPlatformLabel}${batchContentTypeLabel}批量发布 Excel` }}</strong><small>{{ batchForm.workbook ? `${(batchForm.workbook.size / 1024).toFixed(0)} KB` : `仅支持 .xlsx；包含“${batchForm.contentType === 'article' ? '图片文件夹路径' : '视频路径'}”和“标题”表头，单次最多 200 行` }}</small></label>
            <button v-if="batchForm.workbook" class="clear-file" type="button" @click="clearBatchWorkbook">移除表格</button>
          </div>
          <div v-if="batchErrors.length" class="batch-errors"><strong>以下内容未通过校验，未创建任何任务：</strong><p v-for="error in batchErrors" :key="`${error.row}-${error.field}-${error.message}`">第 {{ error.row }} 行 · {{ error.field }}：{{ error.message }}</p></div>
          <div v-if="batchResult" class="batch-result"><strong>已创建 {{ batchResult.created_count }} 条任务</strong><span>批次编号：{{ batchResult.batch_id }}</span><button type="button" class="quiet" @click="activeView = 'jobs'">前往任务与日志</button></div>

          <div class="section-heading"><span>03</span><div><h2>执行方式</h2></div></div>
          <div class="toggles">
            <label><input v-model="batchForm.dryRun" type="checkbox" /><span><strong>流程验证</strong><small>填写并上传每一行内容，但不点击正式发布</small></span></label>
            <label><input v-model="batchForm.headed" type="checkbox" /><span><strong>显示 Microsoft Edge</strong><small>登录、短信和风控验证需要在可见浏览器中手动完成</small></span></label>
          </div>
          <p v-if="batchSubmitError" class="publish-error" role="alert">{{ batchSubmitError }}</p>
          <button class="primary" :disabled="batchSubmitting" type="submit">{{ batchSubmitting ? '正在校验并创建任务…' : batchForm.dryRun ? `创建${batchPlatformLabel}流程验证任务` : `创建${batchPlatformLabel}正式发布任务` }}</button>
        </form>

        <aside class="summary-panel batch-summary-panel">
          <p class="eyebrow">{{ batchForm.platform.toUpperCase() }} BATCH</p>
          <div class="metric"><strong>200</strong><span>单次最多内容行</span></div>
          <div class="checklist"><h3>导入前检查</h3><p><b>1</b> 先校验店铺 Cookie</p><p><b>2</b> 视频路径必须在本机存在</p><p><b>3</b> 首次建议整表流程验证</p><p><b>4</b> 任务期间不要关闭 Edge</p></div>
        </aside>
      </section>

      <section v-else-if="activeView === 'jobs'" class="jobs-layout">
        <div class="jobs-card"><div class="section-heading"><span>LIVE</span><div><h2>任务记录</h2><p>每页最多 500 条并自动刷新；点击条目可查看该任务的独立日志。</p></div></div>
          <div v-if="jobs.some(canDeleteJob)" class="batch-toolbar">
            <label class="batch-toggle"><input type="checkbox" :checked="jobs.filter(canDeleteJob).length > 0 && selectedJobIds.length === jobs.filter(canDeleteJob).length" :indeterminate.prop="selectedJobIds.length > 0 && selectedJobIds.length < jobs.filter(canDeleteJob).length" @change="toggleSelectAllJobs" /><span>当前页可删除项{{ selectedJobIds.length ? `已选 ${selectedJobIds.length} 条` : '全选' }}</span></label>
            <button type="button" class="delete-job" :disabled="!selectedJobIds.length || batchDeleting" @click="batchDeleteJobs">{{ batchDeleting ? '删除中…' : `批量删除${selectedJobIds.length ? `（${selectedJobIds.length} 条）` : ''}` }}</button>
          </div>
          <article v-for="job in jobs" :key="job.id" class="job-row" :class="{ selected: selectedJobIds.includes(job.id) }">
            <input v-if="canDeleteJob(job)" type="checkbox" class="job-select" :checked="selectedJobIds.includes(job.id)" @change="toggleJobSelection(job)" :aria-label="`选中任务 ${job.id}`" />
            <span v-else class="job-select-spacer" aria-hidden="true"></span>
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
