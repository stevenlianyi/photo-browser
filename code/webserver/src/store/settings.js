/* ============================================================
 * 设置 store —— 外观偏好 + 识别参数 + 侧栏状态
 * ============================================================
 * 主题的**权威状态**在 utils/theme.js（localStorage + <html> class），
 * 这里只做一份镜像供组件读取，并保证 store 与 DOM 不会各说各话：
 * 改主题走 setThemeMode()，它同时更新 utils/theme.js 里的 ref。
 * ⚠️ 不要在页面里直接改 themeMode.value —— 那会绕过 localStorage。
 *
 * 识别参数（T_high / T_low / 分年代档策略）在步骤 12 接上后端之后，
 * 权威状态**搬到了服务端**（basicSettings.setThresholdOverride 等进程内开关）。
 * 这里的 matchThresholdHigh/Low 只是**未保存的草稿**：
 * `dirty` 与后端返回的 needRebucket / needCentroidRebuild 才是「要不要重算」的唯一判据。
 * 为什么保留草稿态：滑块拖动的中间值不该立刻打服务端（那是每秒十几个请求），
 * 而界面上必须让用户看得见「我改了但还没生效」。
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import {
  getRebuildStatus,
  getSettings,
  listBackups,
  rebuildCentroids,
  updateMatch,
} from '@/api/settings'
import { DEFAULT_THEME_MODE, isDarkTheme, setThemeMode, themeMode } from '@/utils/theme'
import { DEFAULT_T_HIGH, DEFAULT_T_LOW } from '@/utils/faceState'

/** 侧栏在 <1024 收窄为图标（设计稿 §8） */
export const SIDEBAR_FULL_WIDTH = 220
export const SIDEBAR_MINI_WIDTH = 72

/**
 * 默认阈值（与后端 basicSettings.MATCH_THRESHOLD_PRESETS['conservative'] 对齐）。
 * ⚠️ 定义搬到 utils/faceState.js —— 那里是相似度分档（T_low/T_high 是分档边界）
 *   的唯一出口，两个文件各写一份 0.42/0.62 迟早漂移。这里只做转发，
 *   既有的 `from '@/store/settings'` 引用全部照旧可用。
 */
export { DEFAULT_T_LOW, DEFAULT_T_HIGH }

export const BUCKET_STRATEGIES = [
  { value: 'adaptive', label: '自适应（按出生年定年代档跨度：0–18 岁 3 年 / 18+ 10 年）' },
  { value: 'fixed5', label: '等宽 5 年（无生日时的降级方案）' },
  { value: 'none', label: '不划分年代档（对照组，只用于对比排查）' },
]

export const useSettingsStore = defineStore('settings', () => {
  // ---- 外观 ----
  /** 主题模式（镜像自 utils/theme.js） */
  const theme = computed(() => themeMode.value)
  const dark = computed(() => isDarkTheme.value)
  /** 缩略图密度：舒适 = 大图少而清楚，紧凑 = 一屏更多 */
  const density = ref('comfortable')
  /** 照片流默认视图：网格（设计稿 Q-2 已定：默认网格，时间轴一键切换） */
  const photoViewMode = ref('grid')

  // ---- 后端设置（路径 / 识别参数 / 质心现状 / 备份）----
  /** GET /api/settings 的原始响应；null = 还没取过 */
  const server = ref(null)
  const serverLoading = ref(false)
  const serverError = ref('')

  /** 草稿：滑块拖动的中间值。点「保存」才发给服务端 */
  const matchThresholdHigh = ref(DEFAULT_T_HIGH)
  const matchThresholdLow = ref(DEFAULT_T_LOW)
  const bucketStrategy = ref('adaptive')
  const centroidConfirmedOnly = ref(true)

  /** 「改了参数但还没点保存」—— 只影响提示文案，不影响后端 */
  const draftDirty = ref(false)
  /** 最近一次保存后端返回的下一步指示（needRebucket / needCentroidRebuild / steps） */
  const afterSave = ref(null)

  /** 质心重算进度（后台任务；真实计数） */
  const rebuild = ref({
    running: false,
    total: 0,
    done: 0,
    ok: 0,
    failed: 0,
    percent: 0,
    log: [],
  })
  const rebuildPolling = ref(false)

  // ---- 侧栏 ----
  /** 用户手动折叠（与窄屏自动收窄是两件事，取并集） */
  const sidebarCollapsed = ref(false)
  /** 视口 < 1024（由 App.vue 的 matchMedia 写入） */
  const viewportNarrow = ref(false)
  const sidebarMini = computed(() => sidebarCollapsed.value || viewportNarrow.value)
  const sidebarWidth = computed(() => (sidebarMini.value ? SIDEBAR_MINI_WIDTH : SIDEBAR_FULL_WIDTH))

  // ---- 计算属性 ----
  /** 服务端当前生效的参数（没取到时给 undefined，界面显示「—」） */
  const effective = computed(() => server.value?.match || null)
  const paths = computed(() => server.value?.paths || null)
  const centroidOverview = computed(() => server.value?.centroids || null)

  /** 是否需要重算质心：草稿有改动，或上一次保存后端明确要求过 */
  const needRebucket = computed(() => Boolean(afterSave.value?.needRebucket))
  const needCentroidRebuild = computed(
    () => draftDirty.value || Boolean(afterSave.value?.needCentroidRebuild),
  )
  /**
   * 旧口径的 centroidDirty 保留成computed别名。
   * 组件里已经在用 settings.centroidDirty —— 直接删掉那个 ref 会让
   * 「忘了改的某一处」变成 undefined（界面上提示条静默消失），那是更坏的失败。
   */
  const centroidDirty = computed(() => needCentroidRebuild.value)

  function syncDraftFromServer() {
    const match = server.value?.match
    if (!match) return
    matchThresholdHigh.value = Number(match.tHigh) || DEFAULT_T_HIGH
    matchThresholdLow.value = Number(match.tLow) || DEFAULT_T_LOW
    bucketStrategy.value = match.bucketStrategy || 'adaptive'
    centroidConfirmedOnly.value = Boolean(match.centroidConfirmedOnly)
    draftDirty.value = false
  }

  watch([matchThresholdHigh, matchThresholdLow, bucketStrategy, centroidConfirmedOnly], () => {
    // 只在**已经取过服务端参数之后**才算 dirty：
    // syncDraftFromServer 自己也在写这几个 ref，没有这道闸，
    // 首屏刚挂上就会被判成「参数已改动」。
    if (!server.value) return
    draftDirty.value = true
  })

  async function fetchSettings() {
    serverLoading.value = true
    serverError.value = ''
    try {
      const data = await getSettings()
      server.value = data
      syncDraftFromServer()
      afterSave.value = null
      rebuild.value = data?.rebuild || rebuild.value
      return data
    } catch (e) {
      serverError.value = e?.message || '读取设置失败'
      throw e
    } finally {
      serverLoading.value = false
    }
  }

  /** 保存参数。**只改开关，不重算** —— 后端把下一步指示放在响应里 */
  async function saveMatch() {
    const body = {
      tLow: Number(matchThresholdLow.value),
      tHigh: Number(matchThresholdHigh.value),
      bucketStrategy: bucketStrategy.value,
      confirmedOnly: Boolean(centroidConfirmedOnly.value),
    }
    const data = await updateMatch(body)
    afterSave.value = data
    draftDirty.value = false
    await fetchSettings()
    // ⚠️ fetchSettings 会清空 afterSave，所以顺序是「先存再刷新」；
    //   但 fetchSettings 里又把 afterSave 置空了 —— 那会让提示条立刻消失，
    //   而用户正需要它告诉他「现在该点重算」。这里显式放回去。
    afterSave.value = data
    return data
  }

  /** 回到后端当前生效值（放弃草稿） */
  function discardDraft() {
    syncDraftFromServer()
    afterSave.value = null
  }

  async function fetchBackups() {
    const data = await listBackups()
    if (server.value) server.value = { ...server.value, backups: data }
    return data
  }

  /** 触发重算（后台）。rebucketFirst 固定 true —— DR-22 的硬顺序不给绕过口 */
  async function startRebuild(personCode) {
    const data = await rebuildCentroids({
      personCode: personCode || undefined,
      confirmedOnly: Boolean(centroidConfirmedOnly.value),
      rebucketFirst: true,
    })
    rebuild.value = { ...rebuild.value, ...data }
    return data
  }

  async function refreshRebuild() {
    const data = await getRebuildStatus()
    rebuild.value = data
    return data
  }

  let rebuildTimer = null
  function setRebuildPolling(on) {
    rebuildPolling.value = Boolean(on)
    if (rebuildTimer) {
      clearInterval(rebuildTimer)
      rebuildTimer = null
    }
    if (!rebuildPolling.value) return
    rebuildTimer = setInterval(async () => {
      try {
        await refreshRebuild()
      } catch (e) {
        /* 轮询失败不打断界面：下一步自然会再试 */
      }
      if (!rebuild.value.running) setRebuildPolling(false)
    }, 1500)
  }

  function stopRebuildPolling() {
    setRebuildPolling(false)
  }

  function setTheme(mode) {
    return setThemeMode(mode)
  }

  function setDensity(value) {
    density.value = value === 'compact' ? 'compact' : 'comfortable'
  }

  function setViewMode(value) {
    photoViewMode.value = value === 'timeline' ? 'timeline' : 'grid'
  }

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  function setViewportNarrow(value) {
    viewportNarrow.value = Boolean(value)
  }

  function resetMatchParams() {
    matchThresholdHigh.value = DEFAULT_T_HIGH
    matchThresholdLow.value = DEFAULT_T_LOW
    bucketStrategy.value = 'adaptive'
    centroidConfirmedOnly.value = true
  }

  return {
    // 外观
    theme,
    dark,
    density,
    photoViewMode,
    // 服务端设置
    server,
    serverLoading,
    serverError,
    paths,
    effective,
    centroidOverview,
    matchThresholdHigh,
    matchThresholdLow,
    bucketStrategy,
    centroidConfirmedOnly,
    draftDirty,
    afterSave,
    rebuild,
    rebuildPolling,
    needRebucket,
    needCentroidRebuild,
    centroidDirty,
    // 侧栏
    sidebarCollapsed,
    viewportNarrow,
    sidebarMini,
    sidebarWidth,
    // 行为
    fetchSettings,
    saveMatch,
    discardDraft,
    fetchBackups,
    startRebuild,
    refreshRebuild,
    setRebuildPolling,
    stopRebuildPolling,
    setTheme,
    setDensity,
    setViewMode,
    toggleSidebar,
    setViewportNarrow,
    resetMatchParams,
    DEFAULT_THEME_MODE,
  }
})