<!--
  P-01 概览（设计稿 §4.2）
  ------------------------------------------------------------
  四块：统计卡 ×4 ｜ 待确认队列进度 ｜ 最近入库缩略图行 ｜ 扫描状态条
  ⚠️ 步骤 10 只出结构：下面的数字是设计稿里的**结构示例**，
     步骤 11 接 /api/overview 后由真实计数替换。
-->
<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { Camera, CircleCheck, Clock, Images, Plus, Users } from 'lucide-vue-next'
import { useReviewStore } from '@/store/review'

const review = useReviewStore()

/** 统计卡：图标 + 数值 + 说明（色 + 图标 + 文字三重编码，不单靠颜色） */
const STAT_CARDS = [
  { key: 'photo', label: '照片总数', value: '12,847', unit: '张', icon: Images, tone: 'text-ink' },
  { key: 'person', label: '人物', value: '63', unit: '人', icon: Users, tone: 'text-ink' },
  {
    key: 'pending',
    label: '待确认',
    value: '214',
    unit: '条',
    icon: Clock,
    tone: 'text-warning-ink',
  },
  {
    key: 'scan',
    label: '最近扫描',
    value: '08-31',
    unit: '已暂停',
    icon: CircleCheck,
    tone: 'text-ink-sub',
  },
]

/** 最近入库：10 个缩略图位（步骤 11 接 /api/photos 后换成真实缩略图） */
const RECENT_PHOTOS = [
  { photoCode: 'P-000231', fileName: 'DSC02580.JPG', year: '2006', faces: 2 },
  { photoCode: 'P-000232', fileName: 'IMG_2231.JPG', year: '2015', faces: 1 },
  { photoCode: 'P-000233', fileName: 'DSC03112.JPG', year: '2007', faces: 0 },
  { photoCode: 'P-000234', fileName: 'IMG_4471.JPG', year: '2019', faces: 3 },
  { photoCode: 'P-000235', fileName: 'P1010012.JPG', year: '2013', faces: 0 },
  { photoCode: 'P-000236', fileName: 'DSC04120.JPG', year: '2010', faces: 1 },
  { photoCode: 'P-000237', fileName: 'IMG_5520.JPG', year: '2020', faces: 4 },
  { photoCode: 'P-000238', fileName: 'DSC00944.JPG', year: '2004', faces: 0 },
  { photoCode: 'P-000239', fileName: 'IMG_6104.JPG', year: '2021', faces: 2 },
  { photoCode: 'P-000240', fileName: 'DSC05233.JPG', year: '2012', faces: 0 },
]

/** 待确认进度：已完成 + 剩余（进度条按真实计数算，不做假进度） */
const DONE_COUNT = 1203
const PENDING_TOTAL = 214
const pendingPercent = computed(() =>
  Math.min(100, Math.round((DONE_COUNT / (DONE_COUNT + PENDING_TOTAL)) * 100)),
)
</script>

<template>
  <div class="pb-page space-y-6">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">概览</h2>
        <p class="pb-hint mt-1">本机照片库的当前状态：照片、人物、待确认与扫描进度。</p>
      </div>
      <RouterLink to="/scan-jobs">
        <el-button type="primary">
          <Plus class="mr-1 h-4 w-4" aria-hidden="true" />
          新建扫描
        </el-button>
      </RouterLink>
    </div>

    <!-- 统计卡 ×4 -->
    <section aria-label="库状态统计" class="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <article v-for="card in STAT_CARDS" :key="card.key" class="pb-card p-4">
        <div class="flex items-center gap-2">
          <component :is="card.icon" class="h-4 w-4 text-ink-weak" aria-hidden="true" />
          <h3 class="text-body text-ink-sub">{{ card.label }}</h3>
        </div>
        <p class="mt-2 flex items-baseline gap-1">
          <span class="text-title tabular-nums" :class="card.tone">{{ card.value }}</span>
          <span class="text-caption text-ink-weak">{{ card.unit }}</span>
        </p>
      </article>
    </section>

    <!-- 待确认队列进度 -->
    <section class="pb-card p-4" aria-labelledby="ov-review-title">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h3 id="ov-review-title" class="pb-section-title">待确认队列</h3>
        <RouterLink to="/review">
          <el-button size="small">去处理</el-button>
        </RouterLink>
      </div>
      <p class="mt-2 text-body text-ink-sub">
        剩余 <span class="font-medium text-warning-ink">{{ review.pendingCount || PENDING_TOTAL }}</span> 条 ·
        已完成 {{ DONE_COUNT.toLocaleString('zh-CN') }} 条
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
        「待确认」是还没归属的脸；「我不同意」是机器自动认的、还没人工确认的脸 ——
        浏览照片时看到认错，先去第二个列表里否决。
      </p>
    </section>

    <!-- 最近入库 -->
    <section class="pb-card p-4" aria-labelledby="ov-recent-title">
      <div class="flex items-center justify-between gap-3">
        <h3 id="ov-recent-title" class="pb-section-title">最近入库</h3>
        <RouterLink to="/photos" class="text-caption text-brand-ink hover:underline">
          查看全部
        </RouterLink>
      </div>
      <ul class="mt-3 grid grid-cols-5 gap-2 sm:grid-cols-6 md:grid-cols-10">
        <li v-for="photo in RECENT_PHOTOS" :key="photo.photoCode">
          <RouterLink
            :to="`/photos/${photo.photoCode}`"
            class="block"
            :aria-label="`${photo.fileName}，${photo.year} 年，${photo.faces} 张人脸`"
          >
            <span class="pb-photo-frame block aspect-square">
              <span
                class="absolute inset-x-0 bottom-0 flex items-center justify-between gap-1 p-1 text-caption text-ink-sub"
              >
                <span class="truncate">{{ photo.year }}</span>
                <span v-if="photo.faces > 0" class="inline-flex items-center gap-0.5">
                  <Camera class="h-3 w-3" aria-hidden="true" />{{ photo.faces }}
                </span>
              </span>
            </span>
            <span class="mt-1 block truncate text-caption text-ink-weak">{{ photo.fileName }}</span>
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
      <dl class="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div>
          <dt class="pb-hint">任务</dt>
          <dd class="text-body text-ink">SJ-0006</dd>
        </div>
        <div>
          <dt class="pb-hint">状态</dt>
          <dd class="text-body text-warning-ink">已暂停，等待你点「继续下一批」</dd>
        </div>
        <div>
          <dt class="pb-hint">本批</dt>
          <dd class="text-body tabular-nums text-ink">100 / 100</dd>
        </div>
        <div>
          <dt class="pb-hint">累计</dt>
          <dd class="text-body tabular-nums text-ink">3,800</dd>
        </div>
      </dl>
      <p class="pb-hint mt-3">
        扫描按批次限流：每处理 100 张就停下来等你确认，不会一口气跑完整个照片库。
      </p>
    </section>

    <p class="pb-hint">本页数字为结构示例，接入 /api/overview 后由真实计数替换。</p>
  </div>
</template>