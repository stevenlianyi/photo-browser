<!--
  顶栏（56px，设计稿 §4.1）
  ------------------------------------------------------------
  从左到右：面包屑 + 页面标题 ｜ 全局搜索 ｜ 待确认角标 ｜ 主题三态开关 ｜ 设置入口
  · 角标是**两个数**（待确认 / 我不同意），不合并 —— §4.6 明确要求
  · 搜索用原生 <form role="search"> + <input type="search">：
    Element Plus 的 el-input 在顶栏这种高频位置会多包一层 div，读屏朗读标签时
    多一层噪音；表单页与筛选条里再用 el-input（那里需要 size / clearable 等）
-->
<script setup>
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ClipboardCheck, Search, Settings } from 'lucide-vue-next'
import ThemeSwitch from '@/components/common/ThemeSwitch.vue'
import { usePhotosStore } from '@/store/photos'
import { useReviewStore } from '@/store/review'

const route = useRoute()
const router = useRouter()
const review = useReviewStore()
const photos = usePhotosStore()

/** 页面标题与面包屑都来自路由 meta，页面组件里不重复写一遍 */
const title = computed(() => route.meta?.title ?? '')
const crumbs = computed(() => route.meta?.crumbs ?? [])

const keyword = ref('')

/**
 * 顶栏搜索 = 照片流的关键词筛选（步骤 11 接上）。
 * ⚠️ 回车才生效、且**必须切到照片流**：搜索结果只有照片流有，
 *    在原地过滤是不可见的（用户会以为没反应）。
 */
function submitSearch() {
  const text = keyword.value.trim()
  photos.filters.keyword = text
  photos.applyFilters()
  if (route.path !== '/photos') router.push('/photos')
}

const badgeTitle = computed(
  () => `待确认 ${review.pendingCount} 条待处理 · 我不同意 ${review.disputedCount} 条待申诉`,
)
</script>

<template>
  <header
    class="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b border-line bg-card px-4 md:px-6"
  >
    <!-- 面包屑 + 标题 -->
    <div class="min-w-0 shrink-0">
      <nav aria-label="面包屑">
        <ol class="flex items-center gap-1 text-caption text-ink-weak">
          <li v-for="(crumb, index) in crumbs" :key="crumb" class="flex items-center gap-1">
            <span v-if="index > 0" aria-hidden="true">/</span>
            <span :class="index === crumbs.length - 1 ? 'text-ink-sub' : ''">{{ crumb }}</span>
          </li>
        </ol>
      </nav>
      <h1 class="truncate text-subtitle leading-tight text-ink">{{ title }}</h1>
    </div>

    <div class="ml-auto flex items-center gap-2">
      <!-- 全局搜索 -->
      <form role="search" class="hidden sm:block" @submit.prevent="submitSearch">
        <label class="sr-only" for="pb-global-search">搜索照片与人物</label>
        <div class="relative">
          <Search
            class="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-weak"
            aria-hidden="true"
          />
          <input
            id="pb-global-search"
            v-model="keyword"
            type="search"
            name="keyword"
            placeholder="搜索照片、人物、地点"
            autocomplete="off"
            class="h-9 w-44 rounded-btn border border-line bg-bg pl-8 pr-3 text-body text-ink placeholder:text-ink-weak focus:border-brand lg:w-64"
          />
        </div>
      </form>

      <!-- 待确认角标：两个数字 -->
      <RouterLink
        to="/review"
        class="inline-flex h-9 items-center gap-1.5 rounded-btn border border-line bg-bg px-2.5 text-caption text-ink-sub transition-colors duration-150 hover:bg-surface hover:text-ink"
        :title="badgeTitle"
      >
        <ClipboardCheck class="h-4 w-4" aria-hidden="true" />
        <span class="tabular-nums font-medium text-warning-ink">{{ review.pendingCount }}</span>
        <span aria-hidden="true" class="text-ink-weak">/</span>
        <span class="tabular-nums">{{ review.disputedCount }}</span>
        <span class="sr-only">：{{ badgeTitle }}</span>
      </RouterLink>

      <ThemeSwitch />

      <RouterLink
        to="/settings"
        class="inline-flex h-9 w-9 items-center justify-center rounded-btn text-ink-sub transition-colors duration-150 hover:bg-surface hover:text-ink"
        aria-label="打开设置"
        title="设置"
      >
        <Settings class="h-5 w-5" aria-hidden="true" />
      </RouterLink>
    </div>
  </header>
</template>