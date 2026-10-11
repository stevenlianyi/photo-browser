<!--
  PlaceFixDialog —— 「修正地点」（DR-52）
  ------------------------------------------------------------
  为什么需要这个入口
  ----------------
  某张照片的地点是错的：GPS 漂了几十公里、目录名只是"大概那一片"，或者那批照片
  干脆没有地点线索。用户看着一张照片一眼能说出"这是外婆家"，而库里没有一处能
  记下这句话 —— 这个对话框就是那句话的落点。

  三条硬纪律（与后端同源）
  --------------------
  ① **先看影响面再落库**：提交会**重建整个地点字典**（一条全量 GROUP BY），
     所以预览区必须把「纽约 98 → 97 / 华盛顿 114 → 115」以及"那个地点会变成 0 张"
     说清楚，而不是只给一个"将被修改"。
  ② **「恢复自动」= 传 null / 空串**：它和"填一个地点名"是同一列的两个取值。
     输入框为空 = 恢复自动（跟目录名 / 逆地理的自动值），而不是"还没填"。
  ③ **提交后地点字典已重建**（后端在响应里回 rebuildSummary / rebuildElapsedMs），
     所以要提示一句，并让父组件刷新地点相关视图。

  ⚠️ 候选下拉的**值**是「英文聚合键」（`placeName`），不是中文显示名 ——
     `placeNameOverride` 最终要参与聚合键比较，写中文显示名会与
     `pb_place.placeName` 对不上（界面看着对、地点列表里却少一张）。
     标签才显示中文（`placeDisplayName`）+ 张数。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { RotateCcw } from 'lucide-vue-next'
import { fixPlace, getPlaceFixImpact } from '@/api/photoAction'
import { listPlaces, placeDisplayName } from '@/api/place'
import { formatCount } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 照片详情对象（需要 photoCode / placeName / placeAutoName / placeIsOverride） */
  photo: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'done'])

const nameText = ref('')
const preview = ref(null)
const loading = ref(false)
const submitting = ref(false)
/** 候选地点（`GET /places?groupByNameZh=1`，按张数倒序） */
const candidates = ref([])

const currentKey = computed(() => String(props.photo?.placeName || ''))
const currentText = computed(() => placeDisplayName(props.photo))
/** 未修正时的自动值（「自动：纽约」那一行）：优先用候选表里的中文名 */
const autoText = computed(() => {
  const key = String(props.photo?.placeAutoName || '')
  if (!key) return ''
  return labelOf(key)
})
const hasOverride = computed(() => Boolean(props.photo?.placeIsOverride))

/** 输入框 -> 提交值（去首尾空白；空 = 恢复自动，与后端「空串/null = 恢复自动」一致） */
const targetValue = computed(() => String(nameText.value ?? '').trim() || null)

const canSubmit = computed(
  () => !loading.value && !submitting.value && !!preview.value?.changed,
)

const currentSourceText = computed(() => {
  const one = preview.value?.current?.source
  return {
    override: '人工修正',
    dir: '目录名线索',
    gps: '逆地理',
    none: '无地点线索',
  }[one] || ''
})

/** 聚合键 -> 界面名字（候选表里有就显示中文名，没有就显示键本身） */
function labelOf(placeKey, nameZh = '') {
  if (nameZh) return nameZh
  const key = String(placeKey || '')
  if (!key) return '（未设置）'
  const hit = candidates.value.find((one) => String(one.placeName || '') === key)
  return hit ? placeDisplayName(hit) : key
}

let previewTimer = null

async function loadCandidates() {
  try {
    const body = await listPlaces({
      groupByNameZh: 1, page: 1, size: 200, orderBy: 'photoCount', desc: 1,
    })
    candidates.value = body?.items ?? []
  } catch {
    candidates.value = []
  }
}

async function loadPreview() {
  const code = props.photo?.photoCode
  if (!code) return
  loading.value = true
  try {
    preview.value = await getPlaceFixImpact(code, targetValue.value)
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

watch(nameText, schedulePreview)

watch(
  () => props.modelValue,
  (opened) => {
    if (!opened) return
    // 打开时预填**当前聚合键**（下拉里若有同名选项，会显示成中文标签）
    nameText.value = currentKey.value
    preview.value = null
    loadCandidates()
    loadPreview()
  },
)

/** 恢复自动：清空输入框 -> 预览与提交都会写 null（回到自动值） */
function restoreAuto() {
  nameText.value = ''
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
    const data = await fixPlace(code, targetValue.value, true)
    const ms = Number(data?.rebuildElapsedMs || 0)
    ElMessage.success(
      targetValue.value == null
        ? `已恢复自动：地点回到「${autoText.value || '原值'}」，地点字典已重建（${ms} ms）`
        : `地点已改为「${labelOf(data?.target?.placeKey, data?.target?.nameZh)}」，`
          + `地点字典已重建（${ms} ms）`,
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
    title="修正地点"
    width="560px"
    @update:model-value="(value) => emit('update:modelValue', value)"
  >
    <div class="space-y-4">
      <p class="pb-hint">
        修正的是<b>这张照片的地点</b>。它写进 `placeNameOverride`，目录名线索与
        逆地理结果都<b>不改写</b>，随时可以「恢复自动」。提交后会重建地点字典，
        让地点列表的张数立刻正确。
      </p>

      <dl class="space-y-1.5 text-body">
        <div class="flex justify-between gap-3">
          <dt class="text-ink-weak">当前地点</dt>
          <dd class="text-ink">
            {{ currentText }}
            <span v-if="currentSourceText" class="ml-1 text-caption text-ink-weak">
              （{{ currentSourceText }}）
            </span>
          </dd>
        </div>
        <div v-if="hasOverride && autoText" class="flex justify-between gap-3">
          <dt class="text-ink-weak">自动</dt>
          <dd class="text-ink-sub">{{ autoText }}</dd>
        </div>
      </dl>

      <div class="flex items-end gap-2">
        <label class="flex-1">
          <span class="block text-caption text-ink-weak">修正为</span>
          <el-select
            v-model="nameText"
            class="mt-1 w-full"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="选一个地点，或直接输入新名字（如 外婆家）"
          >
            <el-option
              v-for="one in candidates"
              :key="one.placeCode"
              :value="one.placeName"
              :label="`${one.nameZh || one.placeName} · ${formatCount(one.photoCount)} 张`"
            />
          </el-select>
        </label>
        <el-button :disabled="!hasOverride" @click="restoreAuto">
          <RotateCcw class="mr-1 h-4 w-4" aria-hidden="true" />恢复自动
        </el-button>
      </div>
      <p v-if="nameText === ''" class="text-caption text-ink-weak">
        输入框为空 = 恢复自动（回到目录名 / 逆地理的自动值）。
      </p>
      <p v-else class="text-caption text-ink-weak">
        下拉里没有的地点名可以直接输入（例如「外婆家」），提交后会新建一个地点，
        并记为<b>人工命名</b>（重建地址字典时不会被区县名覆盖）。
      </p>

      <!-- 影响面：必须说清两个地点的张数怎么变 -->
      <div v-if="loading" class="pb-hint">正在计算影响面…</div>
      <div v-else-if="preview" class="rounded-btn bg-surface px-3 py-2">
        <p v-if="!preview.changed" class="text-caption text-ink-sub">
          与当前地点相同，提交不会有任何改动。
        </p>
        <template v-else>
          <p class="text-caption tabular-nums text-ink">
            {{ labelOf(preview.counts.from.placeKey) }}
            {{ formatCount(preview.counts.from.photoCount) }} →
            <b>{{ formatCount(preview.counts.from.after) }}</b>
            <span aria-hidden="true">　</span>
            {{ labelOf(preview.counts.to.placeKey, preview.target.nameZh) }}
            {{ formatCount(preview.counts.to.photoCount) }} →
            <b>{{ formatCount(preview.counts.to.after) }}</b>
          </p>
          <p v-if="!preview.target.exists" class="mt-1 text-caption text-brand-ink">
            「{{ labelOf(preview.target.placeKey, preview.target.nameZh) }}」还不存在，
            这会<b>新建一个地点</b>，并记为<b>人工命名</b>
            —— 重建地址字典时不会被区县名覆盖。
          </p>
          <p v-if="preview.fromBecomesEmpty" class="mt-1 text-caption text-warning-ink">
            「{{ labelOf(preview.counts.from.placeKey) }}」将变为 0 张
            （地点行会保留，计数归零）。
          </p>
          <p v-if="preview.restoreAuto" class="mt-1 text-caption text-ink-sub">
            将清除人工修正，回到自动值
            <b>{{ labelOf(preview.autoPlaceKey) || '（无）' }}</b>。
          </p>
        </template>
      </div>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!canSubmit"
        @click="submit"
      >
        {{ submitting ? '正在重建地点字典…' : '保存并重建地点字典' }}
      </el-button>
    </template>
  </el-dialog>
</template>
