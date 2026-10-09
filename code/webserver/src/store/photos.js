/* ============================================================
 * 照片 store —— 列表 / 筛选 / 网格与时间轴视图
 * ============================================================
 * 筛选字段与后端 /api/photos 的 Query 逐字对应（见 api/browse.js），
 * 这里只做「界面状态 ↔ 查询参数」的翻译，不做业务判断。
 *
 * 分页口径：**页码分页 + 滚动追加**（不是虚拟滚动），理由与代价见
 * `appendMore()` 的注释。10 万张不可能一次进 DOM。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getPhoto, getTimeline, listPhotos, listPersons } from '@/api/browse'
// ⚠️ 地点封装在 api/place.js（R5 起的唯一出口），不再从 browse.js 引
import { listPlaces } from '@/api/place'

/** 与后端 dto.DEFAULT_PAGE_SIZE 对齐；单页上限由后端 le 限制 */
export const DEFAULT_PAGE_SIZE = 60

/** 排序项：值即后端 orderBy 白名单（browse.PHOTO_SORT_COLUMNS） */
export const SORT_OPTIONS = [
  { value: 'takenAt', label: '拍摄时间' },
  { value: 'shotYear', label: '拍摄年份' },
  { value: 'fileSize', label: '文件大小' },
  { value: 'recID', label: '导入顺序' },
]

export const usePhotosStore = defineStore('photos', () => {
  // ---- 列表数据 ----
  const items = ref([])
  const total = ref(0)
  const page = ref(1)
  const size = ref(DEFAULT_PAGE_SIZE)
  const loading = ref(false)
  const loaded = ref(false)
  /** 追加一页时的 loading（与首屏分开，否则追加会把整页刷成骨架） */
  const loadingMore = ref(false)

  // ---- 当前照片详情（P-03）----
  const current = ref(null)
  const currentLoading = ref(false)

  // ---- 筛选下拉的数据源（人物 / 地点）----
  const personOptions = ref([])
  const placeOptions = ref([])
  const optionsLoading = ref(false)

  // ---- 时间轴视图 ----
  const timelineYears = ref([])
  const timelineUnknownCount = ref(0)
  const timelineLoading = ref(false)
  const expandedYear = ref(null)
  const expandedMonth = ref(null)
  /** ym -> 该月照片（懒加载后缓存，来回切换不必重取） */
  const monthPhotos = ref({})

  // ---- 筛选条件（空串 = 不筛；不要用 null，axios 会把 null 键发给后端）----
  const filters = ref({
    keyword: '',
    personCode: '',
    placeName: '',
    shotYearFrom: '',
    shotYearTo: '',
    hasFace: '',
    isDuplicate: '',
  })

  /** 排序：拍摄时间 / 拍摄年份 / 文件大小 / 导入顺序（默认拍摄时间倒序） */
  const orderBy = ref('takenAt')

  /** 角标筛选：只看含人脸 / 只看重复照片 */
  const onlyWithFace = ref(false)
  const onlyDuplicate = ref(false)

  /** 是否有任何筛选条件（用于显示「清除筛选」） */
  const hasFilter = computed(
    () =>
      Boolean(filters.value.keyword) ||
      Boolean(filters.value.personCode) ||
      Boolean(filters.value.placeName) ||
      Boolean(filters.value.shotYearFrom) ||
      Boolean(filters.value.shotYearTo) ||
      onlyWithFace.value ||
      onlyDuplicate.value,
  )

  const hasMore = computed(() => items.value.length < total.value)

  /**
   * 连续滚动最多累积多少格（30 页 = 1800 张）。
   *
   * ⚠️ 为什么必须有这个上限
   * ----------------------
   *   「分页 + 追加」把**每帧**的渲染成本钉死在视口那一屏（content-visibility
   *   + IntersectionObserver），所以「往下翻」这件事本身不会越来越卡。
   *   但**DOM 节点数**会随滚动单调增长：一个人如果真的一路滚完 10 万张，
   *   DOM 里就会攒下 10 万个节点 —— 那才是真正会卡的地方，而且不是「滚动卡」
   *   而是「切页/切主题卡」。
   *   所以到上限就停：想看更后面用**分页器跳**（那是 replace，DOM 回到 60 个节点）。
   *   1800 张 ≈ 30 屏，足够把一个年份看完；而「翻到 2020 年」这种需求
   *   本来就该用年份筛选或分页器，不是靠无限滚。
   */
  const MAX_APPENDED_CELLS = 1800
  const appendCapped = computed(() => items.value.length >= MAX_APPENDED_CELLS)

  /** 到上限后不再自动追加（hasMore 仍为 true，分页器照常可用） */
  const canAppendMore = computed(() => hasMore.value && !appendCapped.value)

  /** 分页信息文案：「显示 1-60 条，共 N 条」 */
  const rangeText = computed(() => {
    const to = items.value.length
    return `显示 ${to ? 1 : 0}-${to} 条，共 ${Number(total.value || 0).toLocaleString('zh-CN')} 条`
  })

  /**
   * 查询参数：把界面开关翻译成后端的 1 / **不传该键**。
   *
   * ⚠️ 这里必须用 `undefined` 而不是 `''`（步骤 10 埋的雷，这一步才第一次真的发出去）：
   *   axios 会把 `''` 原样发成 `hasFace=`，而后端 `hasFace: int = Query(...)`
   *   收到空串直接 422「Input should be a valid integer」——照片流首屏整个加载不出来。
   *   `undefined` 的键会被 axios 整个丢掉，这才是「不筛」的正确表达。
   */
  function toQuery() {
    return {
      page: page.value,
      size: size.value,
      keyword: filters.value.keyword || undefined,
      personCode: filters.value.personCode || undefined,
      placeName: filters.value.placeName || undefined,
      shotYearFrom: filters.value.shotYearFrom || undefined,
      shotYearTo: filters.value.shotYearTo || undefined,
      hasFace: onlyWithFace.value ? 1 : undefined,
      isDuplicate: onlyDuplicate.value ? 1 : undefined,
      orderBy: orderBy.value,
      desc: 1,
    }
  }

  /**
   * 取一页。replace=true 覆盖（首屏 / 换筛选），false 追加（滚动到底）。
   *
   * 为什么是「页码分页 + 追加」而不是虚拟滚动
   * -----------------------------------------
   * 虚拟滚动要接管滚动容器、算 item 高度与 offset，代价是：**照片高度事先
   * 不知道**（EXIF 尺寸不同的横竖图混排），必须先估一个高度再纠正，
   * 而纠正时滚动位置会跳 —— 这个页面的核心动作是「一路往下翻照片」，
   * 位置跳一次就要翻回去重找。
   * 现在这套：页码分页（后端已有 OFFSET 分页 + 索引）+ IntersectionObserver
   * 追加 + 每格 `content-visibility: auto`（跳过屏外格的布局与绘制）。
   * 实测数字见验收报告。
   */
  async function fetchPhotos({ replace = true } = {}) {
    if (replace) loading.value = true
    else loadingMore.value = true
    try {
      const data = await listPhotos(toQuery())
      const list = data?.items ?? []
      items.value = replace ? list : [...items.value, ...list]
      total.value = data?.total ?? 0
      page.value = data?.page ?? page.value
      loaded.value = true
      return data
    } finally {
      loading.value = false
      loadingMore.value = false
    }
  }

  /** 滚动到底部：追加下一页（到 MAX_APPENDED_CELLS 就停，改用分页器） */
  async function appendMore() {
    if (loading.value || loadingMore.value || !canAppendMore.value) return null
    page.value = page.value + 1
    try {
      return await fetchPhotos({ replace: false })
    } catch (error) {
      // 回退页码：否则这次失败会把 page 留在一个没取到的页上，
      // 下一次追加会跳过整整一页照片（看起来就是「漏了一页」，且不会报错）
      page.value = page.value - 1
      throw error
    }
  }

  async function fetchPhoto(photoCode) {
    currentLoading.value = true
    try {
      current.value = await getPhoto(photoCode)
      return current.value
    } finally {
      currentLoading.value = false
    }
  }

  /**
   * 把一张照片的**显示角度**写回本地缓存（DR-43）。
   *
   * 为什么必须有这个函数而不是「旋转成功后整页 reload」
   * -----------------------------------------------
   *   · 详情页的主图/尺寸/人脸框都读 `photos.current`，网格读 `items`，
   *     时间轴展开的月份读 `monthPhotos`。旋转后**只有这些地方**需要变 ——
   *     重取一次详情是白跑一趟（还多一次 original 直传）。
   *   · ⚠️ 更关键的是**翻页不能串角度**（最容易漏测的那条）：
   *     `PhotoPager` 翻到下一张时读的是**那一张自己**的 rotateDeg，
   *     所以这里必须**按 photoCode 命中才改**。写成"无条件改当前值"的话，
   *     转完一张按 →，下一张也会是躺着的（刷新一下就好，所以极易漏测）。
   */
  function setRotateDeg(photoCode, rotateDeg) {
    const code = String(photoCode || '')
    if (!code) return
    const deg = Number(rotateDeg) || 0
    if (current.value && String(current.value.photoCode) === code) {
      current.value = { ...current.value, rotateDeg: deg }
    }
    items.value = items.value.map((one) =>
      String(one?.photoCode) === code ? { ...one, rotateDeg: deg } : one,
    )
    // 时间轴展开的月份：整块换一个对象，让依赖它的 computed 能看见变化
    const next = {}
    let touched = false
    for (const [ym, list] of Object.entries(monthPhotos.value || {})) {
      next[ym] = (list || []).map((one) => {
        if (String(one?.photoCode) !== code) return one
        touched = true
        return { ...one, rotateDeg: deg }
      })
    }
    if (touched) monthPhotos.value = next
  }

  /** 时间轴首屏：年表（含每月计数） */
  async function fetchTimelineYears() {
    timelineLoading.value = true
    try {
      const data = await getTimeline()
      timelineYears.value = data?.items ?? []
      timelineUnknownCount.value = Number(data?.unknownCount) || 0
      return data
    } finally {
      timelineLoading.value = false
    }
  }

  /** 展开某一年：只有一年一个月时直接展开该月，省一次点击 */
  async function toggleYear(year) {
    if (expandedYear.value === year) {
      expandedYear.value = null
      expandedMonth.value = null
      return
    }
    expandedYear.value = year
    expandedMonth.value = null
    const months = (timelineYears.value.find((y) => y.year === year)?.months) || []
    if (months.length === 1) await toggleMonth(year, months[0]?.ym)
  }

  /** 展开某个月：取该月照片（offset 被「一个月」天然兜住，不做深分页） */
  async function toggleMonth(year, ym) {
    if (expandedMonth.value === ym) {
      expandedMonth.value = null
      return
    }
    expandedYear.value = year
    expandedMonth.value = ym
    if (monthPhotos.value[ym]) return
    // ⚠️ 两个值都过一遍 Number：year/ym 来自接口给的分组键，本该是数字，
    //    但一旦哪天出现 "未知年份" 这类分组键，`year=abc` / `month=NaN` 会被
    //    原样发出去 —— 排查时看到的是「后端 422」，根因却在前端一个字符串上。
    //    这里显式失败（不发请求）比发一个必然被拒的请求好。
    const yearNum = Number(year)
    const monthNum = Number(String(ym ?? '').slice(-2))
    if (!Number.isInteger(yearNum) || !Number.isInteger(monthNum)) return
    const data = await getTimeline({ year: yearNum, month: monthNum })
    monthPhotos.value = { ...monthPhotos.value, [ym]: data?.page?.items ?? [] }
  }

  /**
   * 筛选下拉的数据源。
   * ⚠️ 人物列表是**全库**（人名册只有几十~几百人，一次取完比按需取更便宜），
   *    地点走 /api/places 的字典表。
   */
  async function loadFilterOptions() {
    if (personOptions.value.length || optionsLoading.value) return
    optionsLoading.value = true
    try {
      const [persons, places] = await Promise.all([
        listPersons({ page: 1, size: 200, orderBy: 'displayName' }),
        listPlaces({ page: 1, size: 200, orderBy: 'photoCount', desc: 1 }),
      ])
      personOptions.value = persons?.items ?? []
      placeOptions.value = places?.items ?? []
    } catch {
      personOptions.value = []
      placeOptions.value = []
    } finally {
      optionsLoading.value = false
    }
  }

  function setPage(value) {
    page.value = value
  }

  /** 换筛选：一律回到第 1 页（带着旧页码去新条件里翻是错的） */
  function applyFilters() {
    page.value = 1
    items.value = []
  }

  /**
   * ⚠️ 必须**原地改**（Object.assign）而不是换一个新对象：
   *    PhotosView 里 `const filters = photos.filters` 持有的是**当时那个对象**，
   *    换新对象后页面手上的还是旧的 —— 点「清除筛选」时输入框里的条件不会消失，
   *    watch 也看不到变化（它盯的同样是旧对象）。
   */
  function resetFilters() {
    Object.assign(filters.value, {
      keyword: '',
      personCode: '',
      placeName: '',
      shotYearFrom: '',
      shotYearTo: '',
      hasFace: '',
      isDuplicate: '',
    })
    onlyWithFace.value = false
    onlyDuplicate.value = false
    page.value = 1
    items.value = []
  }

  return {
    items,
    total,
    page,
    size,
    loading,
    loadingMore,
    loaded,
    current,
    currentLoading,
    filters,
    orderBy,
    onlyWithFace,
    onlyDuplicate,
    personOptions,
    placeOptions,
    optionsLoading,
    timelineYears,
    timelineUnknownCount,
    timelineLoading,
    expandedYear,
    expandedMonth,
    monthPhotos,
    hasFilter,
    hasMore,
    canAppendMore,
    appendCapped,
    MAX_APPENDED_CELLS,
    rangeText,
    toQuery,
    fetchPhotos,
    appendMore,
    fetchPhoto,
    setRotateDeg,
    fetchTimelineYears,
    toggleYear,
    toggleMonth,
    loadFilterOptions,
    setPage,
    applyFilters,
    resetFilters,
    SORT_OPTIONS,
  }
})
