/* ============================================================
 * 待确认 store —— 两条队列 + 侧栏角标
 * ============================================================
 * 侧栏/顶栏角标是**两个数**（pendingCount / disputedCount），不合并成一个
 * （设计稿 §4.6 明确要求）：「待确认」是没归属的脸，「我不同意」是机器自动认的，
 * 两者要处理的问题不同，合并后用户不知道该先看哪个。
 *
 * ⚠️ pendingCount / disputedCount 是两个不同口径的计数，别拿 pb_scan_job.pendingCount
 *    之类的字段顶替（步骤 3/7 已标注过这个坑）。
 *
 * 写操作为什么不逐个调 fetchBadge()
 * --------------------------------
 * /api/review/* 的每个写接口**都把新的 pendingCount / disputedCount 一起回带**
 * （api/review.py 的 `_counts()`）。所以确认/改判之后直接吃响应里的两个数即可，
 * 少一次往返；只有「别处改了库」时才需要重新拉（页面 onActivated 时做一次）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getPhoto, listPersons } from '@/api/browse'
import { createContact } from '@/api/contacts'
import {
  assignFace,
  batchAssign,
  fixFace,
  getClusterMembers,
  getDisputed,
  getPending,
  getPendingCount,
  splitFace,
} from '@/api/review'

export const REVIEW_TABS = [
  { name: 'pending', label: '待确认' },
  { name: 'disputed', label: '我不同意' },
]

/** 一页取多少条待确认（一次确认后要能连着看几条，别每确认一次就请求一次） */
const PENDING_PAGE_SIZE = 20

/**
 * 本地缓冲的**低水位**：剩余待处理条数 <= 这个值时，后台静默补一页。
 * -----------------------------------------------------------------
 * 取 5 而不是 1：用户连点确认时，补页请求要来得及在「看完第 3 条」前回来，
 * 于是永远不必停下来等网络。取太大（比如 10）则一次确认可能触发连补两页。
 */
const PENDING_LOW_WATER = 5

/**
 * 「已跳过」的持久化位置与容量。
 * -------------------------------
 * 跳过**不写库**（永久排除是「陌生人」），但用户的意思通常是"这一轮先放一放"，
 * 关掉页面再回来不该又被同一批脸挡住 —— 所以存 localStorage，跨会话保留。
 *
 * ⚠️ 上限 500 是与服务端 `excludeFaceCodes` 的上限**对齐**的：
 *    超了服务端会静默截断，那"最老的跳过"就会悄悄失效（最先放回去）。
 *    与其让它被动失效，不如本地就按 FIFO 截断 —— 行为可预期、可解释。
 * ⚠️ 键名带 `.v1`：结构将来若要变（比如加过期时间），换键名等于自然作废旧数据，
 *    不会读到半新半旧的结构。
 */
const DEFERRED_STORAGE_KEY = 'pb.review.deferred.v1'
const DEFERRED_MAX = 500

/** localStorage 在 Node（校验脚本）/ 隐私模式下可能不存在或抛异常，全部兜住 */
function readDeferredCodes() {
  try {
    if (typeof localStorage === 'undefined') return []
    const raw = localStorage.getItem(DEFERRED_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((one) => typeof one === 'string' && one).slice(-DEFERRED_MAX)
  } catch (e) {
    // 存的东西坏了就当没有：跳过集失效最多"多问一次"，不该让页面起不来
    console.warn('[review] 读取本机跳过记录失败，按空处理：', e?.message || e)
    return []
  }
}

function writeDeferredCodes(codes) {
  try {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(DEFERRED_STORAGE_KEY, JSON.stringify((codes || []).slice(-DEFERRED_MAX)))
  } catch (e) {
    // 配额满 / 隐私模式：不影响本次会话的跳过（内存里仍然有效）
    console.warn('[review] 保存本机跳过记录失败（仅影响下次打开）：', e?.message || e)
  }
}

export const useReviewStore = defineStore('review', () => {
  /** 侧栏角标（两个独立计数） */
  const pendingCount = ref(0)
  const disputedCount = ref(0)

  const activeTab = ref('pending')

  // ---- Tab1 待确认 ----
  const pendingItems = ref([])
  /** 队列总数（**含**本会话/本机跳过的脸）—— 与侧栏 pendingCount 同一个数 */
  const pendingTotal = ref(0)
  /** 还要人处理多少条（**不含**跳过）—— 进度文案里的"剩余"用它 */
  const pendingRemaining = ref(0)
  /**
   * 取页用的页码。**恒为 1**（保留导出只为兼容）：新的取页方式是
   * 「page=1 + 把手上已有的脸作为排除集发过去」，不靠 offset 往后翻，
   * 理由见 `fetchPending()` 的注释。
   */
  const pendingPage = ref(1)
  /** 当前处理到第几条（0 起）。确认/跳过会把该条从本地摘掉，游标不动 = 自动前进 */
  const cursor = ref(0)
  const pendingLoading = ref(false)
  /** 服务端说「没有更多了」（exhausted / scanCapped）——避免空翻 */
  const pendingExhausted = ref(false)
  /** 后台补页是否在飞（防并发重复补页） */
  const pendingFetching = ref(false)
  /** 正在提交哪一条（禁用重复点击） */
  const busyFaceCode = ref('')

  /**
   * 「跳过」过的 faceCode —— **跳过 = 排到队列最后**，不是"不再看"。
   * ---------------------------------------------------------------
   * 跳过**不写库**（永久排除是「陌生人」，那是另一件事），只是让这些脸
   * 排到所有没跳过的之后。这份集合直接作为 `excludeFaceCodes` 发给服务端，
   * 由它一次扫描就排除掉（旧实现是"取回来再本地重排 + 串行补页"，实测一次
   * 点击 11 页 = 3 秒多）。
   *
   * 跨会话：存 localStorage（见 DEFERRED_STORAGE_KEY 的注释）。刷新/重开浏览器
   * 之后跳过的脸仍然排到最后，直到用户点「重新显示已跳过的 N 条」。
   * ⚠️ 它**不是**"永久排除"：那件事必须写库（陌生人 / 未知），入口在改判浮层。
   */
  const deferredCodes = ref(readDeferredCodes())
  const deferredSet = computed(() => new Set(deferredCodes.value))
  /** 本地记录条数（含已经不在队列里的历史项）—— 决定"要不要显示重看入口" */
  const deferredCount = computed(() => deferredCodes.value.length)
  /** 其中**此刻仍然待确认**的条数（服务端算的，进度文案与按钮数用它） */
  const deferredPending = ref(0)

  // ---- Tab2 我不同意 ----
  const disputedGroups = ref([])
  const disputedTotal = ref(0)
  const disputedPhotoTotal = ref(0)
  const disputedPage = ref(1)
  const disputedLoading = ref(false)
  const busyPhotoCode = ref('')

  /** 本次会话已处理条数（「剩余 N · 已完成 M」） */
  const doneCount = ref(0)

  // ---- 辅助数据 ----
  /** photoCode -> /api/photos/{code} 详情（左侧大图 + 人脸框需要 bbox） */
  const photoCache = ref({})
  /** personCode -> personSummary（候选的「关系提示」来自 pb_person.relation） */
  const personDirectory = ref({})
  /** 批量确认抽屉：当前簇的成员 */
  const clusterMembers = ref([])
  const clusterCode = ref('')
  const clusterLoading = ref(false)

  const badgeTotal = computed(() => pendingCount.value + disputedCount.value)
  const hasBadge = computed(() => badgeTotal.value > 0)

  const current = computed(() => pendingItems.value[cursor.value] || null)
  /**
   * 还要人处理多少条（= 总数 - 跳过里仍待确认的）。
   *
   * ⚠️ 三个数**各有各的用途，不能互相顶替**（R17b 明确分家）：
   *    `pendingTotal`    队列总数，含跳过的 —— 与侧栏「待确认」角标同一个数；
   *    `remaining`（本处）不含跳过的 —— 进度文案"剩余 N 条"，
   *                      它必须能真的被点到 0，否则进度是个谎；
   *    `doneCount`       本次会话已处理条数，只用于"本次已处理 N 条"文案。
   *    旧写法 `pendingTotal - doneCount` 是错的：pendingTotal 已经是服务端
   *    的**最新**总数（写接口回带），再减一次就是双重扣减（一次掉 2）。
   */
  const remaining = computed(() => Math.max(0, pendingRemaining.value))

  /** 角标取值（接口还没接上时为 0，侧栏显示「0」而不是隐藏按钮） */
  function badgeOf(tabName) {
    return tabName === 'disputed' ? disputedCount.value : pendingCount.value
  }

  /** 吃写接口回带的两个计数（少一次往返） */
  function applyCounts(data) {
    if (!data) return
    if (data.pendingCount !== undefined) pendingCount.value = Number(data.pendingCount) || 0
    if (data.disputedCount !== undefined) disputedCount.value = Number(data.disputedCount) || 0
  }

  async function fetchBadge() {
    const data = await getPendingCount()
    applyCounts(data)
    return data
  }

  /**
   * 取一页待确认（`reset` = 丢弃本地缓冲、从头取）。
   *
   * 与旧实现的根本差别：**排除集交给服务端**，不再"取回来再本地重排 + 串行补页"。
   * 排除集 = 本会话跳过的脸 + 手上已加载但还没处理的脸。
   *
   * 为什么排除集里要带上"已加载的"
   * ------------------------------
   *   服务端的 offset 作用在**排除之后**的结果流上。用户处理/跳过几条后，
   *   同一页码的内容已经变了（前面几条离队了），照旧递增 page 必然重/漏。
   *   固定 page=1 + 排除"我手上已有的"，拿到的永远是**紧接在本地缓冲之后
   *   的那一段** —— 不重不漏，也不依赖 offset 的语义。
   *
   * ⚠️ 「还有没有下一页」看响应的 `exhausted`（或 `scanCapped`），
   *    不看 `items.length < total`：有排除集时后者永远成立，会无限翻页。
   */
  async function fetchPending({ reset = false, silent = false } = {}) {
    if (reset) {
      pendingPage.value = 1
      cursor.value = 0
      pendingExhausted.value = false
    } else if (pendingExhausted.value || pendingFetching.value) {
      return null
    }
    if (!silent) pendingLoading.value = true
    pendingFetching.value = true
    try {
      const data = await getPending({
        page: 1,
        size: PENDING_PAGE_SIZE,
        topN: 5,
        // reset 时旧缓冲已作废，不能把它算进排除集（否则会把要取的都排除掉）
        excludeFaceCodes: reset ? [...deferredCodes.value] : exclusionCodes(),
        // 只把"跳过的"报给服务端算剩余 —— "手里已有的"也是排除，
        // 但那些脸还没处理，不能从"还要处理多少条"里扣掉
        skippedFaceCodes: [...deferredCodes.value],
      })
      const list = data?.items ?? []
      pendingItems.value = reset ? list : [...pendingItems.value, ...list]
      pendingTotal.value = Number(data?.total ?? pendingTotal.value) || 0
      // 「剩余」由服务端给（总数 - 跳过里仍待确认的）：
      // 前端自己算的话得知道"我发过去的哪些其实已经不在队列里"，那是瞎猜。
      pendingRemaining.value = Number(data?.remaining ?? pendingTotal.value) || 0
      deferredPending.value = Number(data?.excludedPending ?? 0) || 0
      pendingExhausted.value = Boolean(data?.exhausted || data?.scanCapped)
      return data
    } finally {
      pendingLoading.value = false
      pendingFetching.value = false
    }
  }

  /** 发给服务端的排除集：跳过的 + 手上已加载的（后者可能被"排除后重取"再拿一次）。 */
  function exclusionCodes() {
    const codes = new Set(deferredCodes.value)
    for (const item of pendingItems.value) {
      if (item?.faceCode) codes.add(item.faceCode)
    }
    return [...codes]
  }

  /**
   * 低水位补货：本地缓冲快见底时**后台静默**补一页。
   *
   * 为什么不 await 在点击路径里：用户要的是"点完立刻看到下一条"。
   * 补页是后台事 —— 成功就多 20 条，失败手上还有几条顶着，下次动作会再试。
   */
  async function ensureBuffer() {
    if (pendingExhausted.value) return
    if (pendingItems.value.length - cursor.value > PENDING_LOW_WATER) return
    try {
      await fetchPending({ silent: true })
    } catch (e) {
      // 补页失败不该打断正在逐条处理的用户
      console.warn('[review] 静默补页失败：', e?.message || e)
    }
  }

  /**
   * 把已经离队（或已跳过）的几条从本地缓冲里摘掉；**游标不动 = 自动前进**。
   *
   * 为什么不再整队重取：这些脸在服务端已经离队（assign/stranger 都改了归属），
   * 本地摘掉与"重新取 page 1"得到的结果等价，代价却从「1 次写 + N 次读」
   * 降到「1 次写」。旧链路实测一次点击要**串行取 11 页**（plan 里有日志证据）。
   */
  function dropLocal(codes, { countAsDone = true } = {}) {
    const gone = new Set((codes || []).filter(Boolean))
    if (!gone.size) return
    const before = pendingItems.value.length
    pendingItems.value = pendingItems.value.filter((item) => !gone.has(item?.faceCode))
    const removed = before - pendingItems.value.length
    if (countAsDone) doneCount.value += removed
    // 游标不动：被摘掉的正是它指向的那条，后面的自然顶上。
    // 只有"摘到比游标还靠前/摘空了"才需要夹住，避免 current 变成 undefined 而不自知。
    if (cursor.value > pendingItems.value.length) cursor.value = pendingItems.value.length
  }

  /**
   * 写接口回带的 pendingCount 就是队列的最新总数，顺手同步两个进度数。
   *
   * 「剩余」这里只能**推定**：总数减掉上次取页时得知的"跳过里仍待确认"的条数。
   * 严格说那批数字在写操作之后可能又变了（跳过的脸也可能被别处处理掉），
   * 但差一条的进度不会误导用户 —— 下一次取页（低水位补页）就会拿到准确值。
   */
  function syncPendingTotal(data) {
    if (data?.pendingCount === undefined) return
    pendingTotal.value = Math.max(0, Number(data.pendingCount) || 0)
    pendingRemaining.value = Math.max(0, pendingTotal.value - Math.max(0, deferredPending.value))
  }

  async function fetchDisputed({ reset = true } = {}) {
    if (reset) disputedPage.value = 1
    disputedLoading.value = true
    try {
      const data = await getDisputed({
        page: disputedPage.value,
        groupByPhoto: 1,
        size: 20,
      })
      disputedGroups.value = data?.items ?? []
      // ⚠️ 两个总数**不是一回事**（api/review.py 的说明）：
      //    total = 人脸行数（数据口径），photoGroupTotal = 照片组数（分页口径）。
      //    分页器必须用后者，否则 37 张脸分布在 5 张照片里时会翻出 2 个空页。
      disputedTotal.value = data?.total ?? 0
      disputedPhotoTotal.value = data?.photoGroupTotal ?? 0
      return data
    } finally {
      disputedLoading.value = false
    }
  }

  /**
   * 左侧大图需要的照片详情（relPath 拿文件名、faces[] 里才有 bbox 能画人脸框）。
   * 带缓存：同一张照片里的另一张脸被处理时不用重取。
   */
  async function ensurePhoto(photoCode) {
    const code = String(photoCode || '')
    if (!code) return null
    if (photoCache.value[code]) return photoCache.value[code]
    const data = await getPhoto(code)
    photoCache.value = { ...photoCache.value, [code]: data }
    return data
  }

  /**
   * 人物名录（候选的「关系提示」来自 pb_person.relation）。
   * 只取一次：几十~几百人，一次全查比按需查便宜。
   */
  async function ensurePersonDirectory() {
    if (Object.keys(personDirectory.value).length) return personDirectory.value
    const data = await listPersons({ page: 1, size: 200, orderBy: 'displayName' })
    const map = {}
    for (const one of data?.items ?? []) map[one.personCode] = one
    personDirectory.value = map
    return map
  }

  /**
   * 全库人物模糊搜索 —— 改判/确认浮层那个搜索框走这里。
   *
   * 为什么不复用候选（topCandidates）：
   *   候选只有 topN=5 条**相似度最高**的人，而用户搜的往往是
   *   「我知道他是谁，但机器没把这条脸排进前 5」（表弟、外婆、 seldom 出现的同事）。
   *   在那 5 条里 filter 一个名字，结果几乎永远是空 —— 表现就是「搜索完全没起作用」。
   *   服务端 /persons?keyword= 是对**全库**做 LIKE（displayName / familyName / email / phone）。
   *
   * ⚠️ 相似度必须留 null：搜索结果不是「比对出来的候选」，
   *    硬凑一个数字比不给更坏（FixFaceDialog 会把 null 渲染成「—」而不是百分比）。
   */
  async function searchPersons(keyword, { size = 20 } = {}) {
    const text = String(keyword || '').trim()
    if (!text) return []
    const data = await listPersons({
      page: 1,
      size,
      keyword: text,
      orderBy: 'displayName',
      desc: 0,
    })
    return (data?.items ?? []).map((one) => ({
      ...one,
      similarity: null,
      relation: one.relation || personDirectory.value[one.personCode]?.relation || '',
    }))
  }

  /** 给候选补上关系提示（后端 topCandidates 不带 relation） */
  function decorateCandidates(candidates) {
    return (candidates || []).map((candidate) => {
      const person = personDirectory.value[candidate.personCode]
      return {
        ...candidate,
        // 姓名以人物目录（/api/persons，直读 pb_person.displayName）为准：
        // 候选里的 displayName 是后端 scorer 顺手带上的，它对空姓名会用
        // personCode 兜底（'CS_BaoRui_Zhang' 这种），而前端把 personCode
        // 显示成人名是最糟的一种错 —— 用户只看到一串编码。
        // 目录是分页快照（只加载前 N 个），所以留两级兜底。
        displayName: person?.displayName || candidate.displayName || candidate.personCode,
        avatarFaceCode: candidate.avatarFaceCode || person?.avatarFaceCode || null,
        // ⚠️ 目录（/api/persons）里带的是服务端解析好的**实际展示那张**
        //    （coverFaceCode，DR-40）。不带上它的话，这些人只能靠队列侧的
        //    `avatarFaceCode`（= 用户手工指定的默认，绝大多数为空）⇒ 改判浮层
        //    的候选/搜索一列里只有少数几个有头像，其余是空圆。见 utils/avatar.js。
        coverFaceCode: candidate.coverFaceCode || person?.coverFaceCode || null,
        relation: person?.relation || '',
      }
    })
  }

  const currentCandidates = computed(() => decorateCandidates(current.value?.topCandidates))

  /**
   * 确认当前这张脸属于某人（Tab1 的主动作）。
   * 走 PUT /review/{faceCode}/assign —— 幂等，重复点不会写坏。
   * 响应里带回该人的 photoCount/faceCount（**照片数立即更新**，验收第 5 条）
   * 与两个角标，所以不需要再拉一次。
   *
   * ⚠️ 确认后**只做本地前进**，不再 `fetchPending({reset:true})`：
   *    这张脸已经在服务端离队，重取整队得到的结果和"本地摘掉它"等价，
   *    而重取要付一次完整取页（旧链路上还要连带串行补页）。
   */
  async function confirmCandidate(candidate) {
    const face = current.value
    if (!face || !candidate?.personCode) return null
    busyFaceCode.value = face.faceCode
    try {
      const data = await assignFace(face.faceCode, { personCode: candidate.personCode })
      applyCounts(data)
      syncPendingTotal(data)
      dropLocal([face.faceCode])
      await ensureBuffer()
      return data
    } finally {
      busyFaceCode.value = ''
    }
  }

  /**
   * 跳过：**把这一条排到队列最后**（不是"不看了"——它仍在待确认里，
   * 只是排到所有没跳过的脸之后）。只改前端会话状态，**不写库**。
   *
   * 实现：把 faceCode 记进排除集 -> 立刻从本地缓冲摘掉（= 看到下一条）-> 低水位补货。
   * 排除集随下一次取页发给服务端，所以刚跳过的脸不会再被摆到眼前。
   */
  async function skip() {
    const face = current.value
    if (face?.faceCode) {
      if (!deferredSet.value.has(face.faceCode)) {
        deferredCodes.value.push(face.faceCode)
        // 超出上限时由 writeDeferredCodes 从头部截断（FIFO）：
        // 最先跳过的先回来，与"这一轮先放一放"的语义一致
        writeDeferredCodes(deferredCodes.value)
      }
      deferredPending.value = Math.min(deferredCodes.value.length, deferredPending.value + 1)
      pendingRemaining.value = Math.max(0, pendingRemaining.value - 1)
      dropLocal([face.faceCode], { countAsDone: false })
    }
    await ensureBuffer()
    return null
  }

  /**
   * 「重新显示已跳过的 N 条」：清空跳过记录（内存 + 本机存储），回到队首重来。
   * ⚠️ 清的是**本机**记录，服务端没存过这份东西，所以不存在"清不干净"。
   */
  async function showDeferred() {
    deferredCodes.value = []
    deferredPending.value = 0
    writeDeferredCodes([])
    await fetchPending({ reset: true })
  }

  /** 忽略此人脸 = 标记为陌生人（**永久排除**，入口处必须有二次确认） */
  function ignoreCurrent() {
    const face = current.value
    if (!face) return Promise.resolve(null)
    return fix('stranger', [face.faceCode])
  }

  /**
   * 改判（唯一纠错入口）。三种 action 共用。
   * ⚠️ stranger 永久排除该脸，调用方必须已做二次确认。
   * 成功后：两个角标与队列总数从响应里更新 + **本地前进**（不再整队重取）。
   *
   * `withCentroid: 0` —— 响应里那段「新旧两人的全部质心桶」是排障用的明细，
   * 界面从不读它，却要为每个人物查一次质心全表；逐条处理时必须关掉。
   */
  async function fix(action, faceCodes, personCode) {
    const codes = Array.isArray(faceCodes) ? faceCodes : [faceCodes].filter(Boolean)
    if (!codes.length) return null
    const data = await fixFace(
      { action, faceCodes: codes, personCode },
      { withCentroid: 0 },
    )
    applyCounts(data)
    syncPendingTotal(data)
    if (activeTab.value === 'disputed') await fetchDisputed({ reset: true })
    if (action === 'unknown') {
      // 退回待确认：这些脸**回到队列**了，本地缓冲里没有它们 ——
      // 不重取整队，下次补页（或用户重新进入页面）自然会带上。
      pendingExhausted.value = false
    } else {
      // assign / stranger：已离队 -> 本地摘掉，游标不动 = 自动前进到下一条
      dropLocal(codes)
    }
    await ensureBuffer()
    return data
  }

  /** 整张照片否决：这些脸退回待确认（= fix('unknown') 批量） */
  function fixWholePhoto(group) {
    const codes = (group?.faces || []).map((f) => f.faceCode).filter(Boolean)
    return fix('unknown', codes)
  }

  /**
   * **确认**「我不同意」里的一张脸：机器认的没错。
   * --------------------------------------------------
   * 走 PUT /review/{faceCode}/assign（= assigner.confirm）：
   *   isConfirmed 0 -> 1、link source=1、**立即重算质心**。
   * 效果有两层：① 这张脸离开「我不同意」（它不再"机器认的、还没人确认"）；
   *             ② 它成为该人质心的**训练样本** —— 确认得越多，自动匹配越准。
   *
   * ⚠️ 与「改判 / 拆分」的区别：那两者改的是**归属**（认成谁），
   *    确认只改**谁认过**（isConfirmed），归属一个人都不动。
   *    所以它不需要选人、不需要浮层 —— 一次点击就是正确语义。
   */
  async function confirmDisputedFace(face) {
    const code = String(face?.faceCode || '')
    const person = String(face?.personCode || '')
    if (!code || !person) return null
    busyPhotoCode.value = String(face?.photoCode || '')
    try {
      const data = await assignFace(code, { personCode: person })
      applyCounts(data)
      await refreshDisputedPage()
      return data
    } finally {
      busyPhotoCode.value = ''
    }
  }

  /**
   * 刷新「我不同意」列表但**留在当前页**（逐条处理的动作不能让列表跳回第一页）。
   *
   * ⚠️ 与 `fetchDisputed({ reset: true })`（fix/整张否决在用）的取舍：
   *    那些动作后面通常跟着"重看一遍队首"，而确认/整张确认是**一条一条往下清**，
   *    每点一次就跳回 page 1 等于让用户反复从头开始翻。
   * ⚠️ 本页被处理空时必须**退一页**，否则用户看到的是一页"暂无"而总数还有值
   *    （分页器与 items 对不上，最容易被当成数据丢了）。
   */
  async function refreshDisputedPage() {
    await fetchDisputed({ reset: false })
    if (!disputedGroups.value.length && disputedPage.value > 1) {
      disputedPage.value -= 1
      await fetchDisputed({ reset: false })
    }
  }

  /**
   * **整张确认**：这一张照片里机器认的脸全对。
   * --------------------------------------------------
   * ⚠️ 一张合影里可能认成**好几个人**（group.personCodes 往往不止一个），
   *    而 batch-assign 的契约是「一批脸归给**同一个人**」，所以先按
   *    personCode 分桶、每人一次事务。不这么分就只能逐张 assign
   *    = N 个事务 + N 次质心重算，与「一次点击解决一张照片」正好相反。
   */
  async function confirmDisputedGroup(group) {
    const faces = Array.isArray(group?.faces) ? group.faces : []
    const buckets = new Map()
    for (const face of faces) {
      const person = String(face?.personCode || '')
      const code = String(face?.faceCode || '')
      if (!person || !code) continue
      if (!buckets.has(person)) buckets.set(person, [])
      buckets.get(person).push(code)
    }
    if (!buckets.size) return null
    busyPhotoCode.value = String(group?.photoCode || '')
    try {
      let assigned = 0
      for (const [personCode, faceCodes] of buckets) {
        const data = await batchAssign({ faceCodes, personCode })
        applyCounts(data)
        assigned += Number(data?.assigned) || 0
      }
      await refreshDisputedPage()
      return { assigned }
    } finally {
      busyPhotoCode.value = ''
    }
  }

  /**
   * 批量确认：取当前这张脸所在簇的全部成员，一次 POST /review/batch-assign。
   * 验收第 6 条「50 张 ≤3 次点击」：点「批量确认」→ 点对话框里的「确认」= 2 次。
   */
  async function openCluster(cluster) {
    const code = String(cluster || current.value?.clusterCode || '')
    if (!code) return null
    clusterCode.value = code
    clusterLoading.value = true
    try {
      const data = await getClusterMembers(code, { size: 200 })
      clusterMembers.value = data?.members ?? []
      return clusterMembers.value
    } finally {
      clusterLoading.value = false
    }
  }

  async function batchConfirm(personCode) {
    const codes = clusterMembers.value.map((m) => m.faceCode).filter(Boolean)
    if (!codes.length || !personCode) return null
    const data = await batchAssign({ faceCodes: codes, personCode })
    applyCounts(data)
    syncPendingTotal(data)
    // 整簇离队：本地摘掉即可（不再整队重取）。簇里可能有尚未加载的脸，
    // 那些本来就不在本地缓冲里，摘不到也不影响。
    dropLocal(codes)
    clusterMembers.value = []
    clusterCode.value = ''
    await ensureBuffer()
    return data
  }

  /**
   * **拆分**：把一张脸真正换个人（POST /review/split）
   * --------------------------------------------------
   * 为什么不复用 fix()：
   *   fix('unknown')  = 丢回待确认队列，**不记「谁本来属于谁」**，日志 isRevertible=0；
   *   split           = 换一个主人，落 opType=SPLIT、isRevertible=1 的日志，
   *                    撤销能把双方归属**和质心**一起还原。
   * 「他其实是我表弟」这种修正只有后者表达得了，所以「拆分」必须是独立端点。
   *
   * personCode 三个取值：
   *   ''                -> 退回待确认队列（等价 fix('unknown')，但**可撤销**）
   *   库里有这个人       -> 归给他
   *   给了 displayName  -> 服务端自动建档再归给他（merger.split 的行为）
   */
  async function splitOne(faceCode, personCode, form) {
    const code = String(faceCode || '').trim()
    if (!code) throw new Error('拆分必须给 faceCode')
    const payload = { faceCode: code, personCode: String(personCode || '') || null }
    if (!payload.personCode) {
      // 只在「拆给一个还不存在的人」时才带 displayName / birthday；
      // 退回待确认时带它们没有意义（没建档），带上反而会让人以为建了档。
      const name = String(form?.displayName || '').trim()
      if (name) {
        payload.displayName = name
        payload.birthday = form?.birthday || undefined
      }
    }
    const data = await splitFace(payload)
    applyCounts(data)
    // 拆分会让两个档案的照片数/人脸数都变，目录缓存直接作废
    personDirectory.value = {}
    return data
  }

  /**
   * 新建人物并**立即把给定的脸归属给他**（设计稿 §4.8 第 ⑧ 条）。
   * 建完再走 fix('assign') —— 服务端会重算这个新人的质心。
   */
  async function createPersonAndAssign(form, faceCodes) {
    const payload = {
      displayName: String(form.displayName || '').trim(),
      familyName: form.familyName || undefined,
      relation: form.relation || undefined,
      familyGroupCode: form.familyGroupCode || undefined,
      birthday: form.birthday || undefined,
      email: form.email || undefined,
      phone: form.phone || undefined,
      categories: Array.isArray(form.categories) ? form.categories : [],
    }
    const created = await createContact(payload)
    const personCode =
      created?.personCode || created?.person?.personCode || created?.item?.personCode
    if (!personCode) throw new Error('新建人物成功但响应里没有 personCode')
    const result = await fix('assign', faceCodes, personCode)
    personDirectory.value = {}
    await ensurePersonDirectory()
    return { personCode, result }
  }

  function setTab(name) {
    activeTab.value = REVIEW_TABS.some((tab) => tab.name === name) ? name : 'pending'
  }

  function closeCluster() {
    clusterCode.value = ''
    clusterMembers.value = []
  }

  return {
    pendingCount,
    disputedCount,
    activeTab,
    pendingItems,
    pendingTotal,
    pendingRemaining,
    pendingPage,
    cursor,
    pendingLoading,
    pendingExhausted,
    busyFaceCode,
    deferredCodes,
    deferredCount,
    deferredPending,
    disputedGroups,
    disputedTotal,
    disputedPhotoTotal,
    disputedPage,
    disputedLoading,
    busyPhotoCode,
    doneCount,
    photoCache,
    personDirectory,
    clusterMembers,
    clusterCode,
    clusterLoading,
    current,
    currentCandidates,
    remaining,
    badgeTotal,
    hasBadge,
    badgeOf,
    applyCounts,
    fetchBadge,
    fetchPending,
    fetchDisputed,
    ensurePhoto,
    ensurePersonDirectory,
    searchPersons,
    decorateCandidates,
    confirmCandidate,
    skip,
    showDeferred,
    ignoreCurrent,
    fix,
    fixWholePhoto,
    confirmDisputedFace,
    confirmDisputedGroup,
    splitOne,
    openCluster,
    closeCluster,
    batchConfirm,
    createPersonAndAssign,
    setTab,
  }
})
