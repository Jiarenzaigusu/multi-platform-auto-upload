<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

import { apiRequest } from '../api-client.js'

const emit = defineEmits(['authenticated'])

const loading = ref(true)
const setupRequired = ref(false)
const authMode = ref('login')
const submitting = ref(false)
const error = ref('')
const form = reactive({
  username: '',
  displayName: '',
  password: '',
  confirmPassword: '',
})
const registering = computed(() => !setupRequired.value && authMode.value === 'register')

/** Resolve an existing session before deciding which authentication form to show. */
async function loadStatus() {
  loading.value = true
  error.value = ''
  try {
    const status = await apiRequest('/api/auth/status')
    setupRequired.value = status.setup_required
    if (status.authenticated && status.user) emit('authenticated', status.user)
  } catch (requestError) {
    error.value = `无法连接登录服务：${requestError.message}`
  } finally {
    loading.value = false
  }
}

function switchMode(mode) {
  authMode.value = mode
  form.password = ''
  form.confirmPassword = ''
  error.value = ''
}

/** Bootstrap the first administrator, register an operator, or sign in. */
async function submit() {
  if (registering.value && form.password !== form.confirmPassword) {
    error.value = '两次输入的密码不一致'
    return
  }
  submitting.value = true
  error.value = ''
  try {
    const path = setupRequired.value
      ? '/api/auth/bootstrap'
      : registering.value
        ? '/api/auth/register'
        : '/api/auth/login'
    const payload = setupRequired.value
      ? {
          username: form.username.trim(),
          display_name: form.displayName.trim(),
          password: form.password,
        }
      : registering.value
        ? {
            username: form.username.trim(),
            display_name: form.displayName.trim(),
            password: form.password,
          }
      : { username: form.username.trim(), password: form.password }
    const user = await apiRequest(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    form.password = ''
    form.confirmPassword = ''
    emit('authenticated', user)
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    submitting.value = false
  }
}

onMounted(loadStatus)
</script>

<template>
  <main class="auth-shell">
    <section class="auth-story" aria-hidden="true">
      <span class="auth-mark">M</span>
      <p>INTERNAL COMMERCE OPS</p>
      <h1>每个人，一间独立的发布工作室。</h1>
      <div class="auth-separation">
        <span>任务</span><i></i><span>Cookie</span><i></i><span>浏览器</span><i></i><span>密钥</span>
      </div>
      <small>天猫与京东自动发布 · 用户数据相互隔离</small>
    </section>

    <section class="auth-panel">
      <form v-if="!loading" class="auth-card" @submit.prevent="submit">
        <div v-if="!setupRequired" class="auth-mode-tabs" aria-label="登录方式">
          <button :class="{ active: !registering }" type="button" @click="switchMode('login')">登录</button>
          <button :class="{ active: registering }" type="button" @click="switchMode('register')">注册</button>
        </div>
        <p class="auth-eyebrow">{{ setupRequired ? 'FIRST RUN / ADMIN' : registering ? 'NEW OPERATOR' : 'SECURE SIGN IN' }}</p>
        <h2>{{ setupRequired ? '创建初始管理员' : registering ? '创建操作者账号' : '进入发布台' }}</h2>
        <span class="auth-help">
          {{ setupRequired
            ? '此入口仅在服务器本机且尚无用户时开放。'
            : registering
              ? '注册成功后默认获得操作者权限；管理员请直接登录。'
              : '使用已有账号登录，管理员无需重新注册。' }}
        </span>
        <label>
          <span>用户名</span>
          <input v-model="form.username" autocomplete="username" minlength="3" maxlength="64" required />
        </label>
        <label v-if="setupRequired || registering">
          <span>显示名称</span>
          <input
            v-model="form.displayName"
            autocomplete="name"
            maxlength="80"
            :placeholder="setupRequired ? '例如：电商运营管理员' : '例如：华东区运营'"
          />
        </label>
        <label>
          <span>密码</span>
          <input
            v-model="form.password"
            :autocomplete="setupRequired || registering ? 'new-password' : 'current-password'"
            :minlength="setupRequired || registering ? 10 : 1"
            maxlength="256"
            required
            type="password"
          />
          <small v-if="setupRequired || registering">至少 10 个字符。</small>
        </label>
        <label v-if="registering">
          <span>确认密码</span>
          <input
            v-model="form.confirmPassword"
            autocomplete="new-password"
            minlength="10"
            maxlength="256"
            required
            type="password"
          />
        </label>
        <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
        <button class="auth-submit" :disabled="submitting" type="submit">
          {{ submitting ? '正在验证…' : setupRequired ? '创建并登录' : registering ? '注册并进入发布台' : '安全登录' }}
        </button>
      </form>
      <div v-else class="auth-loading">正在检查服务状态…</div>
    </section>
  </main>
</template>

<style scoped>
.auth-shell { min-height: 100vh; display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(360px, .75fr); color: #153c33; background: #e8efe8; }
.auth-story { position: relative; overflow: hidden; display: flex; min-height: 100vh; flex-direction: column; justify-content: center; padding: clamp(45px, 8vw, 120px); color: #f4f5e9; background: radial-gradient(circle at 82% 16%, rgba(215,224,93,.3), transparent 28%), linear-gradient(145deg, #12352f, #1f5144 72%, #49683f); }
.auth-story::after { position: absolute; right: -10vw; bottom: -20vw; width: 48vw; height: 48vw; border: 1px solid rgba(225,235,190,.2); border-radius: 50%; box-shadow: 0 0 0 6vw rgba(225,235,190,.04), 0 0 0 13vw rgba(225,235,190,.03); content: ''; }
.auth-mark { display: grid; width: 52px; height: 52px; place-items: center; border-radius: 17px 17px 3px; color: #173c35; background: #d7e05d; font-family: Georgia, serif; font-size: 31px; font-weight: 700; }
.auth-story > p { margin: 28px 0 10px; color: #b7ceab; font-size: 11px; font-weight: 800; letter-spacing: .18em; }
.auth-story h1 { max-width: 700px; margin: 0; font-family: Georgia, "Songti SC", serif; font-size: clamp(42px, 6vw, 82px); font-weight: 400; line-height: 1.08; letter-spacing: -.05em; }
.auth-separation { z-index: 1; display: flex; max-width: 650px; gap: 10px; align-items: center; margin-top: 44px; color: #dce7cc; font-size: 12px; }.auth-separation i { flex: 1; height: 1px; background: rgba(215,224,93,.45); }.auth-story small { z-index: 1; margin-top: 18px; color: #9fbcaa; }
.auth-panel { display: grid; place-items: center; padding: clamp(28px, 6vw, 80px); background: radial-gradient(circle at 20% 8%, #f8fbdc, transparent 18rem); }
.auth-card { width: min(100%, 430px); padding: clamp(28px, 4vw, 48px); border: 1px solid rgba(32,75,65,.17); border-radius: 22px; background: rgba(255,255,250,.83); box-shadow: 0 24px 70px rgba(31,70,53,.13); backdrop-filter: blur(12px); }
.auth-mode-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-bottom: 25px; padding: 4px; border-radius: 11px; background: #e8efe7; }.auth-mode-tabs button { padding: 10px 12px; border: 0; border-radius: 8px; color: #63776d; background: transparent; font-weight: 750; cursor: pointer; }.auth-mode-tabs button.active { color: #f9fff6; background: #28594d; box-shadow: 0 5px 12px rgba(22,68,54,.18); }
.auth-eyebrow { margin: 0 0 8px; color: #6b8161; font-size: 10px; font-weight: 800; letter-spacing: .16em; }.auth-card h2 { margin: 0; font-family: Georgia, "Songti SC", serif; font-size: 34px; font-weight: 500; }.auth-help { display: block; margin: 10px 0 28px; color: #6c7e72; font-size: 13px; line-height: 1.6; }
.auth-card label { display: grid; gap: 7px; margin-bottom: 17px; }.auth-card label > span { color: #39544b; font-size: 12px; font-weight: 750; }.auth-card input { width: 100%; padding: 12px 13px; border: 1px solid #bdcfc1; border-radius: 9px; outline: none; color: #183d36; background: #fff; }.auth-card input:focus { border-color: #648644; box-shadow: 0 0 0 3px rgba(156,193,100,.2); }.auth-card label small { color: #7a887f; font-size: 11px; line-height: 1.5; }
.auth-submit { width: 100%; margin-top: 8px; padding: 14px; border: 0; border-radius: 9px; color: #fbfff7; background: #28594d; box-shadow: 0 8px 17px rgba(22,68,54,.22); font-weight: 750; }.auth-submit:disabled { cursor: wait; opacity: .62; }.auth-error { padding: 10px 12px; border-radius: 8px; color: #873b2f; background: #fdf0ec; font-size: 12px; line-height: 1.5; }.auth-loading { color: #526a60; font-size: 13px; }
@media (max-width: 820px) { .auth-shell { grid-template-columns: 1fr; }.auth-story { min-height: auto; padding: 46px 28px; }.auth-story h1 { max-width: 560px; font-size: 42px; }.auth-panel { min-height: 55vh; }.auth-separation { margin-top: 28px; } }
@media (max-width: 480px) { .auth-story h1 { font-size: 34px; }.auth-separation { display: grid; grid-template-columns: 1fr 1fr; }.auth-separation i { display: none; }.auth-panel { padding: 24px 16px; } }
</style>
