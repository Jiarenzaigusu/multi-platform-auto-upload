<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { createAiCopyApi } from './api.js'
import AiCopyDropdown from './AiCopyDropdown.vue'

const props = defineProps({
  active: { type: Boolean, default: false },
})
const emit = defineEmits(['import-to-workbench'])

const api = createAiCopyApi()
const options = ref({ styles: [], scenes: [], festivals: [], llm: { ready: false, model: '', provider: '' } })
const createDefaultForm = () => ({
  contentBrief: '',
  style: 'friendly',
  scene: 'short_video',
  festival: '',
  productUrl: '',
  searchEndpoint: '',
  searchApiKey: '',
})
const form = reactive(createDefaultForm())
const loadingOptions = ref(true)
const readingProduct = ref(false)
const generating = ref(false)
const productReference = ref(null)
const result = ref(null)
const error = ref('')
const copiedField = ref('')
const searchConfigDetails = ref(null)
let copyTimer

const briefLength = computed(() => form.contentBrief.length)
const festivalOptions = computed(() => [
  { value: '', label: '不指定节日' },
  ...options.value.festivals.map((festival) => ({ value: festival, label: festival })),
])
const canGenerate = computed(() => (
  form.contentBrief.trim().length >= 2 && !generating.value && !loadingOptions.value
))

function searchConfig() {
  return {
    endpoint_url: form.searchEndpoint.trim() || null,
    api_key: form.searchApiKey.trim() || null,
  }
}

function clearFeedback() {
  error.value = ''
  copiedField.value = ''
}

function clearAll() {
  if (generating.value || readingProduct.value) return
  Object.assign(form, createDefaultForm())
  productReference.value = null
  result.value = null
  error.value = ''
  copiedField.value = ''
  window.clearTimeout(copyTimer)
  if (searchConfigDetails.value) searchConfigDetails.value.open = false
}

function clearProductLink() {
  if (readingProduct.value) return
  form.productUrl = ''
  productReference.value = null
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

async function inspectProduct() {
  clearFeedback()
  if (!form.productUrl.trim()) {
    error.value = '请先粘贴一个商品链接'
    return
  }
  readingProduct.value = true
  try {
    productReference.value = await api.inspectProduct({
      product_url: form.productUrl.trim(),
      search: searchConfig(),
    })
  } catch (requestError) {
    productReference.value = null
    error.value = requestError.message
  } finally {
    readingProduct.value = false
  }
}

async function generateCopy() {
  clearFeedback()
  result.value = null
  generating.value = true
  try {
    const response = await api.generate({
      content_brief: form.contentBrief.trim(),
      style: form.style,
      scene: form.scene,
      festival: form.festival.trim() || null,
      product_url: form.productUrl.trim() || null,
      product_search: searchConfig(),
    })
    result.value = response
    if (response.product_reference) productReference.value = response.product_reference
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
  if (!result.value) return
  emit('import-to-workbench', {
    title: result.value.title,
    body: result.value.body,
  })
}

onMounted(loadOptions)
watch(() => props.active, (active) => {
  if (active) loadOptions()
})
</script>

<template>
  <section class="ai-copy-layout">
    <form class="ai-copy-card ai-copy-composer" @submit.prevent="generateCopy">
      <div class="ai-copy-intro">
        <div>
          <p>01 / CREATIVE BRIEF</p>
          <h2>把卖点交给文字</h2>
        </div>
        <span class="ai-copy-model" :class="{ offline: !options.llm.ready }">
          {{ options.llm.ready ? `${options.llm.provider} · ${options.llm.model}` : 'LLM 待配置' }}
        </span>
      </div>

      <p v-if="!loadingOptions && !options.llm.ready" class="ai-copy-warning">
        尚未激活 LLM。页面可正常填写与读取商品，生成前请前往左侧“LLM 适配器”选择模型并填写 API Key。
      </p>
      <p v-if="error" class="ai-copy-error" role="alert">{{ error }}</p>

      <label class="ai-copy-field ai-copy-brief">
        <span><strong>内容要点</strong><small>{{ briefLength }} / 2000</small></span>
        <textarea
          v-model="form.contentBrief"
          maxlength="2000"
          minlength="2"
          required
          placeholder="例如：一双适合夏季通勤的轻量女鞋，突出透气、柔软和百搭；不要使用夸张功效词。"
        />
      </label>

      <fieldset class="ai-copy-choice-group">
        <legend><b>表达风格</b><span>选择文案的语气与节奏</span></legend>
        <div class="ai-copy-style-grid">
          <label
            v-for="(item, index) in options.styles"
            :key="item.value"
            :class="{ selected: form.style === item.value }"
          >
            <input v-model="form.style" type="radio" :value="item.value" />
            <i>0{{ index + 1 }}</i>
            <strong>{{ item.label }}</strong>
          </label>
        </div>
      </fieldset>

      <div class="ai-copy-split-fields">
        <label class="ai-copy-field">
          <span><strong>内容场景</strong><small>必选</small></span>
          <AiCopyDropdown
            v-model="form.scene"
            aria-label="内容场景"
            :options="options.scenes"
            placeholder="选择内容场景"
          />
        </label>
        <label class="ai-copy-field">
          <span><strong>节日氛围</strong><small>可选</small></span>
          <AiCopyDropdown
            v-model="form.festival"
            aria-label="节日氛围"
            :options="festivalOptions"
            placeholder="不指定节日"
          />
        </label>
      </div>

      <section class="ai-copy-product-panel">
        <div class="ai-copy-product-heading">
          <div><p>PRODUCT REFERENCE</p><h3>商品链接参考</h3></div>
          <span>可选</span>
        </div>
        <p class="ai-copy-product-help">粘贴链接后，生成时 LLM 会主动调用商品读取工具，先取得商品资料再写文案。</p>
        <div class="ai-copy-link-row">
          <input v-model="form.productUrl" type="url" placeholder="https://item.example.com/product" />
          <button :disabled="readingProduct" type="button" @click="inspectProduct">
            {{ readingProduct ? '读取中…' : '读取链接' }}
          </button>
          <button
            v-if="form.productUrl || productReference"
            class="ai-copy-delete-link"
            :disabled="readingProduct"
            type="button"
            @click="clearProductLink"
          >删除链接</button>
        </div>

        <details ref="searchConfigDetails" class="ai-copy-search-config">
          <summary>配置商品搜索服务 <span>可选</span></summary>
          <label class="ai-copy-field">
            <span><strong>服务地址</strong><small>POST JSON: { url }</small></span>
            <input v-model="form.searchEndpoint" type="url" placeholder="https://example.com/product/inspect" />
          </label>
          <label class="ai-copy-field">
            <span><strong>API Key</strong><small>仅用于本次请求，不保存</small></span>
            <input v-model="form.searchApiKey" type="password" autocomplete="off" placeholder="Bearer API Key" />
          </label>
        </details>

        <article v-if="productReference" class="ai-copy-reference">
          <span>已读取</span>
          <div><strong>{{ productReference.title }}</strong><p>{{ productReference.summary }}</p></div>
          <dl v-if="Object.keys(productReference.attributes).length">
            <template v-for="(value, key) in productReference.attributes" :key="key">
              <dt>{{ key }}</dt><dd>{{ value }}</dd>
            </template>
          </dl>
        </article>
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
            :disabled="generating || readingProduct"
            type="button"
            @click="clearAll"
          >一键清空</button>
        </div>
      </div>

      <div v-if="generating" class="ai-copy-generating" aria-live="polite">
        <span></span><span></span><span></span>
        <p>{{ form.productUrl ? '正在读取商品资料并构思文案' : '正在根据创意简报构思文案' }}</p>
      </div>

      <template v-else-if="result">
        <article class="ai-copy-output ai-copy-title-output">
          <div class="ai-copy-output-label"><span>标题</span><small>{{ result.title.length }} / 30</small></div>
          <h3>{{ result.title }}</h3>
          <button type="button" @click="copyText('title', result.title)">
            {{ copiedField === 'title' ? '已复制' : '复制标题' }}
          </button>
        </article>

        <article class="ai-copy-output ai-copy-body-output">
          <div class="ai-copy-output-label"><span>正文文案</span><small>{{ result.body.length }} / 1000</small></div>
          <p>{{ result.body }}</p>
          <button type="button" @click="copyText('body', result.body)">
            {{ copiedField === 'body' ? '已复制' : '复制文案' }}
          </button>
        </article>

        <button class="ai-copy-import" type="button" @click="importToWorkbench">
          <span>
            <small>IMPORT TO WORKBENCH</small>
            <strong>导入发布工作台</strong>
            <em>覆盖工作台现有标题与文案</em>
          </span>
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M5 12h13M14 7l5 5-5 5" />
          </svg>
        </button>

        <p class="ai-copy-result-meta">
          由 {{ result.provider }} · {{ result.model }} 生成<span v-if="result.product_reference"> · 已引用商品资料</span>
        </p>
      </template>

      <div v-else class="ai-copy-empty">
        <span>AI</span>
        <h3>一份好文案，从清楚的要点开始</h3>
        <p>填写左侧创意简报，选择风格与场景。商品链接是参考，不会替代你的核心卖点。</p>
        <ol><li>标题控制在 30 字以内</li><li>正文控制在 1000 字以内</li><li>不虚构商品信息与促销承诺</li></ol>
      </div>
    </aside>
  </section>
</template>

<style src="./ai-copy.css"></style>
