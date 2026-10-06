/* ============================================================
 * 设置 store —— 外观偏好 + 识别参数 + 侧栏状态
 * ============================================================
 * 主题的**权威状态**在 utils/theme.js（localStorage + <html> class），
 * 这里只做一份镜像供组件读取，并保证 store 与 DOM 不会各说各话：
 * 改主题走 setThemeMode()，它同时更新 utils/theme.js 里的 ref。
 * ⚠️ 不要在页面里直接改 themeMode.value —— 那会绕过 localStorage。
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { DEFAULT_THEME_MODE, isDarkTheme, setThemeMode, themeMode } from '@/utils/theme'

/** 侧栏在 <1024 收窄为图标（设计稿 §8） */
export const SIDEBAR_FULL_WIDTH = 220
export const SIDEBAR_MINI_WIDTH = 72

export const useSettingsStore = defineStore('settings', () => {
  // ---- 外观 ----
  /** 主题模式（镜像自 utils/theme.js） */
  const theme = computed(() => themeMode.value)
  const dark = computed(() => isDarkTheme.value)
  /** 缩略图密度：舒适 = 大图少而清楚，紧凑 = 一屏更多 */
  const density = ref('comfortable')
  /** 照片流默认视图：网格（设计稿 Q-2 已定：默认网格，时间轴一键切换） */
  const photoViewMode = ref('grid')

  // ---- 识别参数（P-08 可调；改完提示「需重新生成质心」）----
  /** 自动归属阈值：相似度 ≥ T_high 才自动归属 */
  const matchThresholdHigh = ref(0.45)
  /** 置为未知阈值：< T_low 直接进聚类，不进待确认队列 */
  const matchThresholdLow = ref(0.3)
  /** 分桶策略：自适应（按出生年定桶宽）/ 等宽 5 年 / 不分桶 */
  const bucketStrategy = ref('adaptive')

  // ---- 侧栏 ----
  /** 用户手动折叠（与窄屏自动收窄是两件事，取并集） */
  const sidebarCollapsed = ref(false)
  /** 视口 < 1024（由 App.vue 的 matchMedia 写入） */
  const viewportNarrow = ref(false)
  const sidebarMini = computed(() => sidebarCollapsed.value || viewportNarrow.value)
  const sidebarWidth = computed(() => (sidebarMini.value ? SIDEBAR_MINI_WIDTH : SIDEBAR_FULL_WIDTH))

  /** 阈值改动后质心与匹配结果都会变化，需要提示重算（步骤 12 接真接口） */
  const centroidDirty = ref(false)

  watch([matchThresholdHigh, matchThresholdLow, bucketStrategy], () => {
    centroidDirty.value = true
  })

  function setTheme(mode) {
    return setThemeMode(mode)
  }

  function setDensity(value) {
    density.value = value === 'compact' ? 'compact' : 'comfortable'
  }

  function setViewMode(mode) {
    photoViewMode.value = mode === 'timeline' ? 'timeline' : 'grid'
  }

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  function setViewportNarrow(value) {
    viewportNarrow.value = Boolean(value)
  }

  function resetMatchParams() {
    matchThresholdHigh.value = 0.45
    matchThresholdLow.value = 0.3
    bucketStrategy.value = 'adaptive'
    centroidDirty.value = false
  }

  return {
    // 外观
    theme,
    dark,
    density,
    photoViewMode,
    // 识别参数
    matchThresholdHigh,
    matchThresholdLow,
    bucketStrategy,
    centroidDirty,
    // 侧栏
    sidebarCollapsed,
    viewportNarrow,
    sidebarMini,
    sidebarWidth,
    // 行为
    setTheme,
    setDensity,
    setViewMode,
    toggleSidebar,
    setViewportNarrow,
    resetMatchParams,
    DEFAULT_THEME_MODE,
  }
})