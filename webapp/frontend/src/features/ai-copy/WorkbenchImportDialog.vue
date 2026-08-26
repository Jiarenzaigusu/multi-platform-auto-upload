<!-- Select exactly one publishing workspace before importing AI copy. -->
<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close', 'confirm'])

const platformOptions = [
  { value: 'tmall', label: '天猫光合' },
  { value: 'jd', label: '京东京麦' },
  { value: 'xiaohongshu', label: '小红书' },
  { value: 'douyin', label: '抖音' },
]
const platform = ref('tmall')
const contentType = ref('video')
const currentPlatform = computed(() => platformOptions.find((item) => item.value === platform.value) || platformOptions[0])
const targetLabel = computed(() => `${currentPlatform.value.label}${contentType.value === 'video' ? '视频' : '图文'}发布台`)
const importedFieldsLabel = computed(() => (
  platform.value === 'jd' && contentType.value === 'video' ? '标题' : '标题与正文'
))
let bodyOverflowBeforeOpen = ''

watch(() => props.open, (visible) => {
  if (typeof document !== 'undefined') {
    if (visible) {
      bodyOverflowBeforeOpen = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = bodyOverflowBeforeOpen
    }
  }
  if (visible) {
    platform.value = 'tmall'
    contentType.value = 'video'
  }
})

onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.body.style.overflow = bodyOverflowBeforeOpen
  }
})

function confirm() {
  emit('confirm', { platform: platform.value, contentType: contentType.value })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="workbench-import-backdrop" @click.self="emit('close')">
      <section class="workbench-import-dialog" role="dialog" aria-modal="true" aria-labelledby="workbench-import-title">
        <button class="workbench-import-close" type="button" aria-label="关闭" @click="emit('close')">×</button>
        <p>IMPORT DESTINATION</p>
        <h2 id="workbench-import-title">选择导入发布台</h2>
        <span>文案只会写入所选发布台，不会写入其他平台或内容类型；不支持正文的发布台仅导入标题。</span>

        <fieldset>
          <legend>发布平台</legend>
          <label v-for="item in platformOptions" :key="item.value" :class="{ selected: platform === item.value }"><input v-model="platform" type="radio" :value="item.value" /><b>{{ item.label }}</b><small>视频与图文</small></label>
        </fieldset>
        <fieldset>
          <legend>发布类型</legend>
          <label :class="{ selected: contentType === 'video' }"><input v-model="contentType" type="radio" value="video" /><b>视频</b><small>导入标题与视频文案</small></label>
          <label :class="{ selected: contentType === 'article' }"><input v-model="contentType" type="radio" value="article" /><b>图文</b><small>导入标题与图文正文</small></label>
        </fieldset>
        <button class="workbench-import-confirm" type="button" @click="confirm">导入{{ importedFieldsLabel }}到{{ targetLabel }}</button>
      </section>
    </div>
  </Teleport>
</template>
