<script setup>
import { computed, onBeforeUnmount, onMounted, ref, useId } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  options: { type: Array, default: () => [] },
  placeholder: { type: String, default: '请选择' },
  ariaLabel: { type: String, required: true },
})

const emit = defineEmits(['update:modelValue'])
const root = ref(null)
const open = ref(false)
const listboxId = `ai-copy-dropdown-${useId()}`
const selectedLabel = computed(() => (
  props.options.find((option) => option.value === props.modelValue)?.label || props.placeholder
))

function toggle() {
  open.value = !open.value
}

function showOptions() {
  open.value = true
}

function closeOptions() {
  open.value = false
}

function choose(value) {
  emit('update:modelValue', value)
  closeOptions()
}

function closeOnOutsidePointer(event) {
  if (root.value && !root.value.contains(event.target)) closeOptions()
}

onMounted(() => document.addEventListener('pointerdown', closeOnOutsidePointer))
onBeforeUnmount(() => document.removeEventListener('pointerdown', closeOnOutsidePointer))
</script>

<template>
  <div ref="root" class="ai-copy-dropdown" :class="{ open }">
    <button
      :aria-controls="listboxId"
      :aria-expanded="open"
      :aria-label="ariaLabel"
      aria-haspopup="listbox"
      class="ai-copy-dropdown-trigger"
      type="button"
      @click="toggle"
      @keydown.down.prevent="showOptions"
      @keydown.esc.prevent="closeOptions"
    >
      <span>{{ selectedLabel }}</span>
      <svg aria-hidden="true" viewBox="0 0 16 16"><path d="m3.5 6 4.5 4 4.5-4" /></svg>
    </button>
    <div v-if="open" :id="listboxId" class="ai-copy-dropdown-options" role="listbox">
      <button
        v-for="option in options"
        :key="option.value"
        :aria-selected="option.value === modelValue"
        :class="{ selected: option.value === modelValue }"
        role="option"
        type="button"
        @click="choose(option.value)"
      >
        <span>{{ option.label }}</span>
        <b aria-hidden="true">{{ option.value === modelValue ? '✓' : '' }}</b>
      </button>
    </div>
  </div>
</template>
