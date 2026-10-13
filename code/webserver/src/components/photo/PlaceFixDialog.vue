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

  ⚠️ 候选下拉由 `PlaceNamePicker` 提供（R13 · DR-54），值可能是两类：
     ① 已有地点 -> **英文聚合键**（`placeName`）—— `placeNameOverride` 要参与聚合键比较，
        写中文显示名会与 `pb_place.placeName` 对不上（界面看着对、地点列表里却少一张）；
     ② 行政区词表 -> **规范中文名**（`北京市 · 朝阳区`）—— 后端会同时把它写进该行的
        `nameZh`，于是与同区县的 GPS 行**归并成一项**（一次只能写一个字符串，
        而"整个朝阳区"在库里是 8 个键）。

  ⚠️ 影响面的数字一律是**项级**（归并后），不是键级
  -------------------------------------------------
    「北京市 · 朝阳区」在列表里可能是 8 个街道键的合计（2566 张）。弹窗报键级
    就会变成「0 → 1」而列表说「2566 → 2567」—— 两处各自自洽、都不报错。
    ⇒ 名字与数字都从后端的 `counts.*`（项级）取，前端**不再自己查候选表**。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { RotateCcw } from 'lucide-vue-next'
import { fixPlace, getPlaceFixImpact } from '@/api/photoAction'
import { placeDisplayName } from '@/api/place'
import { formatCount } from '@/utils/format'
import PlaceNamePicker from '@/components/common/PlaceNamePicker.vue'

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

const currentKey = computed(() => String(props.photo?.placeName || ''))
const currentText = computed(() => placeDisplayName(props.photo))
/** 未修正时的自动值（「自动：华盛顿」那一行）—— 名字由后端给（`autoNameZh`） */
const autoText = computed(() => {
  const side = {
    nameZh: preview.value?.autoNameZh,
    placeKey: preview.value?.autoPlaceKey || props.photo?.placeAutoName,
  }
  return sideText(side)
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

/**
 * 一侧（from / to / 自动值）的界面名字：**后端给的 `nameZh` 优先**，没有才回落键本身。
 *
 * ⚠️ 不要在前端拿候选表反查 —— 那个表只覆盖"库里有行 + 数据集"两类，
 *    两者都不含的键会被显示成英文，而它恰好是"清空会回到哪里"的唯一依据。
 */
function sideText(side) {
  const nameZh = String(side?.nameZh || '').trim()
  if (nameZh) return nameZh
  const key = String(side?.placeKey || '').trim()
  return key || '（未设置）'
}

let previewTimer = null

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
        : `地点已改为「${sideText(data?.counts?.to)}」，`
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
        <div class="flex-1">
          <span class="block text-caption text-ink-weak">修正为</span>
          <div class="mt-1">
            <PlaceNamePicker
              v-model="nameText"
              placeholder="选一个地点 / 行政区，或直接输入新名字（如 外婆家）"
            />
          </div>
        </div>
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

      <!-- 影响面：必须说清两个**项**的张数怎么变（项 = 同中文名归并后的那一行） -->
      <div v-if="loading" class="pb-hint">正在计算影响面…</div>
      <div v-else-if="preview" class="rounded-btn bg-surface px-3 py-2">
        <p v-if="!preview.changed" class="text-caption text-ink-sub">
          与当前地点相同，提交不会有任何改动。
        </p>
        <template v-else>
          <p class="text-caption tabular-nums text-ink">
            {{ sideText(preview.counts.from) }}
            {{ formatCount(preview.counts.from.photoCount) }} →
            <b>{{ formatCount(preview.counts.from.after) }}</b>
            <span aria-hidden="true">　</span>
            {{ sideText(preview.counts.to) }}
            {{ formatCount(preview.counts.to.photoCount) }} →
            <b>{{ formatCount(preview.counts.to.after) }}</b>
          </p>
          <!-- ⚠️ 第一眼状态（打开弹窗预填了当前键）：什么都没挪，只是"记为人工修正" -->
          <p v-if="preview.sameKey" class="mt-1 text-caption text-ink-sub">
            与当前地点<b>相同</b>：会把这张照片的地点记为<b>人工修正</b>
            （张数不变；以后重扫 / 逆地理都不会再改动它）。
          </p>
          <!-- ⚠️ 同一个归并项：键变了、项没变 —— 张数一个都不动，必须说清楚，
               否则用户看到"改了地点却什么都没变"会以为没生效 -->
          <p v-else-if="preview.sameGroup" class="mt-1 text-caption text-ink-sub">
            两边是<b>同一项</b>（同一个区县下归并在一起）—— 张数不变，
            只是把这张照片的地点键<b>规范成</b>「{{ sideText(preview.counts.to) }}」。
          </p>
          <p v-else-if="preview.target.groupExistingRowCount > 0" class="mt-1 text-caption text-brand-ink">
            将<b>并入</b>「{{ sideText(preview.counts.to) }}」这一项
            —— 它当前由 {{ preview.target.groupExistingRowCount }} 处地点归并而成。
          </p>
          <p v-else-if="!preview.target.exists" class="mt-1 text-caption text-brand-ink">
            「{{ sideText(preview.counts.to) }}」还不存在，这会<b>新建一个地点</b>，
            并记为<b>人工命名</b> —— 重建地址字典时不会被区县名覆盖。
          </p>
          <p v-if="preview.target.isDistrict" class="mt-1 text-caption text-ink-weak">
            这是<b>行政区规范名</b>（与 GPS 算出来的中文名同一份词表），
            因此会与同区县的地点归并成一项。
          </p>
          <p v-if="preview.fromBecomesEmpty" class="mt-1 text-caption text-warning-ink">
            「{{ sideText(preview.counts.from) }}」将变为 0 张
            （地点行会保留，计数归零）。
          </p>
          <p v-if="preview.restoreAuto" class="mt-1 text-caption text-ink-sub">
            将清除人工修正，回到自动值
            <b>{{ autoText || '（无）' }}</b>。
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
