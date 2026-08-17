<script setup>
import { ref } from 'vue'

import { apiRequest, apiUrl } from '../api-client.js'

const emit = defineEmits(['close'])
const pairingCode = ref('')
const expiresAt = ref('')
const busy = ref(false)
const error = ref('')
const copied = ref(false)
const installerName = 'MPAU-Agent-Setup.exe'
const installerUrl = '/downloads/MPAU-Agent-Setup.exe'

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
  try {
    await navigator.clipboard.writeText(pairingCode.value)
    copied.value = true
  } catch {
    error.value = '浏览器无法自动复制，请手动输入配对码。'
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
        <li>配对完成后助手自动保持连接；登录平台和上传任务会在该电脑的 Edge 中执行。</li>
      </ol>
      <a class="agent-download" :href="apiUrl(installerUrl)">下载 {{ installerName }}</a>
      <button class="agent-code-button" :disabled="busy" type="button" @click="generateCode">
        {{ busy ? '正在生成…' : pairingCode ? '重新生成一次性配对码' : '生成一次性配对码' }}
      </button>
      <button v-if="pairingCode" class="agent-code" title="点击复制" type="button" @click="copyCode">
        <strong>{{ pairingCode }}</strong>
        <small>{{ copied ? '已复制到剪贴板' : `${expiryLabel()} · 点击复制` }}</small>
      </button>
      <p v-if="error" class="agent-dialog-error" role="alert">{{ error }}</p>
      <p class="agent-dialog-note">发布台地址填写当前网页地址，例如 <code>https://publish.example.com</code>。生产环境必须使用 HTTPS；本机开发时可使用 <code>http://127.0.0.1:8788</code>。</p>
    </section>
  </div>
</template>

<style scoped>
.agent-dialog-backdrop { position: fixed; z-index: 50; inset: 0; display: grid; place-items: center; padding: 20px; background: rgba(18, 38, 32, .46); backdrop-filter: blur(5px); }.agent-dialog { position: relative; width: min(560px, 100%); padding: 34px; border: 1px solid rgba(32,75,65,.2); border-radius: 18px; color: #29483f; background: #fffefa; box-shadow: 0 30px 80px rgba(13, 42, 33, .35); }.agent-dialog > p:first-of-type { margin: 0 0 8px; color: #6b8161; font-size: 10px; font-weight: 800; letter-spacing: .15em; }.agent-dialog h2 { margin: 0; color: #1c4036; font: 500 29px Georgia, "Songti SC", serif; }.agent-dialog ol { display: grid; gap: 9px; margin: 20px 0; padding-left: 20px; color: #596e63; font-size: 13px; line-height: 1.55; }.agent-dialog-close { position: absolute; top: 13px; right: 15px; border: 0; color: #587067; background: transparent; font-size: 26px; line-height: 1; }.agent-download, .agent-code-button { display: block; width: 100%; padding: 12px 14px; border-radius: 9px; text-align: center; font-size: 13px; font-weight: 750; text-decoration: none; }.agent-download { box-sizing: border-box; color: #fff; background: #28594d; }.agent-code-button { margin-top: 10px; border: 1px solid #d07a56; color: #8d4329; background: #fff6ed; }.agent-code-button:disabled { cursor: wait; opacity: .65; }.agent-code { display: grid; width: 100%; gap: 5px; margin-top: 11px; padding: 12px; border: 1px dashed #8ba47c; border-radius: 9px; color: #224c3d; background: #f5f9ee; }.agent-code strong { font: 800 22px "SFMono-Regular", Consolas, monospace; letter-spacing: .12em; }.agent-code small { color: #6b8174; font-size: 11px; }.agent-dialog-error { margin: 12px 0 0; color: #923d2f; font-size: 12px; }.agent-dialog-note { margin: 18px 0 0; padding-top: 15px; border-top: 1px solid #dce5d7; color: #718177; font-size: 11px; line-height: 1.6; }.agent-dialog-note code { color: #315a4d; font-family: "SFMono-Regular", Consolas, monospace; }
</style>
