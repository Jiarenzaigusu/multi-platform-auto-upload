<script setup>
import { computed, onMounted, ref } from 'vue'

import { createLlmAdapterApi } from './api.js'

const api = createLlmAdapterApi()
const status = ref({ adapters: [], active: null })
const selectedProvider = ref('')
const apiKey = ref('')
const qwenBaseUrl = ref('')
const loading = ref(true)
const saving = ref(false)
const clearing = ref(false)
const deletingProvider = ref('')
const showKey = ref(false)
const error = ref('')
const notice = ref('')

const selectedAdapter = computed(() => (
  status.value.adapters.find((item) => item.provider === selectedProvider.value) || null
))
const isQwen = computed(() => selectedProvider.value === 'qwen')
const hasNewKey = computed(() => apiKey.value.trim().length > 0)
const canActivate = computed(() => (
  selectedAdapter.value
  && (hasNewKey.value ? apiKey.value.trim().length >= 8 : selectedAdapter.value.configured)
  && (!isQwen.value || qwenBaseUrl.value.trim().length > 0)
  && !saving.value
  && !clearing.value
  && !deletingProvider.value
))
const activateLabel = computed(() => {
  if (saving.value) return '正在验证连接…'
  if (!selectedAdapter.value) return '请选择模型'
  if (hasNewKey.value) return `验证、保存并启用 ${selectedAdapter.value.label}`
  if (selectedAdapter.value.configured) return `验证并启用已保存的 ${selectedAdapter.value.label}`
  return `填写并验证 ${selectedAdapter.value.label} API Key`
})

function syncSelectedEndpoint() {
  if (selectedProvider.value !== 'qwen') return
  qwenBaseUrl.value = selectedAdapter.value?.endpoint || ''
}

function selectProvider(provider) {
  selectedProvider.value = provider
  syncSelectedEndpoint()
  apiKey.value = ''
  showKey.value = false
  error.value = ''
  notice.value = ''
}

async function loadStatus() {
  loading.value = true
  error.value = ''
  try {
    status.value = await api.status()
    selectedProvider.value = status.value.active?.provider || status.value.adapters[0]?.provider || ''
    syncSelectedEndpoint()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

async function activateAdapter() {
  if (!canActivate.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    if (hasNewKey.value) {
      const payload = {
        provider: selectedProvider.value,
        api_key: apiKey.value.trim(),
      }
      if (isQwen.value) payload.base_url = qwenBaseUrl.value.trim()
      status.value = await api.activate(payload)
    } else {
      status.value = await api.activateSaved(selectedProvider.value)
    }
    syncSelectedEndpoint()
    apiKey.value = ''
    showKey.value = false
    notice.value = `${status.value.active.label} 连接验证通过，API Key 已保存并成为当前激活模型`
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    saving.value = false
  }
}

async function clearAdapter() {
  clearing.value = true
  error.value = ''
  notice.value = ''
  try {
    status.value = await api.clear()
    apiKey.value = ''
    notice.value = '已停用当前 LLM 适配器，已保存的 API Key 未删除'
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    clearing.value = false
  }
}

async function deleteSelectedCredential() {
  const adapter = selectedAdapter.value
  if (!adapter?.configured || deletingProvider.value) return
  if (!window.confirm(`确定删除 ${adapter.label} 已保存的 API Key 吗？${status.value.active?.provider === adapter.provider ? '删除后该模型会同时停用。' : ''}`)) return

  deletingProvider.value = adapter.provider
  error.value = ''
  notice.value = ''
  try {
    status.value = await api.deleteCredential(adapter.provider)
    apiKey.value = ''
    showKey.value = false
    syncSelectedEndpoint()
    notice.value = `${adapter.label} API Key 已删除`
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    deletingProvider.value = ''
  }
}

onMounted(loadStatus)
</script>

<template>
  <section class="llm-adapter-layout">
    <article class="llm-adapter-console">
      <header class="llm-adapter-heading">
        <div>
          <p>01 / ROUTING CONTROL</p>
          <h2>一次，只让一个模型在线</h2>
        </div>
        <span :class="['llm-live-indicator', { active: status.active }]">
          {{ status.active ? 'ACTIVE' : 'OFFLINE' }}
        </span>
      </header>

      <p v-if="error" class="llm-adapter-message error" role="alert">{{ error }}</p>
      <p v-if="notice" class="llm-adapter-message success" role="status">{{ notice }}</p>

      <section class="llm-current-strip">
        <span>当前路由</span>
        <div v-if="status.active">
          <strong>{{ status.active.label }}</strong>
          <small>{{ status.active.model_label }} · {{ status.active.model }}</small>
          <small class="llm-current-endpoint">{{ status.active.endpoint }}</small>
        </div>
        <div v-else>
          <strong>尚未选择模型</strong>
          <small>AI 文案生成暂不可用</small>
        </div>
        <button v-if="status.active" :disabled="clearing" type="button" @click="clearAdapter">
          {{ clearing ? '停用中…' : '停用' }}
        </button>
      </section>

      <fieldset class="llm-provider-fieldset" :disabled="loading || saving || clearing || Boolean(deletingProvider)">
        <legend><b>选择模型供应商</b><span>单选激活，各模型可分别保存 API Key</span></legend>
        <div class="llm-provider-grid">
          <label
            v-for="(adapter, index) in status.adapters"
            :key="adapter.provider"
            :class="['llm-provider-card', { selected: selectedProvider === adapter.provider, active: status.active?.provider === adapter.provider }]"
          >
            <input
              :checked="selectedProvider === adapter.provider"
              type="radio"
              name="llm-provider"
              :value="adapter.provider"
              @change="selectProvider(adapter.provider)"
            />
            <span class="llm-provider-index">0{{ index + 1 }}</span>
            <span
              v-if="adapter.configured"
              :class="['llm-provider-active', { current: status.active?.provider === adapter.provider }]"
            >{{ status.active?.provider === adapter.provider ? '当前 · 已保存' : '已保存' }}</span>
            <strong>{{ adapter.label }}</strong>
            <em>{{ adapter.model_label }}</em>
            <p>{{ adapter.description }}</p>
          </label>
        </div>
      </fieldset>

      <form class="llm-key-form" @submit.prevent="activateAdapter">
        <div class="llm-key-head">
          <div><p>02 / CREDENTIAL</p><h3>{{ selectedAdapter?.label || '模型' }} API Key</h3></div>
          <span>{{ selectedAdapter?.configured ? '本机已保存，可留空直接启用' : '验证成功后保存到本机私有目录' }}</span>
        </div>
        <label class="llm-key-input">
          <input
            v-model="apiKey"
            :aria-label="`${selectedAdapter?.label || 'LLM'} API Key`"
            :disabled="loading || !selectedAdapter"
            :type="showKey ? 'text' : 'password'"
            autocomplete="off"
            minlength="8"
            maxlength="4096"
            :placeholder="selectedAdapter?.configured ? '已保存；输入新 Key 可覆盖' : selectedAdapter?.key_hint || '请输入 API Key'"
          />
          <button :aria-label="`${showKey ? '隐藏' : '显示'} API Key`" :disabled="!apiKey" type="button" @click="showKey = !showKey">
            {{ showKey ? '隐藏' : '显示' }}
          </button>
        </label>
        <label v-if="isQwen" class="llm-endpoint-input">
          <span>OpenAI 兼容地址 <em>以密钥控制台显示的 Base URL 为准</em></span>
          <input
            v-model="qwenBaseUrl"
            aria-label="千问 OpenAI 兼容地址"
            :disabled="loading || saving || clearing || Boolean(deletingProvider)"
            inputmode="url"
            maxlength="2048"
            placeholder="https://…/compatible-mode/v1"
            type="url"
          />
          <small>控制台显示 dashscope.aliyuncs.com 时保留默认地址；只有显示 ws-…maas.aliyuncs.com 时才替换为专属地址。</small>
        </label>
        <div class="llm-key-meta">
          <span>启用前会发送一次最小请求；新 Key 仅在验证通过后保存</span>
          <span v-if="selectedAdapter">{{ selectedAdapter.model }}</span>
        </div>
        <div class="llm-credential-actions">
          <button class="llm-activate" :disabled="!canActivate" type="submit">
            <span>{{ activateLabel }}</span>
            <b aria-hidden="true">→</b>
          </button>
          <button
            v-if="selectedAdapter?.configured"
            class="llm-delete-key"
            :disabled="Boolean(deletingProvider) || saving || clearing"
            type="button"
            @click="deleteSelectedCredential"
          >{{ deletingProvider === selectedAdapter.provider ? '删除中…' : `删除 ${selectedAdapter.label} API Key` }}</button>
        </div>
      </form>
    </article>

    <aside class="llm-adapter-notes">
      <p>ADAPTER RULES</p>
      <h2>一条清楚的模型通道</h2>
      <div class="llm-route-visual">
        <span>AI COPY</span><i></i><strong>{{ status.active?.label || 'NO MODEL' }}</strong>
      </div>
      <ol>
        <li><b>01</b><span><strong>严格单选</strong>同时只激活一个模型，但每个模型可分别保存 API Key。</span></li>
        <li><b>02</b><span><strong>先验后切</strong>连接验证通过后才替换当前模型，失败不会影响原配置。</span></li>
        <li><b>03</b><span><strong>密钥不回传</strong>状态接口只显示供应商、模型与地址，不返回 API Key。</span></li>
        <li><b>04</b><span><strong>本机私有存储</strong>服务重启后自动恢复；可随时删除指定模型的 API Key。</span></li>
      </ol>
    </aside>
  </section>
</template>

<style src="./llm-adapter.css"></style>
