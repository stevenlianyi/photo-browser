<!--
  ShotYearFixDialog —— 「修正拍摄年代」（DR-42）
  ------------------------------------------------------------
  为什么需要这个入口
  ----------------
  老相册的翻拍件 / 扫描件，`pb_photo.shotYear` 来自 EXIF -> 文件名 -> mtime，
  三条兜底链给的都是**翻拍那一刻**。于是这张照片里的人脸全落进错误的年代档
  （年代档 = f(拍摄年, 该人出生年)）：2019 年翻拍的 1960 年代照片，
  一位 1938 年生的人会落在「2016-2025」，而它应当是「1956-1965」。

  两条硬纪律（与后端同源）
  --------------------
  ① **先看影响面再落库**：写入的是**照片级**年份，而年代档是按各人出生年现算的 ——
     一张合影里三个不同生日的人会朝三个方向变。所以预览区必须把 `persons[]`
     完整列出来（谁、从哪个年代档、到哪个年代档），而不是只给一个"将被修改"的提示。
  ② **「恢复自动」= 传 null**：它和"填一个年份"是同一列的两个取值。
     前端不能把它做成"清空输入框就提交"—— 空输入框的含义是"还没填"，
     两者混在一起，用户会把「清空」当成取消。

  为什么输入框不做 min/max
  ---------------------
  合法区间（SHOT_YEAR_MIN/MAX）只有一处真相，在后端 config 里。前端硬编一份
  会随配置改动而分叉，表现是「前端拦下了后端明明接受的年份」。所以这里只做
  "是不是一个整数"的形式校验，区间交给后端（400 + 明确消息，由调用方提示）。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { RotateCcw } from 'lucide-vue-next'
import { fixShotYear, getShotYearImpact } from '@/api/photoAction'
import { formatBucketKey } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 照片详情对象（需要 shotYear / shotYearExif / shotYearOverride / relPath） */
  photo: { type: Object, default: null },
  /** 提交成功后由父组件负责刷新（这里只报告"已改"） */
})
const emit = defineEmits(['update:modelValue', 'done'])

const yearText = ref('')
const preview = ref(null)
const loading = ref(false)
const submitting = ref(false)
/** 输入框里那串东西既不是空也不是整数（例如 "19x0"） */
const malformed = ref(false)

/** 输入框当前值 -> 目标年份（null = 恢复自动；NaN = 输入不合法） */
const targetYear = computed(() => {
  const text = String(yearText.value ?? '').trim()
  if (!text) return null
  const value = Number(text)
  if (!Number.isFinite(value) || !Number.isInteger(value)) return Number.NaN
  return value
})

const currentYear = computed(() => {
  const one = props.photo || {}
  return one.shotYear ?? null
})

const exifYear = computed(() => {
  const one = props.photo || {}
  return one.shotYearExif ?? one.shotYear ?? null
})

const hasOverride = computed(() => {
  const one = props.photo || {}
  return one.shotYearOverride !== null && one.shotYearOverride !== undefined
})

/** 照片上的脸一张都不会换年代档时，提交按钮就该是灰的（省掉一次空写库） */
const canSubmit = computed(
  () => !loading.value && !submitting.value && !malformed.value && !!preview.value?.changed,
)

let previewTimer = null

async function loadPreview() {
  const code = props.photo?.photoCode
  if (!code) return
  if (Number.isNaN(targetYear.value)) {
    malformed.value = true
    preview.value = null
    return
  }
  malformed.value = false
  loading.value = true
  try {
    preview.value = await getShotYearImpact(code, targetYear.value)
  } catch (error) {
    preview.value = null
    ElMessage.error(error?.message || '预览失败')
  } finally {
    loading.value = false
  }
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(loadPreview, 250)
}

watch(yearText, schedulePreview)

watch(
  () => props.modelValue,
  (opened) => {
    if (!opened) return
    // 打开时用**当前有效年**预填：用户十有八九只是想把它挪几年
    yearText.value = currentYear.value === null ? '' : String(currentYear.value)
    preview.value = null
    malformed.value = false
    loadPreview()
  },
)

function restoreAuto() {
  yearText.value = ''
  loadPreview()
}

function close() {
  emit('update:modelValue', false)
}

async function submit() {
  const code = props.photo?.photoCode
  if (!code || !canSubmit.value) return
  submitting.value = true
  try {
    const data = await fixShotYear(code, targetYear.value, true)
    const moved = Number(data?.bucketsChanged || 0)
    ElMessage.success(
      data?.newOverride == null
        ? '已恢复自动：年代回到扫描时读到的年份'
        : `年代已修正为 ${data.effectiveYear} 年（${moved} 张人脸重刷年代档）`,
    )
    emit('done', data)
    close()
  } catch (error) {
    ElMessage.error(error?.message || '修正失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="修正拍摄年代"
    width="560px"
    @update:model-value="(value) => emit('update:modelValue', value)"
  >
    <div class="space-y-4">
      <p class="pb-hint">
        修正的是<b>这张照片的拍摄年代</b>。年代档 = f(有效拍摄年, 各人出生年) 现算，
        所以同一张合影里每个人的年代档会各自变化（见下方清单）。 扫描器读到的原始年份
        <b>不会被改写</b>，随时可以「恢复自动」。
      </p>

      <dl class="space-y-1.5 text-body">
        <div class="flex justify-between gap-3">
          <dt class="text-ink-weak">扫描时读到的年份</dt>
          <dd class="tabular-nums text-ink">
            {{ exifYear ?? '未读到' }}
            <span class="text-caption text-ink-weak">（EXIF / 文件名 / 文件时间）</span>
          </dd>
        </div>
        <div class="flex justify-between gap-3">
          <dt class="text-ink-weak">当前使用的年代</dt>
          <dd class="tabular-nums text-ink">
            {{ currentYear ?? '未读到' }}
            <span v-if="hasOverride" class="ml-1 text-caption text-brand-ink">人工修正</span>
          </dd>
        </div>
      </dl>

      <div class="flex items-end gap-2">
        <label class="flex-1">
          <span class="block text-caption text-ink-weak">修正为</span>
          <el-input
            v-model="yearText"
            placeholder="例如 1960"
            inputmode="numeric"
            clearable
            class="mt-1"
          />
        </label>
        <el-button :disabled="!hasOverride" @click="restoreAuto">
          <RotateCcw class="mr-1 h-4 w-4" aria-hidden="true" />恢复自动
        </el-button>
      </div>
      <p v-if="malformed" class="text-caption text-danger-ink">请输入一个 4 位年份（如 1960）。</p>
      <p v-else-if="yearText === ''" class="text-caption text-ink-weak">
        输入框为空 = 恢复自动（回到扫描时读到的年份）。
      </p>

      <!-- 影响面：必须列出「谁、从哪个年代档、到哪个年代档」 -->
      <div v-if="loading" class="pb-hint">正在计算受影响的年代档…</div>
      <div v-else-if="preview" class="rounded-btn bg-surface px-3 py-2">
        <p v-if="!preview.changed" class="text-caption text-ink-sub">
          与当前年代相同，提交不会有任何改动。
        </p>
        <template v-else>
          <p class="text-caption text-ink-sub">
            这张照片共 {{ preview.faces }} 张人脸，其中
            <b>{{ preview.affectedFaces }}</b> 张的年代档会变：
          </p>
          <ul class="mt-2 space-y-1.5">
            <li v-for="one in preview.persons" :key="one.personCode || 'unassigned'">
              <p class="text-caption text-ink">
                {{ one.displayName }}
                <span v-if="one.birthday" class="text-ink-weak">（{{ one.birthday }}）</span>
                <span class="text-ink-weak"> · {{ one.faces }} 张脸</span>
              </p>
              <p class="text-caption tabular-nums text-ink-sub">
                {{ (one.oldBuckets || []).map(formatBucketKey).join('、') || '无拍摄年份' }}
                <span aria-hidden="true">→</span>
                {{ (one.newBuckets || []).map(formatBucketKey).join('、') || '无拍摄年份' }}
              </p>
            </li>
          </ul>
          <p class="mt-2 text-caption text-ink-weak">{{ preview.note }}</p>
        </template>
      </div>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
        保存并重算年代档
      </el-button>
    </template>
  </el-dialog>
</template>
