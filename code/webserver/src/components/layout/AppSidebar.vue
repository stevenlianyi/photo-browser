<!--
  左侧一级导航（设计稿 §3.1 / §4.1）
  ------------------------------------------------------------
  · 固定 6 项，顺序即 ASCII 布局顺序；「设置」用分隔线隔开固定在底部
  · 220px → <1024 收窄为 72px 图标栏（宽度由 store 统一算，组件不各自写死）
  · 角标是**两个数**：待确认（没归属的脸）/ 我不同意（机器自动认的），
    分开显示不合并 —— 两者要处理的问题不同（§4.6）
  · 用 RouterLink（渲染 <a href>）而不是 div + @click：可中键新开、可被读屏识别为链接
-->
<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import {
  ClipboardCheck,
  Gauge,
  Images,
  MapPin,
  PanelLeftClose,
  PanelLeftOpen,
  ScanLine,
  Settings,
  Users,
} from 'lucide-vue-next'
import { NAV_ITEMS } from '@/router'
import { useReviewStore } from '@/store/review'
import { useSettingsStore } from '@/store/settings'

const settings = useSettingsStore()
const review = useReviewStore()

// ⚠️ 键名必须与 router 的 NAV_ITEMS[].icon 逐字一致（R5 加了 MapPin 指「地点」）——
//    写错一个字母时 `resolveDynamicComponent` 会给出一个空组件：
//    侧栏少一个图标，页面上**什么都不报**。
const ICONS = { Gauge, Images, Users, MapPin, ClipboardCheck, ScanLine, Settings }

/** 导航项末尾是设置 → 前面加一条分隔线 */
const items = computed(() => NAV_ITEMS)
const mini = computed(() => settings.sidebarMini)

function badgeTitle() {
  return `待确认 ${review.pendingCount} 条 · 我不同意 ${review.disputedCount} 条`
}
</script>

<template>
  <nav
    aria-label="主导航"
    class="sticky top-0 z-30 flex h-screen shrink-0 flex-col border-r border-line bg-card transition-[width] duration-150"
    :style="{ width: `${settings.sidebarWidth}px` }"
  >
    <!-- 品牌区 -->
    <div class="flex h-14 shrink-0 items-center gap-2 border-b border-line px-4">
      <span
        class="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-brand text-caption font-semibold text-ink-invert"
        aria-hidden="true"
      >
        PB
      </span>
      <span v-if="!mini" class="truncate text-subtitle text-ink">photo-browser</span>
    </div>

    <!-- 导航项 -->
    <ul class="flex-1 overflow-y-auto p-2">
      <template v-for="item in items" :key="item.name">
        <li v-if="item.divider" class="my-2 border-t border-line" role="separator" />
        <li>
          <RouterLink
            :to="item.path"
            class="group relative mb-0.5 flex h-10 items-center gap-3 rounded-btn px-3 text-body transition-colors duration-150"
            :class="
              $route.path === item.path || ($route.path.startsWith(item.path) && item.path !== '/')
                ? 'bg-brand-soft text-brand-ink'
                : 'text-ink-sub hover:bg-surface hover:text-ink'
            "
            :aria-current="$route.path === item.path ? 'page' : undefined"
            :title="mini ? item.label : undefined"
            :aria-label="mini ? item.label : undefined"
          >
            <component :is="ICONS[item.icon]" class="h-5 w-5 shrink-0" aria-hidden="true" />

            <template v-if="!mini">
              <span class="truncate">{{ item.label }}</span>
              <!-- 角标：两个独立计数 -->
              <span
                v-if="item.badge"
                class="ml-auto inline-flex items-center gap-0.5 rounded-btn bg-warning-soft px-1.5 py-0.5 text-caption font-medium text-warning-ink"
                :title="badgeTitle()"
              >
                <span>{{ review.pendingCount }}</span>
                <span aria-hidden="true">/</span>
                <span>{{ review.disputedCount }}</span>
                <span class="sr-only">：{{ badgeTitle() }}</span>
              </span>
            </template>

            <!-- 收窄态：只留一个点，hover 出两个数的说明 -->
            <span
              v-else-if="item.badge && review.badgeTotal > 0"
              class="absolute right-3 top-3 h-1.5 w-1.5 rounded-full bg-[var(--pb-warning-line)]"
              aria-hidden="true"
            />
          </RouterLink>
        </li>
      </template>
    </ul>

    <!-- 折叠 / 展开 -->
    <div class="shrink-0 border-t border-line p-2">
      <button
        type="button"
        class="flex h-9 w-full items-center gap-2 rounded-btn px-3 text-caption text-ink-sub transition-colors duration-150 hover:bg-surface hover:text-ink"
        :aria-expanded="!mini"
        :aria-label="mini ? '展开侧边栏' : '收起侧边栏'"
        @click="settings.toggleSidebar()"
      >
        <component
          :is="mini ? PanelLeftOpen : PanelLeftClose"
          class="h-4 w-4 shrink-0"
          aria-hidden="true"
        />
        <span v-if="!mini">收起侧边栏</span>
      </button>
    </div>
  </nav>
</template>