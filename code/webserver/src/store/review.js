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

export const useReviewStore = defineStore('review', () => {
  /** 侧栏角标（两个独立计数） */
  const pendingCount = ref(0)
  const disputedCount = ref(0)

  const activeTab = ref('pending')

  // ---- Tab1 待确认 ----
  const pendingItems = ref([])
  const pendingTotal = ref(0)
  const pendingPage = ref(1)
  /** 当前处理到第几条（0 起）。确认后自动 +1 —— 队列「自动前进」靠它 */
  const cursor = ref(0)
  const pendingLoading = ref(false)
  /** 正在提交哪一条（禁用重复点击） */
  const busyFaceCode = ref('')

  /**
   * 本会话「跳过」过的 faceCode —— **跳过 = 排到队列最后**，不是"不再看"。
   * ---------------------------------------------------------------
   * 旧实现只把游标往前挪一格、**一个字都不写库**，于是：
   *   队列每次重取（改判 fix / 批量确认 / 离开页面再回来都会
   *   `fetchPending({reset:true})`）都会回到 page 1，而跳过的脸 quality
   *   没变、仍按 quality DESC 排在最前面 ⇒ 刚跳过它马上又被问到，
   *   表现正是「很快又要选用一次」。
   * 这里记住跳过的 faceCode，并把它们稳定地重排到**已加载列表的最后**：
   * 只要队列里还有没跳过的脸，跳过的就不会再被摆到眼前。
   * ⚠️ 会话级（不写库、不进 localStorage）：刷新页面即恢复原序 ——
   *    这是有意的，跳过是"这一轮先放一放"，不是永久决定（永久排除是陌生人）。
   */
  const deferredCodes = ref([])
  const deferredSet = computed(() => new Set(deferredCodes.value))
  const deferredCount = computed(() => deferredCodes.value.length)

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
  const remaining = computed(() => Math.max(0, pendingTotal.value - doneCount.value))

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

  async function fetchPending({ reset = false } = {}) {
    if (reset) {
      pendingPage.value = 1
      cursor.value = 0
    }
    pendingLoading.value = true
    try {
      const data = await getPending({ page: pendingPage.value, size: PENDING_PAGE_SIZE, topN: 5 })
      const list = data?.items ?? []
      pendingItems.value = reset ? list : [...pendingItems.value, ...list]
      pendingTotal.value = data?.total ?? 0
      applyDeferOrder()
      // 重取后 page 1 的头几条可能正是"跳过的" —— 先把后续页拉进来，
      // 保证游标处出现的永远是"没跳过"的脸（否则改判一次就又看到它）
      if (reset) await loadUntilFresh()
      return data
    } finally {
      pendingLoading.value = false
    }
  }

  /**
   * 把「已跳过」的条目稳定地挪到**已加载列表的最后**。
   *
   * ⚠️ 必须**稳定**（同组内相对顺序不变）：服务器页是按 quality DESC 分页的，
   *    打乱同组顺序 = "翻页时条目在眼前挪位"，那是分页最忌讳的
   *    （见 processor/review/queue.py 里 pendingQueue 的设计约束）。
   * ⚠️ 只能**重排**、不能**过滤**：`pendingItems.length < pendingTotal`
   *    是"服务器还有下一页"的判据；一旦把跳过的条目从数组里删掉，
   *    这个判据就失真（会提前停止翻页 / 漏掉没跳过的脸）。
   */
  function applyDeferOrder() {
    if (!deferredSet.value.size) return
    const fresh = []
    const deferred = []
    for (const item of pendingItems.value) {
      (deferredSet.value.has(item.faceCode) ? deferred : fresh).push(item)
    }
    pendingItems.value = [...fresh, ...deferred]
  }

  /**
   * 保证游标处是一条「没跳过」的脸；否则把服务器后续页拉进来。
   *
   * 服务器也拉完了就收尾：若游标处剩下的确实只有"跳过的"，
   * 把游标推到末尾（视为本轮清空）—— 否则按一次「跳过」会立刻又看到它，
   * 那正是要修的问题。收尾后 UI 会给出「重新显示已跳过的 N 条」入口。
   */
  async function loadUntilFresh() {
    for (;;) {
      const at = pendingItems.value[cursor.value]
      if (at && !deferredSet.value.has(at.faceCode)) return
      if (pendingItems.value.length >= pendingTotal.value) {
        if (at) cursor.value = pendingItems.value.length
        return
      }
      const before = pendingItems.value.length
      pendingPage.value += 1
      await fetchPending()
      if (pendingItems.value.length <= before) {
        // 防御：页面没长进就别死循环（例如 total 与实际行数不一致）
        if (at) cursor.value = pendingItems.value.length
        return
      }
    }
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

  /** 队列到底了就再取一页（不重置 cursor，否则会跳回队首） */
  async function advance() {
    cursor.value += 1
    doneCount.value += 1
    await loadUntilFresh()
  }

  /**
   * 确认当前这张脸属于某人（Tab1 的主动作）。
   * 走 PUT /review/{faceCode}/assign —— 幂等，重复点不会写坏。
   * 响应里带回该人的 photoCount/faceCount（**照片数立即更新**，验收第 5 条）
   * 与两个角标，所以不需要再拉一次。
   */
  async function confirmCandidate(candidate) {
    const face = current.value
    if (!face || !candidate?.personCode) return null
    busyFaceCode.value = face.faceCode
    try {
      const data = await assignFace(face.faceCode, { personCode: candidate.personCode })
      applyCounts(data)
      pendingTotal.value = Math.max(0, pendingTotal.value - 1)
      await advance()
      return data
    } finally {
      busyFaceCode.value = ''
    }
  }

  /**
   * 跳过：**把这一条排到队列最后**（不是"不看了"——它仍在待确认里，
   * 只是排到所有没跳过的脸之后）。只改前端会话状态，**不写库**。
   *
   * 与旧实现的差别：旧的就是"游标 +1"，队列一重取（改判 / 批量确认 /
   * 离开再回来）跳过的脸又会冒到最前面。现在每次重排后游标不动 =
   * 自动落到下一条，被跳过的已沉到已加载列表末尾。
   */
  async function skip() {
    const face = current.value
    if (face?.faceCode) {
      if (!deferredSet.value.has(face.faceCode)) deferredCodes.value.push(face.faceCode)
      applyDeferOrder()
    }
    await loadUntilFresh()
    return null
  }

  /** 「重新显示已跳过的 N 条」：清空本会话的跳过记录，回到队首重来。 */
  async function showDeferred() {
    deferredCodes.value = []
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
   * 成功后：两个角标从响应里更新 + 队列重取（被改判的那几条已经离队）
   */
  async function fix(action, faceCodes, personCode) {
    const codes = Array.isArray(faceCodes) ? faceCodes : [faceCodes].filter(Boolean)
    if (!codes.length) return null
    const data = await fixFace({ action, faceCodes: codes, personCode })
    applyCounts(data)
    if (activeTab.value === 'disputed') await fetchDisputed({ reset: true })
    // 队列里被改判的那几条已离队：重取当前页，cursor 停在原处 = 自动前进到下一条
    await fetchPending({ reset: true })
    return data
  }

  /** 整张照片否决：这些脸退回待确认（= fix('unknown') 批量） */
  function fixWholePhoto(group) {
    const codes = (group?.faces || []).map((f) => f.faceCode).filter(Boolean)
    return fix('unknown', codes)
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
    const assigned = Number(data?.assigned) || 0
    pendingTotal.value = Math.max(0, pendingTotal.value - assigned)
    doneCount.value += assigned
    clusterMembers.value = []
    clusterCode.value = ''
    await fetchPending({ reset: true })
    cursor.value = 0
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
    pendingPage,
    cursor,
    pendingLoading,
    busyFaceCode,
    deferredCodes,
    deferredCount,
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
    advance,
    confirmCandidate,
    skip,
    showDeferred,
    ignoreCurrent,
    fix,
    fixWholePhoto,
    splitOne,
    openCluster,
    closeCluster,
    batchConfirm,
    createPersonAndAssign,
    setTab,
  }
})
