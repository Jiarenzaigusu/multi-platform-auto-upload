<script setup>
import { onMounted, reactive, ref } from 'vue'

import { apiRequest } from '../../api-client.js'

const props = defineProps({
  currentUserId: { type: String, required: true },
})

const users = ref([])
const loading = ref(true)
const busyUserId = ref('')
const creating = ref(false)
const error = ref('')
const notice = ref('')
const resetPasswords = reactive({})
const createForm = reactive({
  username: '',
  displayName: '',
  password: '',
  role: 'operator',
})

/** Load fresh user state so role and disabled-session changes are immediately visible. */
async function loadUsers() {
  loading.value = true
  error.value = ''
  try {
    users.value = await apiRequest('/api/admin/users')
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

/** Provision a new company account without opening public registration. */
async function createUser() {
  creating.value = true
  error.value = ''
  notice.value = ''
  try {
    await apiRequest('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: createForm.username.trim(),
        display_name: createForm.displayName.trim(),
        password: createForm.password,
        role: createForm.role,
      }),
    })
    Object.assign(createForm, { username: '', displayName: '', password: '', role: 'operator' })
    notice.value = '用户已创建，可以立即使用新账号登录。'
    await loadUsers()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    creating.value = false
  }
}

/** Persist one user's display name, role, and access state. */
async function saveUser(user) {
  busyUserId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    const updated = await apiRequest(`/api/admin/users/${user.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        display_name: user.display_name.trim(),
        role: user.role,
        status: user.status,
      }),
    })
    Object.assign(user, updated)
    notice.value = `已更新 ${updated.username}。`
  } catch (requestError) {
    error.value = requestError.message
    await loadUsers()
  } finally {
    busyUserId.value = ''
  }
}

/** Reset a password and invalidate every session owned by the target user. */
async function resetPassword(user) {
  const password = resetPasswords[user.id] || ''
  if (password.length < 10) {
    error.value = '新密码至少需要 10 个字符。'
    return
  }
  if (!window.confirm(`确定重置 ${user.username} 的密码并退出其所有登录会话吗？`)) return
  busyUserId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    await apiRequest(`/api/admin/users/${user.id}/reset-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
    resetPasswords[user.id] = ''
    notice.value = `${user.username} 的密码已重置，原会话已全部失效。`
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    busyUserId.value = ''
  }
}

/** Revoke active sessions without changing the user's password. */
async function revokeSessions(user) {
  if (!window.confirm(`确定让 ${user.username} 的所有设备退出登录吗？`)) return
  busyUserId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    await apiRequest(`/api/admin/users/${user.id}/revoke-sessions`, { method: 'POST' })
    notice.value = `${user.username} 的登录会话已撤销。`
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    busyUserId.value = ''
  }
}

/** Delete a company login and all data owned by the target user. */
async function deleteUser(user) {
  if (user.id === props.currentUserId) {
    error.value = '不能删除当前登录账号。'
    return
  }
  const message = `确定删除 ${user.username} 吗？\n\n会删除该登录账号，以及该用户名下的任务记录、素材文件、店铺 Cookie、日志、LLM 密钥和本地助手配对记录。`
  if (!window.confirm(message)) return
  busyUserId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    await apiRequest(`/api/admin/users/${user.id}`, { method: 'DELETE' })
    users.value = users.value.filter((entry) => entry.id !== user.id)
    delete resetPasswords[user.id]
    notice.value = `${user.username} 已删除。`
  } catch (requestError) {
    error.value = requestError.message
    await loadUsers()
  } finally {
    busyUserId.value = ''
  }
}

onMounted(loadUsers)
</script>

<template>
  <section class="users-layout">
    <article class="users-card">
      <header><p>01 / ACCESS DIRECTORY</p><h2>公司用户</h2><span>用户名和用户 ID 创建后保持不变，确保工作区不会串用。</span></header>
      <p v-if="error" class="users-message error" role="alert">{{ error }}</p>
      <p v-if="notice" class="users-message success" role="status">{{ notice }}</p>
      <p v-if="loading" class="users-empty">正在读取用户…</p>
      <div v-else class="users-list">
        <article v-for="user in users" :key="user.id" class="user-row">
          <div class="user-identity">
            <span>{{ user.username.slice(0, 1).toUpperCase() }}</span>
            <div>
              <strong>{{ user.username }} <em v-if="user.id === currentUserId">当前账号</em></strong>
              <small>{{ user.id }}</small>
            </div>
          </div>

          <div class="user-panel">
            <div class="user-profile">
              <label>
                <span>显示名称</span>
                <input v-model="user.display_name" maxlength="80" />
              </label>
              <label>
                <span>角色</span>
                <select v-model="user.role">
                  <option value="admin">管理员</option>
                  <option value="operator">操作员</option>
                </select>
              </label>
              <label>
                <span>状态</span>
                <select v-model="user.status">
                  <option value="active">启用</option>
                  <option value="disabled">停用</option>
                </select>
              </label>
            </div>

            <div class="user-save-row">
              <button class="user-save" :disabled="busyUserId === user.id" type="button" @click="saveUser(user)">保存资料</button>
            </div>

            <div class="user-security">
              <input v-model="resetPasswords[user.id]" autocomplete="new-password" minlength="10" placeholder="输入新密码（至少 10 位）" type="password" />
              <button :disabled="busyUserId === user.id" type="button" @click="resetPassword(user)">重置密码</button>
              <button :disabled="busyUserId === user.id" type="button" @click="revokeSessions(user)">退出所有会话</button>
              <button class="user-delete" :disabled="busyUserId === user.id || user.id === currentUserId" type="button" @click="deleteUser(user)">删除账号</button>
            </div>
          </div>
        </article>
      </div>
    </article>

    <aside class="create-user-card">
      <p>02 / NEW MEMBER</p>
      <h2>添加成员</h2>
      <form @submit.prevent="createUser">
        <label><span>用户名</span><input v-model="createForm.username" autocomplete="off" minlength="3" maxlength="64" required placeholder="例如 zhangsan" /></label>
        <label><span>显示名称</span><input v-model="createForm.displayName" autocomplete="off" maxlength="80" placeholder="例如 张三" /></label>
        <label><span>初始密码</span><input v-model="createForm.password" autocomplete="new-password" minlength="10" maxlength="256" required type="password" /></label>
        <label><span>角色</span><select v-model="createForm.role"><option value="operator">操作员</option><option value="admin">管理员</option></select></label>
        <button :disabled="creating" type="submit">{{ creating ? '正在创建…' : '创建公司账号' }}</button>
      </form>
      <dl><dt>管理员</dt><dd>用户管理、发布、AI 与模型配置</dd><dt>操作员</dt><dd>发布、AI 与自己的模型配置</dd></dl>
    </aside>
  </section>
</template>

<style scoped>
.users-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 18px;
  align-items: start;
}

.users-card,
.create-user-card {
  border: 1px solid rgba(33, 67, 62, .13);
  border-radius: 15px;
  box-shadow: 0 16px 42px rgba(29, 65, 56, .08);
  background: rgba(252, 253, 249, .9);
}

.users-card {
  padding: clamp(20px, 2.4vw, 30px);
}

.users-card header > p,
.create-user-card > p {
  margin: 0 0 7px;
  color: #7a8f78;
  font-size: 9px;
  font-weight: 850;
  letter-spacing: .16em;
}

.users-card h2,
.create-user-card h2 {
  margin: 0;
  color: #183033;
  font-size: 24px;
  font-weight: 850;
}

.users-card header > span {
  display: block;
  margin-top: 7px;
  color: #6a786e;
  font-size: 12px;
}

.users-list {
  display: grid;
  gap: 10px;
  margin-top: 18px;
}

.user-row {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  padding: 18px;
  border: 1px solid #d6dfd9;
  border-radius: 14px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, .66), rgba(247, 250, 246, .96)),
    #f7faf6;
}

.user-row + .user-row {
  margin-top: 2px;
}

.user-identity {
  display: flex;
  gap: 12px;
  align-items: center;
  min-width: 0;
  padding: 0 0 13px;
  border-bottom: 1px dashed #dfe8df;
}

.user-identity > span {
  display: grid;
  flex: 0 0 40px;
  width: 40px;
  height: 40px;
  place-items: center;
  border-radius: 50%;
  color: #173532;
  background: #e7ed6a;
  font-size: 16px;
  font-weight: 900;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.35);
}

.user-identity strong,
.user-identity small {
  display: block;
}

.user-identity strong {
  color: #24473c;
  font-size: 13px;
}

.user-identity small {
  overflow: hidden;
  max-width: min(520px, 62vw);
  margin-top: 4px;
  color: #829087;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-identity em {
  margin-left: 6px;
  padding: 3px 6px;
  border-radius: 99px;
  color: #2e6448;
  background: #dcecd5;
  font-size: 9px;
  font-style: normal;
}

.user-panel {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.user-profile {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(132px, .48fr) minmax(118px, .42fr);
  gap: 10px;
  align-items: end;
  min-width: 0;
}

.user-profile label,
.create-user-card label {
  display: grid;
  gap: 6px;
}

.user-profile label > span,
.create-user-card label > span {
  color: #496158;
  font-size: 11px;
  font-weight: 850;
}

.user-profile input,
.user-profile select,
.create-user-card input,
.create-user-card select {
  min-width: 0;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid #cbd8d0;
  border-radius: 8px;
  outline: none;
  color: #183033;
  background: #fff;
  font-size: 12px;
}

.user-profile input:focus,
.user-profile select:focus,
.create-user-card input:focus,
.create-user-card select:focus {
  border-color: #779e65;
  box-shadow: 0 0 0 3px rgba(133, 174, 101, .16);
}

.user-row button,
.create-user-card button {
  min-height: 40px;
  padding: 9px 10px;
  border: 1px solid #91a88f;
  border-radius: 8px;
  color: #244e41;
  background: #f9fbf5;
  font-size: 10px;
  font-weight: 800;
  line-height: 1.25;
  white-space: nowrap;
}

.user-row button:disabled,
.create-user-card button:disabled {
  cursor: wait;
  opacity: .55;
}

.user-save {
  background: #eef6e3;
}

.user-save-row {
  display: flex;
  justify-content: flex-start;
}

.user-save-row .user-save {
  min-width: 112px;
}

.user-security {
  display: flex;
  flex-wrap: nowrap;
  gap: 10px;
  align-items: center;
  min-width: 0;
  padding-top: 2px;
}

.user-security input {
  flex: 1 1 240px;
  min-height: 40px;
}

.user-security button {
  flex: 0 0 auto;
  min-width: 96px;
}

.user-security button:nth-of-type(3) {
  min-width: 118px;
}

.user-security button:last-child,
.user-delete {
  border-color: #c4968b;
  color: #843d31;
}

.users-message {
  margin: 14px 0 0;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 11px;
}

.users-message.error {
  color: #873b2f;
  background: #fdf0ec;
}

.users-message.success {
  color: #286046;
  background: #e5f4e4;
}

.users-empty {
  color: #748379;
  font-size: 12px;
}

.create-user-card {
  position: sticky;
  top: 20px;
  padding: 23px;
  color: #eff1df;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, .05), transparent 45%),
    #1a4846;
}

.create-user-card > p {
  color: #e7ed6a;
}

.create-user-card h2 {
  color: #fff;
}

.create-user-card form {
  display: grid;
  gap: 13px;
  margin-top: 18px;
}

.create-user-card label > span {
  color: #c3d5c8;
}

.create-user-card button[type="submit"] {
  margin-top: 2px;
  border: 0;
  color: #173c35;
  background: #e7ed6a;
  box-shadow: 0 9px 20px rgba(129, 145, 48, .22);
}

.create-user-card dl {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 6px 10px;
  margin: 20px 0 0;
  padding-top: 16px;
  border-top: 1px solid rgba(224, 240, 194, .22);
  font-size: 10px;
  line-height: 1.45;
}

.create-user-card dt {
  color: #e7ed6a;
  font-weight: 850;
}

.create-user-card dd {
  margin: 0;
  color: #b5cab9;
}

@media (max-width: 1150px) {
  .users-layout {
    grid-template-columns: 1fr;
  }

  .create-user-card {
    position: static;
  }

  .user-profile {
    grid-template-columns: minmax(200px, 1fr) minmax(128px, .55fr) minmax(112px, .5fr);
  }

  .user-security {
    flex-wrap: wrap;
  }
}

@media (max-width: 700px) {
  .user-row {
    gap: 10px;
  }

  .user-profile {
    grid-template-columns: 1fr;
  }

  .user-security {
    align-items: stretch;
  }

  .user-security input {
    flex-basis: 100%;
  }

  .user-security button {
    flex: 1 1 130px;
  }
}
</style>
