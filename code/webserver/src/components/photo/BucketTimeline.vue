<!--
  BucketTimeline —— 按年代档分组的横向缩略图带（设计稿 §五「关键组件」表）
  ------------------------------------------------------------
  为什么按「年代档」而不是按年
  ------------------------------
  人脸跨年龄漂移是这个工具的**核心难点**（S0 实测：不分年代档时 FR 32.75%，
  分年代档后降到约 19%）。所以同一个人的脸在 1998 和 2020 是**两个年代档**，
  按年看会让人误以为「这两年他不在这库里」，按年代档看才是真实的分布。
  （设计稿 §4.5 也明确：时间轴按年代档分组，直观呈现跨年龄漂移。）

  两条硬规则
  ----------
  ① **空年代档不显示**：`ALL` 兜底年代档和没样本的年份在库里都存在，
     渲染出来就是一条永远 0 张的横带 —— 用户会以为加载失败。
  ② **年代档数自适应**：年代档少（≤ BUCKET_ROW_BREAKPOINT）时用「左标签 + 右横带」的
     行式，读起来像时间轴；年代档多时自动改成网格卡片，否则 20 个年代档会把页面
     拉成一条极长的窄带，每个年代档只放得下两三张图。

  年代档数是**后端给的**（pb_face.shotBucket / 人物详情的 buckets[]），
  这里不自己按年份造年代档 —— 造出来的年代档与库里口径不一致，跨页面一对就露馅。
-->
<script setup>
import { computed } from 'vue'
import PhotoThumb from '@/components/photo/PhotoThumb.vue'
import { formatBucketKey, formatCount, formatFileSize } from '@/utils/format'

const props = defineProps({
  /**
   * [{ bucketKey, photos: [photoSummary…], count? }]
   * count 缺省时取 photos.length。
   */
  groups: { type: Array, default: () => [] },
  /** 每个年代档最多渲染几张缩略图（其余折成「+N」，避免一个年代档刷 200 张图） */
  maxPerBucket: { type: Number, default: 12 },
  /** 是否显示文件名（横带里太挤，默认只给年份） */
  showName: { type: Boolean, default: false },
  /**
   * 各年代档的打包现状（**后端原样**，R15）：每项含 `timelineKey` / `photoTotal` /
   * `totalBytes` / `zipExists` / `zipSize` / `stale`。
   *
   * ⚠️ 匹配用的是 `timelineKey`（后端给的）而**不是** `bucketKey`：
   *    时间轴把「无拍摄年份」写作 `(无拍摄年份)`，打包侧要的是 `NOYEAR`。
   *    组件不翻译这两个键 —— 翻译一次就有一处会忘。
   */
  packBuckets: { type: Array, default: () => [] },
  /**
   * 当前打包任务快照（后端原样；`state='BUILDING'` 表示正在打）。
   * 后端**全局只允许一个**打包任务，所以只有一份、不是每档一份。
   */
  packTask: { type: Object, default: null },
})

const emit = defineEmits(['select', 'pack'])

/** 年代档数超过这个数就切网格布局 */
const BUCKET_ROW_BREAKPOINT = 6

/** 空年代档在这里就被剔掉 —— 组件内部不再判两次 */
const buckets = computed(() =>
  (props.groups || [])
    .map((group) => {
      const photos = Array.isArray(group.photos) ? group.photos : []
      return {
        bucketKey: group.bucketKey || '',
        label: formatBucketKey(group.bucketKey),
        photos,
        count: Number(group.count) || photos.length,
        pack: packOf(group.bucketKey),
      }
    })
    .filter((group) => group.count > 0 && group.photos.length > 0),
)

/** 时间轴档键 -> 打包项（找不到返回 null：这一档还没算过体积） */
function packOf(bucketKey) {
  const key = String(bucketKey || '')
  return (props.packBuckets || [])
    .find((one) => String(one.timelineKey || '') === key) || null
}

/** 正在打的是不是这一档 */
function packingOf(group) {
  const task = props.packTask
  if (!task || String(task.state || '') !== 'BUILDING') return null
  const pack = group.pack
  if (!pack || String(task.bucketKey || '') !== String(pack.bucketKey || '')) return null
  return task
}

/** 是否有别的档正在打（**全局单任务**：此时所有按钮都不能点） */
const busyElsewhere = computed(() => String(props.packTask?.state || '') === 'BUILDING')

/**
 * 按钮文案。四态各自不同 —— 用户扫一眼横带就知道「哪一档打过了、多大」。
 *
 * ⚠️ 「不显示体积」与「显示 0 B」是两回事：没打过时**什么都不给**，
 *    而不是给一个 `0 B`（那看起来像「打了个空包」）。
 */
function packTextOf(group) {
  const task = packingOf(group)
  if (task) return `${formatCount(task.processed)}/${formatCount(task.total)}`
  const pack = group.pack
  if (pack?.zipExists && pack.stale) return '重新打包'
  if (pack?.zipExists) return `下载 ${formatFileSize(pack.zipSize)}`
  return '打包 zip'
}

/**
 * 按钮下方的副标题：预计体积 / 已打包张数 / 已过期提示。
 *
 * 这里就是把「界面张数 ≠ 包内张数」讲清楚的那个地方（plan §6.3）：
 * 写出「排除重复件后 171 张」，用户才不会以为包打少了。
 */
function packHintOf(group) {
  const task = packingOf(group)
  if (task) return `已打包 ${formatCount(task.processed)}/${formatCount(task.total)} 张`
  const pack = group.pack
  if (pack?.zipExists && pack.stale) {
    return `原图有变动（现 ${formatCount(pack.photoTotal)} 张，包内 ${formatCount(pack.zipPhotoCount)} 张）`
  }
  if (pack?.zipExists) return `已打包 ${formatCount(pack.zipPhotoCount)} 张`
  if (!pack) return ''
  return `排除重复件后 ${formatCount(pack.photoTotal)} 张 · 约 ${formatFileSize(pack.totalBytes)}`
}

/** 点按钮：下载 / 重打 / 首次打包 由父组件决定（组件只管发意图） */
function emitPack(group) {
  const pack = group.pack
  emit('pack', {
    timelineKey: group.bucketKey,
    bucketKey: pack?.bucketKey || group.bucketKey,
    zipExists: Boolean(pack?.zipExists),
    stale: Boolean(pack?.stale),
    totalBytes: Number(pack?.totalBytes) || 0,
  })
}

const useGrid = computed(() => buckets.value.length > BUCKET_ROW_BREAKPOINT)

/** 每个年代档实际渲染的那几张 + 折叠掉的张数 */
function visibleOf(group) {
  const limit = Math.max(0, props.maxPerBucket)
  const shown = group.photos.slice(0, limit)
  return { shown, rest: group.photos.length - shown.length }
}
</script>

<template>
  <div v-if="!buckets.length" class="pb-hint">还没有可按年代分组的照片。</div>

  <!-- 年代档少：行式（左时间刻度 + 右横向缩略图带） -->
  <ul v-else-if="!useGrid" class="space-y-3">
    <li v-for="group in buckets" :key="group.bucketKey" class="flex gap-3">
      <div class="w-24 shrink-0 pt-1 text-right">
        <p class="text-caption font-medium tabular-nums text-ink">{{ group.label }}</p>
        <p class="text-caption tabular-nums text-ink-weak">{{ group.count }} 张</p>

        <!-- R15：按年代档打包 zip。**组件不发请求** —— 只 emit，
             由 PersonDetailView 走 store（与上面的 emit('select') 同一纪律） -->
        <button
          type="button"
          class="mt-1 rounded border border-line px-1.5 py-0.5 text-caption text-ink-sub hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="busyElsewhere && !packingOf(group)"
          :title="packHintOf(group)"
          @click="emitPack(group)"
        >
          {{ packTextOf(group) }}
        </button>
        <div
          v-if="packingOf(group)"
          class="mt-1 h-1 w-full overflow-hidden rounded bg-black/10"
          role="progressbar"
          :aria-valuenow="Number(packingOf(group).percent) || 0"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div class="h-1 rounded bg-sky-500" :style="{ width: `${Number(packingOf(group).percent) || 0}%` }" />
        </div>
        <p v-if="packHintOf(group)" class="mt-0.5 text-caption leading-tight text-ink-weak">
          {{ packHintOf(group) }}
        </p>
      </div>
      <ul class="flex flex-1 flex-wrap gap-2">
        <li v-for="photo in visibleOf(group).shown" :key="photo.photoCode" class="w-20">
          <button
            type="button"
            class="block w-full text-left"
            :aria-label="`${group.label} 年代的照片`"
            @click="emit('select', { photo, bucketKey: group.bucketKey })"
          >
            <PhotoThumb :photo="photo" :show-name="showName" thumb-size="200" />
          </button>
        </li>
        <li
          v-if="visibleOf(group).rest > 0"
          class="flex h-20 w-20 items-center justify-center rounded-thumb border border-dashed border-line text-caption tabular-nums text-ink-weak"
        >
          +{{ visibleOf(group).rest }}
        </li>
      </ul>
    </li>
  </ul>

  <!-- 年代档多：网格卡片（每卡一个年代档，横带内换行） -->
  <ul v-else class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
    <li v-for="group in buckets" :key="group.bucketKey" class="pb-card p-3">
      <div class="flex items-baseline justify-between gap-2">
        <p class="text-caption font-medium tabular-nums text-ink">{{ group.label }}</p>
        <p class="text-caption tabular-nums text-ink-weak">{{ group.count }} 张</p>
      </div>

      <!-- R15：按年代档打包 zip（与行式布局同一套文案与禁用规则） -->
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="rounded border border-line px-1.5 py-0.5 text-caption text-ink-sub hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="busyElsewhere && !packingOf(group)"
          :title="packHintOf(group)"
          @click="emitPack(group)"
        >
          {{ packTextOf(group) }}
        </button>
        <p v-if="packHintOf(group)" class="text-caption leading-tight text-ink-weak">
          {{ packHintOf(group) }}
        </p>
      </div>
      <div
        v-if="packingOf(group)"
        class="mt-1 h-1 w-full overflow-hidden rounded bg-black/10"
        role="progressbar"
        :aria-valuenow="Number(packingOf(group).percent) || 0"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <div class="h-1 rounded bg-sky-500" :style="{ width: `${Number(packingOf(group).percent) || 0}%` }" />
      </div>
      <ul class="mt-2 flex flex-wrap gap-2">
        <li v-for="photo in visibleOf(group).shown" :key="photo.photoCode" class="w-16">
          <button
            type="button"
            class="block w-full text-left"
            :aria-label="`${group.label} 年代的照片`"
            @click="emit('select', { photo, bucketKey: group.bucketKey })"
          >
            <PhotoThumb :photo="photo" :show-name="showName" thumb-size="200" />
          </button>
        </li>
        <li
          v-if="visibleOf(group).rest > 0"
          class="flex h-16 w-16 items-center justify-center rounded-thumb border border-dashed border-line text-caption tabular-nums text-ink-weak"
        >
          +{{ visibleOf(group).rest }}
        </li>
      </ul>
    </li>
  </ul>
</template>
