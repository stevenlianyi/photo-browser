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
      return data
    } finally {
      pendingLoading.value = false
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

  /** 给候选补上关系提示（后端 topCandidates 不带 relation） */
  function decorateCandidates(candidates) {
    return (candidates || []).map((candidate) => {
      const person = personDirectory.value[candidate.personCode]
      return {
        ...candidate,
        displayName: candidate.displayName || person?.displayName || candidate.personCode,
        avatarFaceCode: candidate.avatarFaceCode || person?.avatarFaceCode || null,
        relation: person?.relation || '',
      }
    })
  }

  const currentCandidates = computed(() => decorateCandidates(current.value?.topCandidates))

  /** 队列到底了就再取一页（不重置 cursor，否则会跳回队首） */
  async function advance() {
    cursor.value += 1
    doneCount.value += 1
    if (
      cursor.value >= pendingItems.value.length &&
      pendingItems.value.length < pendingTotal.value
    ) {
      pendingPage.value += 1
      await fetchPending()
      cursor.value = Math.min(cursor.value, Math.max(0, pendingItems.value.length - 1))
    }
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

  /** 跳过：只是不看这一条，**不写库**（下次进来它还在队列里） */
  async function skip() {
    if (cursor.value < pendingItems.value.length - 1) {
      cursor.value += 1
      return null
    }
    if (pendingItems.value.length < pendingTotal.value) {
      pendingPage.value += 1
      await fetchPending()
      return null
    }
    cursor.value = pendingItems.value.length
    return null
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
    decorateCandidates,
    advance,
    confirmCandidate,
    skip,
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
