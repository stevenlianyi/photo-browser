<!--
  PlaceRenameDialog —— 「重命名地点」（DR-52 · B）
  ------------------------------------------------------------
  改的是 `pb_place.nameZh`（**显示名**），只此一列：
    · `placeCode` / `placeName` 一个字都不动（`placeName` 是 placeCode 的派生源，
      动它键就漂移 —— DR-32 明确列为风险）；
    · `nameZh` 不在 `REBUILD_COLUMNS` 里（DR-28）-> rebuild **绝不覆盖**，
      改它就是最终值，所以本操作**不需要**重建字典。

  ⚠️ 两个连带（都要在界面上说清楚，否则用户会以为"改坏了"）：
    ① `nameZh` 是**归并键** —— `GET /places?groupByNameZh=1` 按它归并，
       改名会改变分组（要点：归并项里改的是**主编码**那一行的名字）。
    ② `resolvePlaceFilter()` 优先按 `nameZh` 匹配 —— 改名后按**新名字**筛选命中、
       按旧名字不再命中。
  ⚠️ 输入框的 placeholder 显示 `placeName`（聚合键，**清除后要回落到的那一个值**）：
     让人一眼知道「清除」之后会看到什么，而不是清完看到一个没预期的名字。
     ⚠️ 这里**刻意不用 `placeRawName()`**：它在"显示名与 placeName 相同时"
       会返回空串（那是给 tooltip 去重用的口径）—— 而本对话框恰恰需要
       「清除后回落成什么」这个答案，哪怕两者当前相同。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { placeDisplayName, renamePlace } from '@/api/place'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 地点详情对象（需要 placeCode / placeName / nameZh / placeCodes） */
  place: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'done'])

const text = ref('')
const submitting = ref(false)

const placeCode = computed(() => String(props.place?.placeCode || ''))
/** 聚合键（= 清除显示名之后界面会回落到的那一个值） */
const rawName = computed(() => String(props.place?.placeName || ''))
const currentText = computed(() => placeDisplayName(props.place))
/** 归并了多处时才提示"只改主编码" */
const merged = computed(() => Number(props.place?.placeCodes?.length || 0) > 1)
/**: 已经是人工命名（`nameSource === 'manual'`）—— 提示一句"已经是" */
const isManual = computed(() => String(props.place?.nameSource || '') === 'manual')

const canSubmit = computed(() => Boolean(placeCode.value) && !submitting.value)

watch(
  () => props.modelValue,
  (opened) => {
    if (!opened) return
    // 初值 = 当前 nameZh（没有就留空 —— 让人直接开始输入）
    text.value = props.place?.nameZh ? String(props.place.nameZh) : ''
  },
)

function clearName() {
  text.value = ''
}

function close() {
  emit('update:modelValue', false)
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const value = String(text.value ?? '').trim() || null
    const data = await renamePlace(placeCode.value, value)
    ElMessage.success(
      value == null
        ? '已清除显示名，界面回落到英文聚合键'
        : `显示名已改为「${value}」`,
    )
    emit('done', data)
    close()
  } catch (error) {
    ElMessage.error(error?.message || '重命名失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="重命名地点"
    width="520px"
    @update:model-value="(value) => emit('update:modelValue', value)"
  >
    <div class="space-y-4">
      <p class="pb-hint">
        只改这个地点的<b>显示名</b>（`nameZh`），地点编码与英文聚合键一个字都不动，
        也不会被重建字典覆盖。当前显示：<b>{{ currentText }}</b>
      </p>

      <div class="flex items-end gap-2">
        <label class="flex-1">
          <span class="block text-caption text-ink-weak">显示名</span>
          <el-input
            v-model="text"
            class="mt-1"
            clearable
            :placeholder="rawName || '（无英文聚合键）'"
          />
        </label>
        <el-button @click="clearName">清除</el-button>
      </div>
      <p class="text-caption text-ink-weak">
        留空 = 清除，界面回落到
        <code>{{ rawName || '原英文聚合键' }}</code>。
      </p>
      <p class="text-caption text-ink-weak">
        保存后这个名字会被标记为<b>人工命名</b>（`nameSource=manual`）——
        重建地址字典时**不会再被区县名覆盖**；「清除」同样是人工决定，
        清除后界面固定回落英文聚合键，不会被自动回填。
        <span v-if="isManual">当前已经是人工命名。</span>
      </p>
      <p v-if="merged" class="text-caption text-warning-ink">
        这个地点当前归并了 {{ place.placeCodes.length }} 处（同名地点）——
        本次只改<b>主编码</b>那一行的显示名，归并分组可能因此变化。
      </p>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>
