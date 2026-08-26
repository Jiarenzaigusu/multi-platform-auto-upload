<script setup>
import { computed, ref } from 'vue'

import { apiRequest, apiUrl } from '../api-client.js'

const props = defineProps({
  agentVersion: { type: String, default: '' },
  latestVersion: { type: String, default: '' },
  online: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])
const pairingCode = ref('')
const expiresAt = ref('')
const busy = ref(false)
const error = ref('')
const copied = ref(false)
const installerName = 'MPAU-Agent-Setup.exe'
const installerUrl = '/downloads/MPAU-Agent-Setup.exe'

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

const updateAvailable = computed(() => (
  props.online
    && props.agentVersion
    && props.latestVersion
    && compareVersions(props.agentVersion, props.latestVersion) < 0
))

const expiryLabel = () => {
  const value = new Date(expiresAt.value)
  return Number.isNaN(value.getTime())
    ? '5 分钟内有效'
    : `${value.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} 前有效`
}

async function generateCode() {
  busy.value = true
  error.value = ''
  copied.value = false
  try {
    const result = await apiRequest('/api/agent/pairing-code', { method: 'POST' })
    pairingCode.value = result.pairing_code
    expiresAt.value = result.expires_at
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    busy.value = false
  }
}

async function copyCode() {
  if (!pairingCode.value) return
  error.value = ''
  try {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(pairingCode.value)
    } else if (!copyWithLegacyClipboard(pairingCode.value)) {
      throw new Error('clipboard unavailable')
    }
    copied.value = true
  } catch {
    // HTTP server-IP access is not a secure context, so use the user-gesture
    // fallback supported by browsers that still permit document.execCommand.
    if (copyWithLegacyClipboard(pairingCode.value)) {
      copied.value = true
      return
    }
    error.value = '当前浏览器限制剪贴板权限，请手动输入配对码。'
  }
}

function copyWithLegacyClipboard(value) {
  if (typeof document.execCommand !== 'function') return false
  const field = document.createElement('textarea')
  field.value = value
  field.setAttribute('readonly', '')
  field.style.cssText = 'position:fixed;opacity:0;pointer-events:none;'
  document.body.appendChild(field)
  try {
    field.select()
    return document.execCommand('copy')
  } finally {
    field.remove()
  }
}
</script>

<template>
  <div class="agent-dialog-backdrop" role="presentation" @click.self="emit('close')">
    <section class="agent-dialog" aria-labelledby="agent-setup-title" role="dialog" aria-modal="true">
      <button class="agent-dialog-close" aria-label="关闭" type="button" @click="emit('close')">×</button>
      <p>LOCAL EXECUTION ASSISTANT</p>
      <h2 id="agent-setup-title">安装并配对 Windows 助手</h2>
      <ol>
        <li>在 Windows 电脑下载并安装助手。</li>
        <li>打开“MPAU 本地执行助手”，填入发布台地址和下方配对码。</li>
        <li>配对完成后助手自动保持连接；登录平台和上传任务会在该电脑的 Edge 中执行。单条发布的文件选择框会直传到最新助手。</li>
      </ol>
      <a class="agent-download" :href="apiUrl(installerUrl)">下载 {{ installerName }}</a>
      <p v-if="updateAvailable" class="agent-update-hint" role="status">
        检测到 Windows 助手有新版本 <strong>v{{ latestVersion }}</strong>（当前 v{{ agentVersion }}）。
        无需重新下载安装：直接双击“MPAU 本地执行助手”打开窗口，里面有更新进度和“安装新版本”按钮，助手会自动更新并重启。
      </p>
      <p v-else-if="online && agentVersion" class="agent-update-hint current">
        Windows 助手在线，版本 v{{ agentVersion }}，已是最新。
      </p>
      <button class="agent-code-button" :disabled="busy" type="button" @click="generateCode">
        {{ busy ? '正在生成…' : pairingCode ? '重新生成一次性配对码' : '生成一次性配对码' }}
      </button>
      <button v-if="pairingCode" class="agent-code" title="点击复制" type="button" @click="copyCode">
        <strong>{{ pairingCode }}</strong>
        <small>{{ copied ? '已复制到剪贴板' : `${expiryLabel()} · 点击复制` }}</small>
      </button>
      <p v-if="error" class="agent-dialog-error" role="alert">{{ error }}</p>
      <p class="agent-dialog-note">发布台地址填写当前网页地址，例如 <code>http://10.31.108.221:8788</code>。</p>
    </section>
  </div>
</template>

<style scoped>
.agent-dialog-backdrop {
  position: fixed;
  z-index: 50;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(13, 34, 31, .5);
  backdrop-filter: blur(8px);
}

.agent-dialog {
  position: relative;
  width: min(560px, 100%);
  padding: 30px;
  border: 1px solid rgba(33, 67, 62, .16);
  border-radius: 18px;
  color: #29483f;
  background:
    radial-gradient(circle at 90% 0, rgba(231, 237, 106, .2), transparent 12rem),
    #fcfdf8;
  box-shadow: 0 28px 72px rgba(13, 42, 33, .28);
}

.agent-dialog > p:first-of-type {
  margin: 0 0 8px;
  color: #7a8f78;
  font-size: 9px;
  font-weight: 850;
  letter-spacing: .16em;
}

.agent-dialog h2 {
  margin: 0;
  color: #183532;
  font-size: 28px;
  font-weight: 850;
  line-height: 1.15;
}

.agent-dialog ol {
  display: grid;
  gap: 8px;
  margin: 18px 0 0;
  padding-left: 20px;
  color: #5e7267;
  font-size: 12px;
  line-height: 1.55;
}

.agent-dialog-close {
  position: absolute;
  top: 12px;
  right: 14px;
  border: 0;
  color: #587067;
  background: transparent;
  font-size: 24px;
  line-height: 1;
}

.agent-download,
.agent-code-button {
  display: block;
  width: 100%;
  padding: 12px 14px;
  border-radius: 9px;
  text-align: center;
  font-size: 12px;
  font-weight: 850;
  text-decoration: none;
}

.agent-download {
  box-sizing: border-box;
  margin-top: 14px;
  color: #173532;
  background: #e7ed6a;
  box-shadow: 0 9px 20px rgba(129, 145, 48, .2);
}

.agent-code-button {
  margin-top: 10px;
  border: 1px solid #d09c45;
  color: #8d4329;
  background: #fff6ed;
}

.agent-code-button:disabled {
  cursor: wait;
  opacity: .65;
}

.agent-code {
  display: grid;
  width: 100%;
  gap: 4px;
  margin-top: 11px;
  padding: 12px;
  border: 1px dashed #a8bf9e;
  border-radius: 9px;
  color: #224c3d;
  background: #f5f9ee;
}

.agent-code strong {
  font-size: 20px;
  font-weight: 900;
  letter-spacing: .08em;
}

.agent-code small {
  color: #6b8174;
  font-size: 10px;
}

.agent-dialog-error {
  margin: 12px 0 0;
  color: #923d2f;
  font-size: 11px;
}

.agent-dialog-note {
  margin: 16px 0 0;
  padding-top: 14px;
  border-top: 1px solid #dce5d7;
  color: #718177;
  font-size: 10px;
  line-height: 1.6;
}

.agent-dialog-note code {
  color: #315a4d;
}

.agent-update-hint {
  margin: 12px 0 0;
  padding: 11px 13px;
  border: 1px solid #e5b892;
  border-radius: 9px;
  color: #7c4522;
  background: #fdf3e7;
  font-size: 11px;
  line-height: 1.55;
}

.agent-update-hint.current {
  border-color: #b7cdb0;
  color: #3d5c46;
  background: #f2f8ee;
}
</style>
