<!--
  AiCopyView.vue：AI 文案工坊主视图。

  功能：
  - 通过 Excel 匹配商品核心卖点，或直接输入核心卖点（二选一）
  - 可选读取商品链接（最多 20 个，逐个调用商品读取工具）
  - 调用 LLM 生成标题与正文（含风格/场景/节日/目标字数）
  - 复制结果或导入发布工作台

  目标字数：输入过程不夹紧、失焦时归位到默认值或合法范围。
  生成前必须所有商品 ID 都能在卖点表中匹配。
-->
<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { createAiCopyApi } from './api.js'
import AiCopyDropdown from './AiCopyDropdown.vue'
import WorkbenchImportDialog from './WorkbenchImportDialog.vue'
import {
  clearAiCopyDraft, clearSellingPointWorkbook, loadSellingPointWorkbook, readAiCopyDraft,
  saveAiCopyDraft, saveSellingPointWorkbook,
} from './ai-copy-draft-store.js'

const props = defineProps({
  apiBase: { type: String, default: '' },
  active: { type: Boolean, default: false },
  userId: { type: String, default: '' },
})
const emit = defineEmits(['import-to-workbench'])

const api = createAiCopyApi(props.apiBase)
const options = ref({ styles: [], scenes: [], festivals: [], llm: { ready: false, model: '', provider: '' } })
const titleLimitPresets = [10, 15]
const bodyLimitPresets = [25, 50, 100, 200]
const titleCountPresets = [1, 3, 5]
const bodyCountPresets = [1, 2, 3]
const titleMin = 2
const titleMax = 100
const bodyMin = 10
const bodyMax = 1000
const candidateCountMin = 1
const candidateCountMax = 10
const titleLimitDefault = 15
const bodyLimitDefault = 100
const titleCountDefault = 1
const bodyCountDefault = 1
const hanCharacterPattern = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/g
const createDefaultForm = () => ({
  sellingPointInputMode: 'excel',
  productIdentifiers: '',
  manualSellingPoint: '',
  titleMaxChars: titleLimitDefault,
  bodyMaxChars: bodyLimitDefault,
  titleCount: titleCountDefault,
  bodyCount: bodyCountDefault,
  style: 'atmospheric_seeding',
  scene: 'daily_styling',
  festival: '',
  customStyle: '',
  customScene: '',
  customFestival: '',
  productUrls: '',
})
const form = reactive(createDefaultForm())
const loadingOptions = ref(true)
const uploadingSellingPoints = ref(false)
const readingProduct = ref(false)
const generating = ref(false)
const sellingPointCatalog = ref(null)
const productReferences = ref([])
const result = ref(null)
const error = ref('')
const success = ref('')
const copiedField = ref('')
const sellingPointFileInput = ref(null)
const batchExcelFileInput = ref(null)
const importingToBatchExcel = ref(false)
const downloadBatchExcelCopy = ref(false)
const selectedTitleIndex = ref(0)
const selectedBodyIndex = ref(0)
const editingTitleIndex = ref(null)
const editingBodyIndex = ref(null)
const titleEditValue = ref('')
const bodyEditValue = ref('')
const workbenchImportOpen = ref(false)
const restoringDraft = ref(true)
let draftSaveTimer
let copyTimer
let successTimer

const productIdentifiers = computed(() => {
  const identifiers = form.productIdentifiers
    .split(/[\s,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean)
  return [...new Map(identifiers.map((value) => [value.toLocaleLowerCase(), value])).values()]
})
const manualSellingPoint = computed(() => form.manualSellingPoint.trim())
const isManualSellingPointMode = computed(() => form.sellingPointInputMode === 'manual')
const productUrls = computed(() => [...new Set(
  form.productUrls
    .split(/\s+/)
    .map((value) => value.trim())
    .filter(Boolean),
)])
const sellingPointEntryMap = computed(() => new Map(
  (sellingPointCatalog.value?.entries || []).map((entry) => [
    entry.identifier.trim().toLocaleLowerCase(),
    entry,
  ]),
))
const matchedSellingPoints = computed(() => productIdentifiers.value
  .map((identifier) => sellingPointEntryMap.value.get(identifier.toLocaleLowerCase()))
  .filter(Boolean))
const missingProductIdentifiers = computed(() => productIdentifiers.value.filter(
  (identifier) => !sellingPointEntryMap.value.has(identifier.toLocaleLowerCase()),
))
const festivalOptions = computed(() => [
  { value: '', label: '不指定节日' },
  ...options.value.festivals.map((festival) => ({ value: festival, label: festival })),
])
const customSelectionValue = '__custom__'
const hasCustomStyle = computed(() => Boolean(form.customStyle.trim()))
const selectedScene = computed({
  get: () => (form.customScene.trim() ? customSelectionValue : form.scene),
  set: (value) => {
    form.scene = value
    form.customScene = ''
  },
})
const selectedFestival = computed({
  get: () => (form.customFestival.trim() ? customSelectionValue : form.festival),
  set: (value) => {
    form.festival = value
    form.customFestival = ''
  },
})

function normalizeTitleLimit(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return null
  return Math.min(titleMax, Math.max(titleMin, Math.round(numeric)))
}
function normalizeBodyLimit(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return null
  return Math.min(bodyMax, Math.max(bodyMin, Math.round(numeric)))
}
function normalizeCandidateCount(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return null
  return Math.min(candidateCountMax, Math.max(candidateCountMin, Math.round(numeric)))
}
function pickTitleLimit(value) {
  const normalized = normalizeTitleLimit(value)
  if (normalized === null) return
  form.titleMaxChars = normalized
}
function pickBodyLimit(value) {
  const normalized = normalizeBodyLimit(value)
  if (normalized === null) return
  form.bodyMaxChars = normalized
}
function pickTitleCount(value) {
  const normalized = normalizeCandidateCount(value)
  if (normalized !== null) form.titleCount = normalized
}
function pickBodyCount(value) {
  const normalized = normalizeCandidateCount(value)
  if (normalized !== null) form.bodyCount = normalized
}
function cleanNumericInput(raw) {
  // 只允许数字字符 + 空串；输入过程中不做夹紧，让用户能自由输入中间态。
  const cleaned = String(raw ?? '').replace(/[^\d]/g, '')
  return cleaned
}
function onTitleLimitInput(event) {
  const cleaned = cleanNumericInput(event.target.value)
  if (cleaned !== event.target.value) event.target.value = cleaned
  form.titleMaxChars = cleaned === '' ? '' : Number(cleaned)
}
function onBodyLimitInput(event) {
  const cleaned = cleanNumericInput(event.target.value)
  if (cleaned !== event.target.value) event.target.value = cleaned
  form.bodyMaxChars = cleaned === '' ? '' : Number(cleaned)
}
function onTitleCountInput(event) {
  const cleaned = cleanNumericInput(event.target.value)
  if (cleaned !== event.target.value) event.target.value = cleaned
  form.titleCount = cleaned === '' ? '' : Number(cleaned)
}
function onBodyCountInput(event) {
  const cleaned = cleanNumericInput(event.target.value)
  if (cleaned !== event.target.value) event.target.value = cleaned
  form.bodyCount = cleaned === '' ? '' : Number(cleaned)
}
function onTitleLimitBlur() {
  if (form.titleMaxChars === '' || form.titleMaxChars === null) {
    // 留空 = 不覆盖，回退到初始默认值
    form.titleMaxChars = titleLimitDefault
    return
  }
  const normalized = normalizeTitleLimit(form.titleMaxChars)
  form.titleMaxChars = normalized === null ? titleLimitDefault : normalized
}
function onBodyLimitBlur() {
  if (form.bodyMaxChars === '' || form.bodyMaxChars === null) {
    form.bodyMaxChars = bodyLimitDefault
    return
  }
  const normalized = normalizeBodyLimit(form.bodyMaxChars)
  form.bodyMaxChars = normalized === null ? bodyLimitDefault : normalized
}
function onTitleCountBlur() {
  const normalized = normalizeCandidateCount(form.titleCount)
  form.titleCount = normalized === null ? titleCountDefault : normalized
}
function onBodyCountBlur() {
  const normalized = normalizeCandidateCount(form.bodyCount)
  form.bodyCount = normalized === null ? bodyCountDefault : normalized
}
// 仅在数值非空且越界时算 invalid，避免输入过程显示提示
const titleLimitValid = computed(() => {
  if (form.titleMaxChars === '' || form.titleMaxChars === null) return true
  const value = Number(form.titleMaxChars)
  return Number.isFinite(value) && value >= titleMin && value <= titleMax
})
const bodyLimitValid = computed(() => {
  if (form.bodyMaxChars === '' || form.bodyMaxChars === null) return true
  const value = Number(form.bodyMaxChars)
  return Number.isFinite(value) && value >= bodyMin && value <= bodyMax
})
const titleCountValid = computed(() => {
  if (form.titleCount === '' || form.titleCount === null) return true
  const value = Number(form.titleCount)
  return Number.isFinite(value) && value >= candidateCountMin && value <= candidateCountMax
})
const bodyCountValid = computed(() => {
  if (form.bodyCount === '' || form.bodyCount === null) return true
  const value = Number(form.bodyCount)
  return Number.isFinite(value) && value >= candidateCountMin && value <= candidateCountMax
})

const resultTitleMax = computed(() => {
  if (!result.value) return form.titleMaxChars || 30
  const fromResponse = Number(result.value.title_max_chars)
  return Number.isFinite(fromResponse) && fromResponse > 0 ? fromResponse : (form.titleMaxChars || 30)
})
const resultBodyMax = computed(() => {
  if (!result.value) return form.bodyMaxChars || 1000
  const fromResponse = Number(result.value.body_max_chars)
  return Number.isFinite(fromResponse) && fromResponse > 0 ? fromResponse : (form.bodyMaxChars || 1000)
})
const resultTitles = computed(() => {
  if (!result.value) return []
  return result.value.titles?.length ? result.value.titles : [result.value.title]
})
const resultBodies = computed(() => {
  if (!result.value) return []
  return result.value.bodies?.length ? result.value.bodies : [result.value.body]
})
const selectedTitle = computed(() => resultTitles.value[selectedTitleIndex.value] || resultTitles.value[0] || '')
const selectedBody = computed(() => resultBodies.value[selectedBodyIndex.value] || resultBodies.value[0] || '')
function countHanCharacters(text) {
  return (String(text || '').match(hanCharacterPattern) || []).length
}

function resetEditState() {
  editingTitleIndex.value = null
  editingBodyIndex.value = null
  titleEditValue.value = ''
  bodyEditValue.value = ''
}

function updateResultCandidate(kind, index, value) {
  if (!result.value) return false
  const isTitle = kind === 'title'
  const values = (isTitle ? resultTitles.value : resultBodies.value).slice()
  if (!values[index]) return false
  values[index] = value
  if (isTitle) {
    result.value.titles = values
    result.value.title = values[0] || ''
  } else {
    result.value.bodies = values
    result.value.body = values[0] || ''
  }
  return true
}

function beginEditTitle(index) {
  selectedTitleIndex.value = index
  editingTitleIndex.value = index
  titleEditValue.value = resultTitles.value[index] || ''
}

function beginEditBody(index) {
  selectedBodyIndex.value = index
  editingBodyIndex.value = index
  bodyEditValue.value = resultBodies.value[index] || ''
}

function cancelEditTitle() {
  editingTitleIndex.value = null
  titleEditValue.value = ''
}

function cancelEditBody() {
  editingBodyIndex.value = null
  bodyEditValue.value = ''
}

function saveEditedTitle(index) {
  const value = titleEditValue.value.trim()
  if (!value) {
    error.value = '标题不能为空'
    return false
  }
  if (!updateResultCandidate('title', index, value)) return false
  selectedTitleIndex.value = index
  cancelEditTitle()
  error.value = ''
  return true
}

function saveEditedBody(index) {
  const value = bodyEditValue.value.trim()
  if (!value) {
    error.value = '文案不能为空'
    return false
  }
  if (!updateResultCandidate('body', index, value)) return false
  selectedBodyIndex.value = index
  cancelEditBody()
  error.value = ''
  return true
}

function saveActiveEdits() {
  if (editingTitleIndex.value !== null && !saveEditedTitle(editingTitleIndex.value)) return false
  if (editingBodyIndex.value !== null && !saveEditedBody(editingBodyIndex.value)) return false
  return true
}

const canGenerate = computed(() => (
  (isManualSellingPointMode.value
    ? manualSellingPoint.value.length > 0
    : Boolean(sellingPointCatalog.value)
      && productIdentifiers.value.length >= 1
      && productIdentifiers.value.length <= 20
      && missingProductIdentifiers.value.length === 0)
  && productUrls.value.length <= 20
  && titleLimitValid.value
  && bodyLimitValid.value
  && titleCountValid.value
  && bodyCountValid.value
  && !uploadingSellingPoints.value
  && !readingProduct.value
  && !generating.value
  && !loadingOptions.value
))

function clearFeedback() {
  error.value = ''
  success.value = ''
  copiedField.value = ''
  window.clearTimeout(successTimer)
}

function switchSellingPointInputMode(mode) {
  if (mode === form.sellingPointInputMode || generating.value || uploadingSellingPoints.value) return
  form.sellingPointInputMode = mode
  result.value = null
  resetEditState()
  clearFeedback()
}

function showSuccess(message) {
  window.clearTimeout(successTimer)
  success.value = message
  successTimer = window.setTimeout(() => { success.value = '' }, 2000)
}

function clearAll() {
  if (generating.value || readingProduct.value || uploadingSellingPoints.value) return
  const catalogId = sellingPointCatalog.value?.catalog_id
  if (catalogId) api.deleteSellingPointCatalog(catalogId).catch(() => {})
  Object.assign(form, createDefaultForm())
  sellingPointCatalog.value = null
  productReferences.value = []
  result.value = null
  resetEditState()
  error.value = ''
  copiedField.value = ''
  window.clearTimeout(copyTimer)
  window.clearTimeout(successTimer)
  if (sellingPointFileInput.value) sellingPointFileInput.value.value = ''
  clearAiCopyDraft(props.userId)
  clearSellingPointWorkbook(props.userId).catch(() => {})
}

function chooseSellingPointFile() {
  if (!uploadingSellingPoints.value) sellingPointFileInput.value?.click()
}

async function uploadSellingPointFile(file, { persist = true, preserveResult = false } = {}) {
  if (!file) return
  clearFeedback()
  uploadingSellingPoints.value = true
  try {
    const uploaded = await api.uploadSellingPoints(file)
    const previousCatalogId = sellingPointCatalog.value?.catalog_id
    sellingPointCatalog.value = uploaded
    if (!preserveResult) {
      result.value = null
      resetEditState()
    }
    if (persist) await saveSellingPointWorkbook(file, props.userId)
    if (previousCatalogId && previousCatalogId !== uploaded.catalog_id) {
      api.deleteSellingPointCatalog(previousCatalogId).catch(() => {})
    }
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    uploadingSellingPoints.value = false
  }
}

async function uploadSellingPointCatalog(event) {
  try {
    await uploadSellingPointFile(event.target.files?.[0])
  } finally {
    event.target.value = ''
  }
}

function clearSellingPointCatalog() {
  if (uploadingSellingPoints.value || generating.value) return
  const catalogId = sellingPointCatalog.value?.catalog_id
  if (catalogId) api.deleteSellingPointCatalog(catalogId).catch(() => {})
  sellingPointCatalog.value = null
  result.value = null
  resetEditState()
  error.value = ''
  clearSellingPointWorkbook(props.userId).catch(() => {})
  if (sellingPointFileInput.value) sellingPointFileInput.value.value = ''
}

function invalidateProductReferences() {
  productReferences.value = []
}

function clearProductLinks() {
  if (readingProduct.value) return
  form.productUrls = ''
  productReferences.value = []
  error.value = ''
}

async function loadOptions() {
  loadingOptions.value = true
  try {
    options.value = await api.options()
  } catch (requestError) {
    error.value = `无法加载 AI 文案配置：${requestError.message}`
  } finally {
    loadingOptions.value = false
  }
}

async function inspectProducts() {
  clearFeedback()
  if (!productUrls.value.length) {
    error.value = '请至少粘贴一个商品链接'
    return
  }
  if (productUrls.value.length > 20) {
    error.value = '一次最多支持 20 个商品链接'
    return
  }
  readingProduct.value = true
  try {
    productReferences.value = await api.inspectProducts({
      product_urls: productUrls.value,
    })
  } catch (requestError) {
    productReferences.value = []
    error.value = requestError.message
  } finally {
    readingProduct.value = false
  }
}

async function generateCopy() {
  clearFeedback()
  if (isManualSellingPointMode.value) {
    if (!manualSellingPoint.value) {
      error.value = '请填写商品核心卖点'
      return
    }
  } else {
    if (!sellingPointCatalog.value) {
      error.value = '请先上传商品核心卖点 Excel'
      return
    }
    if (!productIdentifiers.value.length) {
      error.value = '请至少输入一个商品 ID 或货号'
      return
    }
    if (productIdentifiers.value.length > 20) {
      error.value = '一次最多支持 20 个商品 ID 或货号'
      return
    }
  }
  if (productUrls.value.length > 20) {
    error.value = '一次最多支持 20 个商品链接'
    return
  }
  if (!isManualSellingPointMode.value && missingProductIdentifiers.value.length) {
    error.value = `Excel 中未找到：${missingProductIdentifiers.value.join('、')}`
    return
  }
  if (!titleLimitValid.value) {
    error.value = `标题目标字数必须在 ${titleMin}-${titleMax} 之间`
    return
  }
  if (!bodyLimitValid.value) {
    error.value = `文案目标字数必须在 ${bodyMin}-${bodyMax} 之间`
    return
  }
  if (!titleCountValid.value) {
    error.value = `标题生成个数必须在 ${candidateCountMin}-${candidateCountMax} 之间`
    return
  }
  if (!bodyCountValid.value) {
    error.value = `文案生成个数必须在 ${candidateCountMin}-${candidateCountMax} 之间`
    return
  }
  result.value = null
  resetEditState()
  generating.value = true
  const titleLimit = form.titleMaxChars === '' || form.titleMaxChars === null
    ? null
    : Number(form.titleMaxChars)
  const bodyLimit = form.bodyMaxChars === '' || form.bodyMaxChars === null
    ? null
    : Number(form.bodyMaxChars)
  const titleCount = form.titleCount === '' || form.titleCount === null
    ? titleCountDefault
    : Number(form.titleCount)
  const bodyCount = form.bodyCount === '' || form.bodyCount === null
    ? bodyCountDefault
    : Number(form.bodyCount)
  try {
    const response = await api.generate({
      selling_point_input_mode: form.sellingPointInputMode,
      selling_point_catalog_id: isManualSellingPointMode.value ? null : sellingPointCatalog.value.catalog_id,
      product_identifiers: isManualSellingPointMode.value ? [] : productIdentifiers.value,
      manual_selling_point: isManualSellingPointMode.value ? manualSellingPoint.value : null,
      style: hasCustomStyle.value ? null : form.style,
      scene: form.customScene.trim() ? null : form.scene,
      festival: form.customFestival.trim() ? null : (form.festival.trim() || null),
      custom_style: form.customStyle.trim() || null,
      custom_scene: form.customScene.trim() || null,
      custom_festival: form.customFestival.trim() || null,
      product_urls: productUrls.value,
      title_max_chars: titleLimit,
      body_max_chars: bodyLimit,
      title_count: titleCount,
      body_count: bodyCount,
    })
    result.value = response
    selectedTitleIndex.value = 0
    selectedBodyIndex.value = 0
    resetEditState()
    productReferences.value = response.product_references || []
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    generating.value = false
  }
}

async function copyText(field, value) {
  try {
    await navigator.clipboard.writeText(value)
    copiedField.value = field
    window.clearTimeout(copyTimer)
    copyTimer = window.setTimeout(() => { copiedField.value = '' }, 1800)
  } catch {
    error.value = '浏览器未授予剪贴板权限，请手动选择文字复制'
  }
}

function importToWorkbench() {
  if (!saveActiveEdits()) return
  if (!result.value || !selectedTitle.value || !selectedBody.value) return
  workbenchImportOpen.value = true
}

function confirmWorkbenchImport(target) {
  if (!saveActiveEdits()) return
  if (!result.value || !selectedTitle.value || !selectedBody.value) return
  emit('import-to-workbench', {
    title: selectedTitle.value,
    body: selectedBody.value,
    platform: target.platform,
    contentType: target.contentType,
  })
  workbenchImportOpen.value = false
}

function chooseBatchExcelFile() {
  if (!saveActiveEdits()) return
  if (!result.value) {
    error.value = '请先生成文案，再导入到批量发布 Excel'
    return
  }
  if (isManualSellingPointMode.value) {
    error.value = '直接输入卖点模式没有商品 ID，无法按商品 ID 导入批量发布表格'
    return
  }
  if (importingToBatchExcel.value) return
  if (downloadBatchExcelCopy.value) {
    batchExcelFileInput.value?.click()
    return
  }
  openAndImportBatchExcel()
}

// 默认路径：通过 File System Access API 将结果写回用户选择的原文件。
async function openAndImportBatchExcel() {
  let handles
  try {
    handles = await window.showOpenFilePicker({
      multiple: false,
      types: [{
        description: 'Excel 工作簿',
        accept: {
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
        },
      }],
    })
  } catch (requestError) {
    if (requestError?.name === 'AbortError') return
    error.value = `无法打开文件选择器：${requestError?.message || requestError}`
    return
  }
  const fileHandle = handles[0]
  try {
    const permission = await fileHandle.requestPermission({ mode: 'readwrite' })
    if (permission !== 'granted') {
      error.value = '需要文件读写权限才能直接修改该 Excel'
      return
    }
  } catch {
    error.value = '无法获取文件读写权限，已取消导入'
    return
  }
  await importToBatchExcelByHandle(fileHandle)
}

async function importToBatchExcelByHandle(fileHandle) {
  if (!saveActiveEdits()) return
  const file = await fileHandle.getFile()
  if (!file.name.toLowerCase().endsWith('.xlsx')) {
    error.value = '请选择 .xlsx 格式的批量发布表格'
    return
  }
  if (!result.value || !selectedTitle.value || !selectedBody.value) return
  clearFeedback()
  importingToBatchExcel.value = true
  try {
    const response = await api.importToBatchExcel(
      file,
      selectedTitle.value,
      selectedBody.value,
      productIdentifiers.value.join(','),
    )
    const summaryText = importSummaryText(response)
    const writable = await fileHandle.createWritable()
    await writable.write(await response.arrayBuffer())
    await writable.close()
    showSuccess(`✅ 已更新原文件「${file.name}」${summaryText ? `（${summaryText}）` : ''}。若 WPS/Excel 正打开该文件，请关闭且不要保存旧窗口后重新打开。`)
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    importingToBatchExcel.value = false
  }
}

function importSummaryText(response) {
  const summaryHeader = response.headers.get('X-Import-Summary') || ''
  const matched = summaryHeader.match(/updated=(\d+)/)
  const created = summaryHeader.match(/created=(\d+)/)
  return matched || created
    ? `已更新 ${matched ? matched[1] : 0} 行，新建 ${created ? created[1] : 0} 行`
    : ''
}

// 可选路径：下载独立结果文件，不修改正在打开的原文件。
async function importToBatchExcel(event) {
  if (!saveActiveEdits()) {
    event.target.value = ''
    return
  }
  const file = event.target.files?.[0]
  if (!file) return
  if (!result.value) {
    error.value = '请先生成文案，再导入到批量发布 Excel'
    return
  }
  clearFeedback()
  importingToBatchExcel.value = true
  try {
    const response = await api.importToBatchExcel(
      file,
      selectedTitle.value,
      selectedBody.value,
      productIdentifiers.value.join(','),
    )
    const summaryText = importSummaryText(response)
    const blob = await response.blob()
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `batch_imported_${file.name}`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
    showSuccess(`✅ 已生成「batch_imported_${file.name}」${summaryText ? `（${summaryText}）` : ''}。原文件未被修改，可在 WPS/Excel 打开时安全下载；请打开该新文件查看结果。`)
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    importingToBatchExcel.value = false
    event.target.value = ''
  }
}

function restoreAiCopyDraft() {
  const draft = readAiCopyDraft(props.userId)
  if (!draft) return
  if (draft.form && typeof draft.form === 'object') {
    for (const key of Object.keys(createDefaultForm())) {
      if (Object.hasOwn(draft.form, key)) form[key] = draft.form[key]
    }
  }
  if (draft.result && typeof draft.result === 'object') result.value = draft.result
  if (Array.isArray(draft.productReferences)) productReferences.value = draft.productReferences
  if (Number.isInteger(draft.selectedTitleIndex)) selectedTitleIndex.value = draft.selectedTitleIndex
  if (Number.isInteger(draft.selectedBodyIndex)) selectedBodyIndex.value = draft.selectedBodyIndex
}

async function restoreSellingPointWorkbook() {
  try {
    const file = await loadSellingPointWorkbook(props.userId)
    if (file) await uploadSellingPointFile(file, { persist: false, preserveResult: true })
  } catch (restoreError) {
    error.value = `恢复本机卖点 Excel 失败：${restoreError.message}`
  }
}

function persistAiCopyDraft() {
  if (restoringDraft.value) return
  window.clearTimeout(draftSaveTimer)
  draftSaveTimer = window.setTimeout(() => {
    saveAiCopyDraft({ version: 1, form: { ...form }, result: result.value, productReferences: productReferences.value,
      selectedTitleIndex: selectedTitleIndex.value, selectedBodyIndex: selectedBodyIndex.value, savedAt: new Date().toISOString() }, props.userId)
  }, 180)
}

onMounted(async () => {
  restoreAiCopyDraft()
  await loadOptions()
  await restoreSellingPointWorkbook()
  restoringDraft.value = false
})
watch(() => props.active, (active) => {
  if (active) loadOptions()
})
watch([form, result, productReferences, selectedTitleIndex, selectedBodyIndex], persistAiCopyDraft, { deep: true })
</script>

<template>
  <section class="ai-copy-layout">
    <form class="ai-copy-card ai-copy-composer" @submit.prevent="generateCopy">
      <div class="ai-copy-intro">
        <div>
          <p>01 / CREATIVE BRIEF</p>
          <h2>把卖点交给AI</h2>
        </div>
        <span class="ai-copy-model" :class="{ offline: !options.llm.ready }">
          {{ options.llm.ready ? `${options.llm.provider} · ${options.llm.model}` : 'LLM 待配置' }}
        </span>
      </div>

      <p v-if="!loadingOptions && !options.llm.ready" class="ai-copy-warning">
        尚未激活 LLM。页面可正常填写与读取商品，生成前请前往左侧“LLM 适配器”选择模型并填写 API Key。
      </p>
      <p v-if="error" class="ai-copy-error" role="alert">{{ error }}</p>
      <p v-if="success" class="ai-copy-success">{{ success }}</p>

      <section class="ai-copy-selling-points">
        <div class="ai-copy-selling-point-header">
          <strong>商品核心卖点</strong>
          <div class="ai-copy-selling-point-toggle" role="group" aria-label="商品核心卖点输入方式">
            <button
              :class="{ active: !isManualSellingPointMode }"
              type="button"
              @click="switchSellingPointInputMode('excel')"
            >Excel 匹配</button>
            <button
              :class="{ active: isManualSellingPointMode }"
              type="button"
              @click="switchSellingPointInputMode('manual')"
            >直接输入</button>
          </div>
        </div>

        <label v-if="!isManualSellingPointMode" class="ai-copy-field ai-copy-identifier-field">
          <span>
            <strong>商品 ID / 货号</strong>
            <small>已输入 {{ productIdentifiers.length }} / 20</small>
          </span>
          <textarea
            v-model="form.productIdentifiers"
            rows="3"
            maxlength="2000"
            required
            placeholder="每行输入一个商品 ID 或货号"
          />
        </label>

        <label v-else class="ai-copy-field ai-copy-manual-selling-point-field">
          <span>
            <strong>直接输入核心卖点</strong>
            <small>{{ manualSellingPoint.length }} / 2000</small>
          </span>
          <textarea
            v-model="form.manualSellingPoint"
            rows="6"
            maxlength="2000"
            required
            placeholder="直接填写希望模型重点表达的商品核心卖点，可分行输入"
          />
        </label>

        <div v-if="!isManualSellingPointMode" class="ai-copy-catalog-upload">
          <input
            ref="sellingPointFileInput"
            class="ai-copy-file-input"
            type="file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            @change="uploadSellingPointCatalog"
          />
          <div>
            <b>SELLING POINT LIBRARY</b>
            <strong>上传商品核心卖点 Excel</strong>
          </div>
          <button :disabled="uploadingSellingPoints" type="button" @click="chooseSellingPointFile">
            {{ uploadingSellingPoints ? '解析中…' : sellingPointCatalog ? '更换表格' : '选择 Excel' }}
          </button>
          <a
            class="ai-copy-template-link"
            :href="`${apiBase}/api/ai-copy/selling-point-template`"
            download
          >
            下载 Excel 模板
          </a>
        </div>

        <article v-if="!isManualSellingPointMode && sellingPointCatalog" class="ai-copy-catalog-status">
          <span>已读取</span>
          <div>
            <strong>{{ sellingPointCatalog.filename }}</strong>
            <small>{{ sellingPointCatalog.row_count }} 条唯一商品卖点；本机已保存副本，刷新后会自动恢复</small>
          </div>
          <button type="button" @click="clearSellingPointCatalog">移除</button>
        </article>

        <p v-if="!isManualSellingPointMode && productIdentifiers.length > 20" class="ai-copy-match-error">
          一次最多支持 20 个商品 ID 或货号。
        </p>
        <p v-else-if="!isManualSellingPointMode && sellingPointCatalog && missingProductIdentifiers.length" class="ai-copy-match-error">
          Excel 中未找到：{{ missingProductIdentifiers.join('、') }}
        </p>

        <div v-if="!isManualSellingPointMode && matchedSellingPoints.length" class="ai-copy-selling-point-preview">
          <div>
            <strong>已匹配 {{ matchedSellingPoints.length }} 条核心卖点</strong>
            <small>将作为本次标题与正文的重要参考</small>
          </div>
          <ol>
            <li v-for="entry in matchedSellingPoints" :key="entry.identifier">
              <b>{{ entry.identifier }}</b>
              <span>{{ entry.selling_point }}</span>
            </li>
          </ol>
        </div>
      </section>

      <fieldset class="ai-copy-choice-group ai-copy-limits">
        <legend><b>目标汉字数</b></legend>

        <div class="ai-copy-limit-row">
          <span class="ai-copy-limit-label"><b>标题字数</b></span>
          <div class="ai-copy-limit-chips">
            <button
              v-for="preset in titleLimitPresets"
              :key="`title-preset-${preset}`"
              type="button"
              :class="{ active: form.titleMaxChars === preset }"
              @click="pickTitleLimit(preset)"
            >{{ preset }} 字</button>
          </div>
          <label class="ai-copy-limit-custom">
            <span>自定义</span>
            <input
              type="text"
              inputmode="numeric"
              pattern="[0-9]*"
              maxlength="3"
              placeholder="自定义"
              :value="form.titleMaxChars"
              @input="onTitleLimitInput"
              @blur="onTitleLimitBlur"
            />
            <small v-if="!titleLimitValid">需为 {{ titleMin }}-{{ titleMax }} 之间的整数</small>
          </label>
        </div>

        <div class="ai-copy-limit-row">
          <span class="ai-copy-limit-label"><b>文案字数</b></span>
          <div class="ai-copy-limit-chips">
            <button
              v-for="preset in bodyLimitPresets"
              :key="`body-preset-${preset}`"
              type="button"
              :class="{ active: form.bodyMaxChars === preset }"
              @click="pickBodyLimit(preset)"
            >{{ preset }} 字</button>
          </div>
          <label class="ai-copy-limit-custom">
            <span>自定义</span>
            <input
              type="text"
              inputmode="numeric"
              pattern="[0-9]*"
              maxlength="4"
              placeholder="自定义"
              :value="form.bodyMaxChars"
              @input="onBodyLimitInput"
              @blur="onBodyLimitBlur"
            />
            <small v-if="!bodyLimitValid">需为 {{ bodyMin }}-{{ bodyMax }} 之间的整数</small>
          </label>
        </div>
      </fieldset>

      <fieldset class="ai-copy-choice-group ai-copy-limits ai-copy-counts">
        <legend><b>生成个数</b></legend>

        <div class="ai-copy-limit-row">
          <span class="ai-copy-limit-label"><b>标题个数</b></span>
          <div class="ai-copy-limit-chips">
            <button
              v-for="preset in titleCountPresets"
              :key="`title-count-${preset}`"
              type="button"
              :class="{ active: form.titleCount === preset }"
              @click="pickTitleCount(preset)"
            >{{ preset }} 条</button>
          </div>
          <label class="ai-copy-limit-custom">
            <span>自定义</span>
            <input
              type="text"
              inputmode="numeric"
              pattern="[0-9]*"
              maxlength="2"
              placeholder="自定义"
              :value="form.titleCount"
              @input="onTitleCountInput"
              @blur="onTitleCountBlur"
            />
            <small v-if="!titleCountValid">需为 {{ candidateCountMin }}-{{ candidateCountMax }} 之间的整数</small>
          </label>
        </div>

        <div class="ai-copy-limit-row">
          <span class="ai-copy-limit-label"><b>文案个数</b></span>
          <div class="ai-copy-limit-chips">
            <button
              v-for="preset in bodyCountPresets"
              :key="`body-count-${preset}`"
              type="button"
              :class="{ active: form.bodyCount === preset }"
              @click="pickBodyCount(preset)"
            >{{ preset }} 条</button>
          </div>
          <label class="ai-copy-limit-custom">
            <span>自定义</span>
            <input
              type="text"
              inputmode="numeric"
              pattern="[0-9]*"
              maxlength="2"
              placeholder="自定义"
              :value="form.bodyCount"
              @input="onBodyCountInput"
              @blur="onBodyCountBlur"
            />
            <small v-if="!bodyCountValid">需为 {{ candidateCountMin }}-{{ candidateCountMax }} 之间的整数</small>
          </label>
        </div>
      </fieldset>

      <fieldset class="ai-copy-choice-group">
        <legend><b>文案风格</b></legend>
        <div class="ai-copy-style-grid">
          <label
            v-for="item in options.styles"
            :key="item.value"
            :class="{ selected: !hasCustomStyle && form.style === item.value }"
          >
            <input
              :checked="!hasCustomStyle && form.style === item.value"
              type="radio"
              :value="item.value"
              @change="form.style = item.value; form.customStyle = ''"
            />
            <strong>{{ item.label }}</strong>
          </label>
        </div>
        <label class="ai-copy-field ai-copy-custom-context">
          <span><strong>自定义文案风格</strong></span>
          <input v-model="form.customStyle" maxlength="100" placeholder="请输入自定义文案风格" />
        </label>
      </fieldset>

      <div class="ai-copy-split-fields">
        <label class="ai-copy-field">
          <span><strong>内容场景</strong><small>预设或自定义</small></span>
          <AiCopyDropdown
            v-model="selectedScene"
            aria-label="内容场景"
            :options="options.scenes"
            placeholder="自定义场景已启用"
          />
          <input v-model="form.customScene" maxlength="100" placeholder="请输入自定义内容场景" />
        </label>
        <label class="ai-copy-field">
          <span><strong>节日氛围</strong><small>预设或自定义</small></span>
          <AiCopyDropdown
            v-model="selectedFestival"
            aria-label="节日氛围"
            :options="festivalOptions"
            placeholder="自定义主题已启用"
          />
          <input v-model="form.customFestival" maxlength="80" placeholder="请输入自定义节日或主题" />
        </label>
      </div>

      <section class="ai-copy-product-panel">
        <div class="ai-copy-product-heading">
          <div><p>PRODUCT REFERENCES</p><h3>多商品链接参考</h3></div>
          <span>{{ productUrls.length ? `${productUrls.length} / 20` : '可选' }}</span>
        </div>
        <p class="ai-copy-product-help">每行粘贴一个天猫或京东商品链接，最多 20 个。生成时会逐条读取商品资料，再综合所有商品信息写文案。</p>
        <div class="ai-copy-link-row">
          <textarea
            v-model="form.productUrls"
            rows="4"
            maxlength="20000"
            spellcheck="false"
            placeholder="每行一个天猫或京东商品链接&#10;https://detail.tmall.com/item.htm?id=...&#10;https://item.jd.com/....html"
            @input="invalidateProductReferences"
          />
          <button
            :disabled="readingProduct || !productUrls.length || productUrls.length > 20"
            type="button"
            @click="inspectProducts"
          >
            {{ readingProduct ? '逐条读取中…' : productUrls.length ? `读取 ${productUrls.length} 个链接` : '读取链接' }}
          </button>
          <button
            v-if="productUrls.length || productReferences.length"
            class="ai-copy-delete-link"
            :disabled="readingProduct"
            type="button"
            @click="clearProductLinks"
          >清空链接</button>
        </div>

        <p v-if="productUrls.length > 20" class="ai-copy-link-error">
          一次最多支持 20 个商品链接，当前已输入 {{ productUrls.length }} 个。
        </p>

        <div v-if="productReferences.length" class="ai-copy-reference-list">
          <article
            v-for="(reference, index) in productReferences"
            :key="reference.source_url"
            class="ai-copy-reference"
          >
            <span>已读取 {{ index + 1 }}</span>
            <div>
              <strong>{{ reference.title }}</strong>
              <small>{{ reference.source_url }}</small>
              <p>{{ reference.summary }}</p>
            </div>
            <dl v-if="Object.keys(reference.attributes).length">
              <template v-for="(value, key) in reference.attributes" :key="key">
                <dt>{{ key }}</dt><dd>{{ value }}</dd>
              </template>
            </dl>
          </article>
        </div>
      </section>

      <button class="ai-copy-generate" :disabled="!canGenerate" type="submit">
        <span>{{ generating ? '正在组织标题与文案…' : '生成标题与文案' }}</span>
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 12h13M14 7l5 5-5 5" /></svg>
      </button>
    </form>

    <aside class="ai-copy-results">
      <div class="ai-copy-result-head">
        <div><p>02 / GENERATED COPY</p><h2>生成结果</h2></div>
        <div class="ai-copy-result-actions">
          <span v-if="result" class="ai-copy-ready">READY</span>
          <button
            class="ai-copy-clear"
            :disabled="generating || readingProduct || uploadingSellingPoints"
            type="button"
            @click="clearAll"
          >一键清空</button>
        </div>
      </div>

      <div v-if="generating" class="ai-copy-generating" aria-live="polite">
        <span></span><span></span><span></span>
        <p>{{ productUrls.length ? `正在读取并综合 ${productUrls.length} 个商品资料` : '正在根据已匹配的核心卖点构思文案' }}</p>
      </div>

      <template v-else-if="result">
        <section class="ai-copy-output-group">
          <div class="ai-copy-candidate-heading">
            <span>标题候选</span><small>已生成 {{ resultTitles.length }} 条；请选择 1 条用于导入</small>
          </div>
          <article
            v-for="(title, index) in resultTitles"
            :key="`title-${index}`"
            :class="['ai-copy-output', 'ai-copy-title-output', { selected: selectedTitleIndex === index }]"
          >
            <div class="ai-copy-output-label">
              <span>标题{{ resultTitles.length > 1 ? ` ${index + 1}` : '' }}</span>
              <small>{{ countHanCharacters(title) }} 汉字 · 目标 {{ resultTitleMax }} 汉字</small>
            </div>
            <label v-if="resultTitles.length > 1" class="ai-copy-candidate-select">
              <input type="checkbox" :checked="selectedTitleIndex === index" @change="selectedTitleIndex = index" />
              <span>选用此标题</span>
            </label>
            <template v-if="editingTitleIndex === index">
              <textarea
                v-model="titleEditValue"
                class="ai-copy-edit-textarea ai-copy-title-editor"
                maxlength="120"
                rows="2"
                @keydown.meta.enter.prevent="saveEditedTitle(index)"
                @keydown.ctrl.enter.prevent="saveEditedTitle(index)"
              />
              <div class="ai-copy-edit-meta">
                <span>{{ countHanCharacters(titleEditValue) }} 汉字 · 保存后用于复制、导入工作台和 Excel</span>
              </div>
              <div class="ai-copy-output-actions">
                <button type="button" @click="saveEditedTitle(index)">保存标题</button>
                <button class="ai-copy-secondary-action" type="button" @click="cancelEditTitle">取消</button>
              </div>
            </template>
            <template v-else>
              <h3>{{ title }}</h3>
              <div class="ai-copy-output-actions">
                <button type="button" @click="copyText(`title-${index}`, title)">
                  {{ copiedField === `title-${index}` ? '已复制' : '复制标题' }}
                </button>
                <button class="ai-copy-secondary-action" type="button" @click="beginEditTitle(index)">编辑标题</button>
              </div>
            </template>
          </article>
        </section>

        <section class="ai-copy-output-group">
          <div class="ai-copy-candidate-heading">
            <span>正文文案候选</span><small>已生成 {{ resultBodies.length }} 条；请选择 1 条用于导入</small>
          </div>
          <article
            v-for="(body, index) in resultBodies"
            :key="`body-${index}`"
            :class="['ai-copy-output', 'ai-copy-body-output', { selected: selectedBodyIndex === index }]"
          >
            <div class="ai-copy-output-label">
              <span>正文文案{{ resultBodies.length > 1 ? ` ${index + 1}` : '' }}</span>
              <small>{{ countHanCharacters(body) }} 汉字 · 目标 {{ resultBodyMax }} 汉字</small>
            </div>
            <label v-if="resultBodies.length > 1" class="ai-copy-candidate-select">
              <input type="checkbox" :checked="selectedBodyIndex === index" @change="selectedBodyIndex = index" />
              <span>选用此文案</span>
            </label>
            <template v-if="editingBodyIndex === index">
              <textarea
                v-model="bodyEditValue"
                class="ai-copy-edit-textarea ai-copy-body-editor"
                maxlength="1600"
                rows="6"
                @keydown.meta.enter.prevent="saveEditedBody(index)"
                @keydown.ctrl.enter.prevent="saveEditedBody(index)"
              />
              <div class="ai-copy-edit-meta">
                <span>{{ countHanCharacters(bodyEditValue) }} 汉字 · 保存后用于复制、导入工作台和 Excel</span>
              </div>
              <div class="ai-copy-output-actions">
                <button type="button" @click="saveEditedBody(index)">保存文案</button>
                <button class="ai-copy-secondary-action" type="button" @click="cancelEditBody">取消</button>
              </div>
            </template>
            <template v-else>
              <p>{{ body }}</p>
              <div class="ai-copy-output-actions">
                <button type="button" @click="copyText(`body-${index}`, body)">
                  {{ copiedField === `body-${index}` ? '已复制' : '复制文案' }}
                </button>
                <button class="ai-copy-secondary-action" type="button" @click="beginEditBody(index)">编辑文案</button>
              </div>
            </template>
          </article>
        </section>

        <button class="ai-copy-import" type="button" @click="importToWorkbench">
          <span>
            <small>IMPORT TO WORKBENCH</small>
            <strong>导入发布工作台</strong>
            <em>选择后直接导入</em>
          </span>
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M5 12h13M14 7l5 5-5 5" />
          </svg>
        </button>

        <WorkbenchImportDialog
          :open="workbenchImportOpen"
          @close="workbenchImportOpen = false"
          @confirm="confirmWorkbenchImport"
        />

        <!-- 隐藏的文件输入：用于选择批量发布 Excel -->
        <input
          ref="batchExcelFileInput"
          class="ai-copy-file-input"
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          @change="importToBatchExcel"
        />

        <button
          class="ai-copy-import-batch"
          type="button"
          :disabled="!result || importingToBatchExcel || isManualSellingPointMode"
          @click="chooseBatchExcelFile"
        >
          <span>
            <small>IMPORT TO BATCH EXCEL</small>
            <strong>{{ importingToBatchExcel ? '正在处理…' : '导入批量发布表格' }}</strong>
            <em>{{ isManualSellingPointMode ? '直接输入模式没有商品 ID，不能导入批量发布表格' : downloadBatchExcelCopy ? '下载独立导入结果，不覆盖原文件' : '直接填入原文件；请先关闭 WPS/Excel 中打开的该文件' }}</em>
          </span>
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
            <polyline points="14,2 14,8 20,8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
            <line x1="12" y1="18" x2="12" y2="12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
            <polyline points="9,15 12,18 15,15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>

        <label class="ai-copy-download-option">
          <input v-model="downloadBatchExcelCopy" type="checkbox" />
          <span>下载为新文件（保留原文件）</span>
        </label>

        <p class="ai-copy-result-meta">
          由 {{ result.provider }} · {{ result.model }} 生成
          <span> · 已引用 {{ result.selling_point_references.length }} 条核心卖点</span>
          <span v-if="result.product_references.length"> · 已引用 {{ result.product_references.length }} 个商品资料</span>
        </p>
      </template>

      <div v-else class="ai-copy-empty">
        <span>AI</span>
        <h3>匹配商品卖点，或直接交给 AI</h3>
        <p>可上传核心卖点 Excel 后按商品 ID 精准匹配，也可切换为直接输入核心卖点。</p>
        <ol><li>标题与正文字数可在「目标字数」中预设或自定义</li><li>不虚构商品信息与促销承诺</li></ol>
      </div>
    </aside>
  </section>
</template>

<style src="./ai-copy.css"></style>
