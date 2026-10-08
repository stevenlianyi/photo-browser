<!--
  P-01 概览（设计稿 §4.2）
  ------------------------------------------------------------
  统计卡 ×4 ｜ **待确认 + 我不同意** 两个入口 ｜ 最近入库缩略图行 ｜ 扫描状态条

  为什么概览页必须同时给「我不同意」的入口
  ---------------------------------------
    「待确认」是没归属的脸；「我不同意」是**机器自动认的、还没人确认**的脸。
    后者才是用户浏览时最该处理的（那里全是错分的风险），
    而它默认是 0（质心只用确认样本，冷启动时无自动归属），
    所以必须带着一句解释出现，否则用户看到 0 会以为功能坏了。
-->
<script setup>
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Camera,
  CircleCheck,
  Clock,
  Images,
  Plus,
  ScanLine,
  TriangleAlert,
  Users,
} from 'lucide-vue-next'
import { getOverview, listPhotos } from '@/api/browse'
import { thumbUrl } from '@/api/static'
import { formatCount, formatDateTime, formatYear } from '@/utils/format'
// 进照片详情要**声明来源**：详情页的「返回」才知道该回哪（见 utils/photoReturn.js）
import { photoDetailLink } from '@/utils/photoReturn'
import { JOB_STATUS_META } from '@/store/scan'
import { useReviewStore } from '@/store/review'

const review = useReviewStore()

const overview = ref(null)
const recent = ref([])
const loading = ref(true)
const error = ref('')

const STATUS_TONE = {
  neutral: 'text-ink-sub',
  info: 'text-info-ink',
  warning: 'text-warning-ink',
  success: 'text-success-ink',
  danger: 'text-danger-ink',
}

const jobMeta = computed(() =>
  JOB_STATUS_META[overview.value?.latestJob?.jobStatus] || JOB_STATUS_META.IDLE,
)

const stats = computed(() => {
  const one = overview.value || {}
  return [
    {
      key: 'photo',
      label: '照片总数',
      value: formatCount(one.photoCount),
      unit: '张',
      icon: Images,
      tone: 'text-ink',
    },
    {
      key: 'person',
      label: '人物',
      value: formatCount(one.personCount),
      unit: '人',
      icon: Users,
      tone: 'text-ink',
    },
    {
      key: 'face',
      label: '已人工确认的脸',
      value: formatCount(one.confirmedCount),
      unit: '张',
      icon: CircleCheck,
      tone: 'text-success-ink',
      hint: '只有这些脸会进入质心 —— 确认得越多，自动归属越准',
    },
    {
      key: 'stranger',
      label: '已排除的陌生人',
      value: formatCount(one.strangerCount),
      unit: '张',
      icon: TriangleAlert,
      tone: 'text-ink-sub',
    },
  ]
})

/** 待确认进度：已完成 = 已确认 + 已排除（这两个都是「处理完了」的意思） */
const pendingDone = computed(
  () =>
    (Number(overview.value?.confirmedCount) || 0)
    + (Number(overview.value?.strangerCount) || 0),
)
const pendingTotal = computed(
  () => pendingDone.value + (Number(overview.value?.pendingCount) || 0),
)
const pendingPercent = computed(() =>
  pendingTotal.value ? Math.round((pendingDone.value / pendingTotal.value) * 100) : 0,
)

/** 「我不同意」为空时的解释（DR-16③ 的冷启动语义） */
const disputedEmptyReason = computed(() => {
  if ((Number(overview.value?.confirmedCount) || 0) < 3) {
    return '质心只由**人工确认**的样本生成；确认不足 3 张时该人不参与自动匹配，'
      + '所以「我不同意」这一栏现在是空的 —— 这是正确行为，不是故障。'
  }
  return '确认过一些脸之后，跑一次匹配就会出现「机器认的、还没你确认」的那些脸。'
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [one, photos] = await Promise.all([
      getOverview(),
      listPhotos({ page: 1, size: 10, orderBy: 'recID', desc: 1 }),
    ])
    overview.value = one
    recent.value = photos?.items ?? []
    // 角标也一起刷新：别的页面确认过之后回到概览，数字必须已经变了
    await review.fetchBadge()
  } catch (e) {
    error.value = e?.message || '读取概览失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="pb-page space-y-6">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">概览</h2>
        <p class="pb-hint mt-1">本机照片库的当前状态：照片、人物、待确认与扫描进度。</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <el-button :loading="loading" @click="load">刷新</el-button>
        <RouterLink to="/scan-jobs">
          <el-button type="primary">
            <Plus class="mr-1 h-4 w-4" aria-hidden="true" />
            新建扫描
          </el-button>
        </RouterLink>
      </div>
    </div>

    <p
      v-if="error"
      class="flex items-start gap-2 rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink"
      role="alert"
    >
      <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        {{ error }}
        <el-button size="small" text @click="load">重试</el-button>
      </span>
    </p>

    <!-- 统计卡 ×4 -->
    <section aria-label="库状态统计" class="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <article v-for="card in stats" :key="card.key" class="pb-card p-4">
        <div class="flex items-center gap-2">
          <component :is="card.icon" class="h-4 w-4 text-ink-weak" aria-hidden="true" />
          <h3 class="text-body text-ink-sub">{{ card.label }}</h3>
        </div>
        <p class="mt-2 flex items-baseline gap-1">
          <span class="text-title tabular-nums" :class="card.tone">{{ card.value }}</span>
          <span class="text-caption text-ink-weak">{{ card.unit }}</span>
        </p>
        <p v-if="card.hint" class="pb-hint mt-1">{{ card.hint }}</p>
        <div v-else-if="loading" class="mt-2 h-3 w-20 animate-pulse rounded-btn bg-skeleton" />
      </article>
    </section>

    <!-- 两个队列：待确认 / 我不同意（**分两个入口，不合并**） -->
    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <section class="pb-card p-4" aria-labelledby="ov-pending-title">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h3 id="ov-pending-title" class="pb-section-title">待确认队列</h3>
          <RouterLink to="/review">
            <el-button size="small">去处理</el-button>
          </RouterLink>
        </div>
        <p class="mt-2 text-body text-ink-sub">
          剩余 <span class="font-medium text-warning-ink">{{ formatCount(overview?.pendingCount) }}</span>
          条 · 已处理 {{ formatCount(pendingDone) }} 条
        </p>
        <div
          class="mt-3 h-2 w-full overflow-hidden rounded-btn bg-surface"
          role="progressbar"
          :aria-valuenow="pendingPercent"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-label="待确认处理进度"
        >
          <div class="h-full bg-brand" :style="{ width: `${pendingPercent}%` }" />
        </div>
        <p class="pb-hint mt-2">
          「待确认」是<b>还没有归属</b>的脸（库里没这个人，或相似度不够）。它们同时进聚类，
          所以不处理也不会「消失」，但也不会自己认出来。
        </p>
      </section>

      <section class="pb-card p-4" aria-labelledby="ov-disputed-title">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h3 id="ov-disputed-title" class="pb-section-title flex items-center gap-1.5">
            <span aria-hidden="true">◐</span>
            我不同意
          </h3>
          <RouterLink to="/review">
            <el-button size="small" :disabled="!overview?.disputedCount">去否决</el-button>
          </RouterLink>
        </div>
        <p class="mt-2 text-body text-ink-sub">
          <span class="font-medium text-warning-ink">{{ formatCount(overview?.disputedCount) }}</span>
          条 · 机器认的、还没你确认过的脸
        </p>
        <p
          v-if="!overview?.disputedCount"
          class="mt-3 rounded-btn bg-surface px-3 py-2 text-caption text-ink-sub"
        >
          {{ disputedEmptyReason }}
        </p>
        <p v-else class="mt-3 text-caption text-ink-sub">
          这一栏是<b>纠错的主入口</b>：浏览照片时看到认错的人，就去这里否决 ——
          比在几百张照片里一张张找要快得多。
        </p>
      </section>
    </div>

    <!-- 最近入库 -->
    <section class="pb-card p-4" aria-labelledby="ov-recent-title">
      <div class="flex items-center justify-between gap-3">
        <h3 id="ov-recent-title" class="pb-section-title">最近入库</h3>
        <RouterLink to="/photos" class="text-caption text-brand-ink hover:underline">
          查看全部
        </RouterLink>
      </div>
      <ul v-if="loading" class="mt-3 grid grid-cols-5 gap-2 sm:grid-cols-6 md:grid-cols-10">
        <li v-for="n in 10" :key="n">
          <div class="pb-photo-frame aspect-square animate-pulse bg-skeleton" />
        </li>
      </ul>
      <p v-else-if="!recent.length" class="pb-hint mt-3">
        库里还没有照片。到
        <RouterLink class="text-brand-ink hover:underline" to="/scan-jobs">扫描任务</RouterLink>
        建一个任务，原图只读、扫描不改任何文件。
      </p>
      <ul v-else class="mt-3 grid grid-cols-5 gap-2 sm:grid-cols-6 md:grid-cols-10">
        <li v-for="photo in recent" :key="photo.photoCode">
          <RouterLink
            :to="photoDetailLink(photo.photoCode, { path: '/', name: '概览' })"
            class="block"
            :aria-label="`${photo.relPath}，${formatYear(photo)} 年，${photo.faceCount} 张人脸`"
          >
            <span class="pb-photo-frame relative block aspect-square">
              <img
                :src="thumbUrl(photo.photoCode, 200)"
                :alt="photo.relPath"
                class="h-full w-full object-cover"
                loading="lazy"
                decoding="async"
              />
              <span
                class="absolute inset-x-0 bottom-0 flex items-center justify-between gap-1 px-1 pb-0.5 text-caption text-ink-invert"
                style="background: linear-gradient(transparent, rgba(0, 0, 0, 0.55))"
              >
                <span class="tabular-nums">{{ formatYear(photo) }}</span>
                <span v-if="photo.faceCount > 0" class="inline-flex items-center gap-0.5">
                  <Camera class="h-3 w-3" aria-hidden="true" />{{ photo.faceCount }}
                </span>
              </span>
            </span>
          </RouterLink>
        </li>
      </ul>
    </section>

    <!-- 扫描状态条 -->
    <section class="pb-card p-4" aria-labelledby="ov-scan-title">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h3 id="ov-scan-title" class="pb-section-title">扫描状态</h3>
        <RouterLink to="/scan-jobs" class="text-caption text-brand-ink hover:underline">
          扫描任务
        </RouterLink>
      </div>

      <div v-if="overview?.latestJob" class="mt-3">
        <dl class="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt class="pb-hint">任务</dt>
            <dd class="truncate font-mono text-body text-ink">{{ overview.latestJob.jobCode }}</dd>
          </div>
          <div>
            <dt class="pb-hint">状态</dt>
            <dd class="text-body" :class="STATUS_TONE[jobMeta.tone]">
              {{ overview.latestJob.jobStatusText || jobMeta.label }}
            </dd>
          </div>
          <div>
            <dt class="pb-hint">本批</dt>
            <dd class="text-body tabular-nums text-ink">
              {{
                overview.latestJob.batchProcessed === null
                  || overview.latestJob.batchProcessed === undefined
                  ? '—'
                  : `${overview.latestJob.batchProcessed} / ${overview.latestJob.batchSize}`
              }}
            </dd>
          </div>
          <div>
            <dt class="pb-hint">累计</dt>
            <dd class="text-body tabular-nums text-ink">
              {{ formatCount(overview.latestJob.processedCount) }}
              <span v-if="overview.latestJob.totalCount" class="text-ink-weak">
                / {{ formatCount(overview.latestJob.totalCount) }}
              </span>
            </dd>
          </div>
        </dl>
        <p
          v-if="overview.latestJob.jobStatus === 'PAUSED'"
          class="mt-3 flex items-start gap-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
        >
          <Clock class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>
            已暂停，等待你点「继续下一批」——
            扫描按批次限流（每 {{ overview.latestJob.batchSize }} 张停一次），
            不会一口气跑完整个照片库。
            <RouterLink class="ml-1 underline" to="/scan-jobs">去继续</RouterLink>
          </span>
        </p>
        <p v-else class="pb-hint mt-3">
          扫描按批次限流：每处理 {{ overview.latestJob.batchSize }} 张就停下来等你确认。
          <template v-if="overview.latestJob.finishedYMDHMS">
            最近一次结束于 {{ formatDateTime(overview.latestJob.finishedYMDHMS) }}。
          </template>
        </p>
      </div>

      <p v-else class="pb-hint mt-3">
        <ScanLine class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
        还没有扫描任务。到
        <RouterLink class="text-brand-ink hover:underline" to="/scan-jobs">扫描任务</RouterLink>
        建一个，原图只读、扫描不改任何文件。
      </p>

      <p
        v-if="overview?.movedPendingPhotos"
        class="mt-3 rounded-btn bg-surface px-3 py-2 text-caption text-ink-sub"
      >
        另有 <b>{{ formatCount(overview.movedPendingPhotos) }}</b> 张照片被判定为
        <b>疑似移动 / 改名</b>（内容相同但路径变了）—— 库里两条记录都在，
        <b>不会自动改路径</b>，由你在照片详情里核对。
      </p>
    </section>
  </div>
</template>