<script setup>
import { computed, ref } from 'vue'
import { apiFetch, apiRequest } from '../api-client.js'

const props = defineProps({
  mode: { type: String, default: 'video' },
  limit: { type: Number, default: 1 },
})
const emit = defineEmits(['selected'])

const open = ref(false)
const loading = ref(false)
const importing = ref(false)
const folders = ref([])
const assets = ref([])
const path = ref([])
const selected = ref([])
const error = ref('')
const configured = ref(false)
const connecting = ref(false)
const connection = ref({ host: '', key: '', secret: '', tenant: '', catalog: '' })

const title = computed(() => props.mode === 'video' ? '从 DAM 选择视频' : props.mode === 'cover' ? '从 DAM 选择封面' : '从 DAM 选择图片')
// DAM is the source of truth; platform-specific format validation remains in the publish form.
const filteredAssets = computed(() => assets.value)

async function show() {
  open.value = true
  error.value = ''
  selected.value = []
  path.value = []
  loading.value = true
  try {
    const status = await apiRequest('/api/dam/status')
    configured.value = status.configured
    if (!status.configured) return
    await initializeBinding(status.bindings || [], status.binding)
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function initializeBinding(bindings, preferredBinding = null) {
  const binding = preferredBinding || bindings.find((item) => (
    !connection.value.tenant || (item.tenantCode === connection.value.tenant && item.catalogCode === connection.value.catalog)
  )) || bindings[0]
  if (binding?.scopeMode === 'FOLDER_ROOTS') {
    folders.value = (binding.rootCollectionIds || []).map((id) => ({ id, name: `授权目录 #${id}`, hasChildren: true }))
  } else {
    await loadFolders(null)
  }
}

async function connectDam() {
  connecting.value = true
  error.value = ''
  try {
    const result = await apiRequest('/api/dam/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(connection.value),
    })
    configured.value = true
    path.value = []
    assets.value = []
    selected.value = []
    await initializeBinding(result.bindings || [], result.binding)
  } catch (cause) {
    error.value = cause.message
  } finally {
    connecting.value = false
  }
}

async function disconnectDam() {
  await apiRequest('/api/dam/session', { method: 'DELETE' })
  configured.value = false
  folders.value = []
  assets.value = []
  path.value = []
  selected.value = []
  connection.value.secret = ''
}

async function loadFolders(parentId) {
  const query = parentId ? `?parent_id=${encodeURIComponent(parentId)}` : ''
  const result = await apiRequest(`/api/dam/folders${query}`)
  folders.value = result.folders || []
}

async function enter(folder) {
  loading.value = true
  error.value = ''
  try {
    path.value.push({ id: folder.id, name: folder.name || `目录 #${folder.id}` })
    const [folderResult, assetResult] = await Promise.all([
      apiRequest(`/api/dam/folders?parent_id=${folder.id}`),
      apiRequest(`/api/dam/assets?folder_id=${folder.id}&page_size=100`),
    ])
    folders.value = folderResult.folders || []
    assets.value = assetResult.assets?.list || []
    selected.value = []
  } catch (cause) {
    path.value.pop()
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function goUp() {
  if (!path.value.length) return
  path.value.pop()
  assets.value = []
  selected.value = []
  const parent = path.value.at(-1)
  if (parent) {
    const [folderResult, assetResult] = await Promise.all([
      apiRequest(`/api/dam/folders?parent_id=${parent.id}`),
      apiRequest(`/api/dam/assets?folder_id=${parent.id}&page_size=100`),
    ])
    folders.value = folderResult.folders || []
    assets.value = assetResult.assets?.list || []
  } else {
    await show()
  }
}

function toggle(asset) {
  if (props.limit === 1) {
    selected.value = selected.value[0]?.id === asset.id ? [] : [asset]
    return
  }
  const exists = selected.value.some((item) => item.id === asset.id)
  selected.value = exists
    ? selected.value.filter((item) => item.id !== asset.id)
    : selected.value.length < props.limit ? [...selected.value, asset] : selected.value
}

async function importSelected() {
  if (!selected.value.length) return
  importing.value = true
  error.value = ''
  try {
    const files = []
    for (const asset of selected.value) {
      const response = await apiFetch(`/api/dam/assets/${asset.id}/download`)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'DAM 素材下载失败')
      }
      const blob = await response.blob()
      files.push(new File([blob], asset.originalFilename || asset.name || `dam-${asset.id}`, {
        type: asset.mimeType || blob.type || 'application/octet-stream',
        lastModified: Date.now(),
      }))
    }
    emit('selected', props.limit === 1 ? files[0] : files)
    open.value = false
  } catch (cause) {
    error.value = cause.message
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <button type="button" class="dam-open-button" @click="show">{{ title }}</button>
  <Teleport to="body">
    <div v-if="open" class="dam-picker-backdrop" @click.self="open = false">
      <section class="dam-picker" :class="{ 'dam-picker-login': !configured }" role="dialog" aria-modal="true" :aria-label="title">
        <header>
          <div><small>DAM OPENAPI</small><h2>{{ title }}</h2></div>
          <div class="dam-picker-header-actions"><button v-if="configured" type="button" class="quiet" @click="disconnectDam">更换连接</button><button type="button" class="quiet" @click="open = false">关闭</button></div>
        </header>
        <form v-if="!configured" class="dam-login" @submit.prevent="connectDam">
          <div><small>测试期连接</small><h3>连接 DAM OpenAPI</h3><p>凭证只保存在当前服务进程内，不写入浏览器存储或数据库。</p></div>
          <label><span>API Host</span><input v-model.trim="connection.host" required placeholder="https://.../ddc-dam-backend" /></label>
          <label><span>Key ID</span><input v-model.trim="connection.key" required autocomplete="off" placeholder="dam_xxxxxxxx" /></label>
          <label><span>Secret</span><input v-model="connection.secret" required type="password" autocomplete="new-password" /></label>
          <label><span>Tenant</span><input v-model.trim="connection.tenant" required /></label>
          <label><span>Catalog</span><input v-model.trim="connection.catalog" required /></label>
          <p v-if="error" class="form-error">{{ error }}</p>
          <button type="submit" :disabled="connecting">{{ connecting ? '正在验证…' : '验证并连接' }}</button>
        </form>
        <template v-else>
          <div class="dam-picker-path"><button type="button" :disabled="!path.length" @click="goUp">← .. 返回上一级</button><span v-for="item in path" :key="item.id">/ {{ item.name }}</span></div>
          <p v-if="error" class="form-error">{{ error }}</p>
          <div class="dam-picker-body">
            <aside><b>目录</b><button v-for="folder in folders" :key="folder.id" type="button" @click="enter(folder)"><span>{{ folder.name }}</span><small>{{ folder.assetCount ?? '' }} ›</small></button><p v-if="!folders.length && !loading">没有下一级目录</p></aside>
            <main><div class="dam-picker-assets"><button v-for="asset in filteredAssets" :key="asset.id" type="button" :class="{ selected: selected.some(item => item.id === asset.id) }" @click="toggle(asset)"><img :src="asset.thumbnailUrl || asset.quickPreviewUrl" alt="" /><b>{{ asset.originalFilename || asset.name }}</b><small>{{ asset.mimeType || '格式未知' }} · {{ asset.fileSize ? `${(asset.fileSize / 1024 / 1024).toFixed(1)} MB` : '大小未知' }}</small></button></div><p v-if="path.length && !filteredAssets.length && !loading" class="dam-picker-empty">当前目录没有素材</p><p v-if="!path.length" class="dam-picker-empty">请从左侧选择目录</p></main>
          </div>
          <footer><span>已选择 {{ selected.length }} / {{ limit }}</span><button type="button" :disabled="!selected.length || importing" @click="importSelected">{{ importing ? '正在导入…' : '使用所选素材' }}</button></footer>
        </template>
      </section>
    </div>
  </Teleport>
</template>
