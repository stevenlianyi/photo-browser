<!--
  P-03 照片详情（设计稿 §4.4）
  ------------------------------------------------------------
  左侧大图 + 人脸框，右侧拍摄信息与「出现的人」。

  四条硬约束
  ----------
  ① **原图只在这里出现**：网格只给缩略图，点击进入本页才发 /api/original
     （验收第 2/3 条）。本页**不提供任何编辑 / 覆盖 / 删除原图的入口** ——
     软删除只改库里的记录，磁盘上的文件一个字节都不动。
  ② **人脸框永远可见**（P0-1），四重编码（色 + 描边 + 图标 + 文字），
     描边再表达归属来源：实线=人工确认 / **虚线=机器自动（可否决）** / 点线=待确认。
  ③ **「✗ 不是他」一次点击可达**（P0-6）：人脸框悬停即出，不绕到人物详情。
  ④ **不可逆操作复述 + 二次确认**（P0-3）：软删除、标记陌生人。

  左右翻页（DR-31，A 档：纯前端）
  ----------------------------------------
  ⑤ `←` `→` 与两侧浮动箭头，**默认**翻的是 `store.photos.items` 里相邻的那一张 ——
     入口仍然只有 `/photos/:photoCode`（可分享、可刷新），**没有新接口**。
     分母用**已加载条数**而不是后端 `total`；到头**不循环**。
     ⚠️ 从人物 / 地点详情进来时，入口会带 `?scope=`，翻的是**那一批照片**
     （按需分页，往下翻自动续下一页），而不是全库 —— 否则在「白瑞琴的照片」里
     翻两张就翻到陌生人的照片上，界面上没有任何东西提示这件事发生了。
     约定见 `utils/photoReturn.js` 顶部；没有 `scope` 的入口（待确认 / 概览 /
     重复对比）行为一字不变，那几种情况仍走「不在列表里就禁用 + 给说明」。
     ⚠️ **改判进行中禁翻**：改判会改掉这张脸的人脸框三态与侧栏「出现的人」，
     请求没回来就跳走 ⇒ 回来时状态陈旧，**而且不报错**。所以 busy 覆盖
     改判 / 确认 / 标记重复 / 软删除 / 翻页，成功后先 `reloadPhoto()` 再解禁。

  关于 Range
  ---------
  /api/original 支持 Range（206 + Content-Range）。本页会先发一个
  `Range: bytes=0-65535` 的探测请求：① 把「分段可用」变成 Network 面板里
  **看得见**的事实（右侧「原图分段」行显示状态码与 Content-Range）；
  ② 提前把连接与首字节热起来。
  ⚠️ 缩放平移**不靠 Range 拼 JPEG 分片** —— JPEG 不整体解码就没有局部，
  那样做只会得到半张花屏。放大后拖动是 CSS 缩放 + 原生滚动，已解码的位图
  不再发任何请求。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft,
  Copy,
  Download,
  Maximize2,
  Pencil,
  Search,
  Trash2,
  Undo2,
  X,
} from 'lucide-vue-next'
import FaceBox from '@/components/photo/FaceBox.vue'
import PhotoPager from '@/components/photo/PhotoPager.vue'
import ShotYearFixDialog from '@/components/photo/ShotYearFixDialog.vue'
import FixFaceDialog from '@/components/common/FixFaceDialog.vue'
import { usePhotosStore } from '@/store/photos'
import { useReviewStore } from '@/store/review'
import { useSettingsStore } from '@/store/settings'
import { listPhotos } from '@/api/browse'
// ⚠️ 地点显示名的**唯一入口**（契约：nameZh ?? placeName）。各页自己写一遍
//    `placeZh || placeName` 的后果是「GPS 地点在某页是中文、在某页是英文」,
//    而两处都"看起来正常"，谁也不会发现。
import { listPlacePhotos, placeDisplayName, placeRawName } from '@/api/place'
import {
  getDeleteImpact,
  markDuplicate,
  softDelete,
  unmarkDuplicate,
} from '@/api/photoAction'
import { faceUrl, originalUrl, thumbUrl } from '@/api/static'
import { similarityText } from '@/utils/faceState'
// 「返回」与「翻页范围」的约定（入口声明来源 / 详情页回退）——见文件顶部说明
import {
  goBackOr,
  inheritSource,
  parseScope,
  photoDetailLink,
  photoReturnTarget,
} from '@/utils/photoReturn'
import {
  EMPTY,
  baseName,
  displaySize,
  formatBucketKey,
  formatDate,
  formatDateTime,
  formatFileSize,
  shortHash,
} from '@/utils/format'

const route = useRoute()
const router = useRouter()
const photos = usePhotosStore()
const review = useReviewStore()
const settings = useSettingsStore()

const photo = computed(() => photos.current)
const photoCode = computed(() => String(route.params.photoCode || ''))

/**
 * 「地点」栏的显示名（R5）：**中文优先**。
 *
 * `placeDisplayName()` 同时认 `nameZh`（地点接口）与 `placeZh`（照片接口），
 * 所以行首直接传 `photo` 就够了 —— 不必在这里再抄一遍字段映射。
 *
 * ⚠️ 英文原值**照旧带出来**（tooltip）：排障时要回答「这个中文名是从哪个
 *    英文键算出来的」。丢掉它，中文名算错了就永远查不出来。
 */
const placeText = computed(() => placeDisplayName(photo.value))
const placeTooltip = computed(() => {
  const raw = placeRawName(photo.value)
  return raw ? `英文聚合键：${raw}` : undefined
})

/** 姓名解析：getPhoto 的 faces[] 与 persons[] 是分开的两份数据，要在这里对上 */
const personMap = computed(() => {
  const map = {}
  for (const person of photo.value?.persons || []) map[person.personCode] = person
  return map
})

function nameOf(face) {
  if (!face?.personCode) return ''
  return personMap.value[face?.personCode]?.displayName || face.personCode
}

const faces = computed(() => photo.value?.faces || [])
const pendingFaces = computed(() => faces.value.filter((f) => f.state === 'pending'))

/**
 * 这张脸的候选。
 * 能在已加载的待确认队列里对上就**用真候选**（带相似度）；对不上就退回
 * 「全部人物按姓名列出来」—— 因为任意一张脸都没有现成的相似度可给，
 * 硬编一个数字比不给更坏。
 */
function candidatesOf(face) {
  const entry = review.pendingItems.find((item) => item.faceCode === face?.faceCode)
  if (entry) return review.decorateCandidates(entry.topCandidates)
  return review.decorateCandidates(
    Object.values(review.personDirectory).map((person) => ({
      personCode: person.personCode,
      displayName: person.displayName,
      avatarFaceCode: person.avatarFaceCode,
      similarity: null,
    })),
  )
}

/** 主图容器比例必须等于**显示**方向的比例，否则人脸框的百分比会整体偏移 */
const frameStyle = computed(() => {
  const size = displaySize(photo.value)
  if (!size.width || !size.height) return { width: '100%' }
  return {
    aspectRatio: `${size.width} / ${size.height}`,
    width: `min(100%, calc(70vh * ${size.width} / ${size.height}))`,
  }
})

const fileLabel = computed(() => baseName(photo.value?.relPath) || photoCode.value)

// ---- Range 探测（把「分段可用」变成看得见的事实）----
const rangeProbe = ref({ status: '', contentRange: '' })

async function probeRange() {
  if (!photoCode.value) return
  try {
    const response = await fetch(originalUrl(photoCode.value), {
      headers: { Range: 'bytes=0-65535' },
    })
    await response.arrayBuffer()
    rangeProbe.value = {
      status: String(response.status),
      contentRange: response.headers.get('Content-Range') || '',
    }
  } catch {
    rangeProbe.value = { status: '—', contentRange: '' }
  }
}

async function reloadPhoto() {
  photos.current = await photos.fetchPhoto(photoCode.value)
}

// ---- 改判浮层（三种 action 共用）----
const fixVisible = ref(false)
const fixFaces = ref([])
const fixMode = ref('assign')
const fixCandidates = ref([])
const fixPhotoLabel = ref('')
const fixLoading = ref(false)

const fixSiblings = computed(() =>
  faces.value.filter((face) => !fixFaces.value.some((f) => f.faceCode === face.faceCode)),
)

function openFix(face, mode = 'assign') {
  // ⚠️ 必须把 displayName 一起带过去：FixFaceDialog 的复述要写「当前被认成 <谁>」，
  //    而 getPhoto 的 faces[] 里**只有 personCode 没有姓名**（姓名在 persons[] 里）。
  //    漏了的话浮层会显示成「当前被认成 UI_xxxx」这种编码 —— 用户完全看不懂。
  fixFaces.value = [{ ...face, displayName: nameOf(face) }]
  fixMode.value = mode
  fixCandidates.value = candidatesOf(face)
  fixPhotoLabel.value = photo.value?.relPath || photoCode.value
  fixVisible.value = true
}

/** 侧栏「出现的人」每行的「✗ 改判」：把这个人在这张照片里的脸全带出来 */
function openFixForPerson(person) {
  const owned = faces.value.filter(
    (face) => face.personCode === person.personCode && face.state !== 'stranger',
  )
  if (!owned.length) return
  // 同上：带上姓名，否则浮层的复述里只有 personCode
  fixFaces.value = owned.map((face) => ({ ...face, displayName: person.displayName }))
  fixMode.value = 'assign'
  fixCandidates.value = candidatesOf(owned[0])
  fixPhotoLabel.value = photo.value?.relPath || photoCode.value
  fixVisible.value = true
}

async function submitFix({ action, faceCodes, personCode }) {
  fixLoading.value = true
  try {
    const data = await review.fix(action, faceCodes, personCode)
    review.applyCounts(data)
    // 详情页要立刻反映改判结果（框的颜色 / 文字 /「出现的人」列表）
    await reloadPhoto()
    fixVisible.value = false
    ElMessage.success(
      action === 'unknown'
        ? '已置为未知：这些脸回到「待确认」队列'
        : action === 'stranger'
          ? '已标记为陌生人：这些脸不再出现在任何队列里'
          : '改判完成：该人照片数与两个角标已同步更新',
    )
  } finally {
    fixLoading.value = false
  }
}

async function createPersonAndAssign({ form, faceCodes }, onDone) {
  fixLoading.value = true
  try {
    await review.createPersonAndAssign(form, faceCodes)
    await reloadPhoto()
    fixVisible.value = false
    onDone?.()
    ElMessage.success('已新建人物并把该脸归属给他')
  } finally {
    fixLoading.value = false
  }
}

// ---- 确认归属（未归属的脸）----
const confirmVisible = ref(false)
const confirmFace = ref(null)
const confirmCandidates = ref([])
const confirmKeyword = ref('')
const confirmLoading = ref(false)
/** 与改判浮层同一个毛病：候选池只有 topN 条，搜名字几乎必然搜不到 → 补全库搜索结果 */
const confirmHits = ref([])
/** 本次关键词是否已经问过服务端（决定列表是「搜索结果」还是「候选的本地筛选」） */
const confirmSearched = ref(false)
let confirmTimer = null
let confirmSeq = 0

watch(confirmKeyword, (value) => {
  clearTimeout(confirmTimer)
  confirmSeq += 1
  const text = String(value || '').trim()
  if (!text) {
    confirmHits.value = []
    confirmSearched.value = false
    return
  }
  confirmTimer = setTimeout(async () => {
    const seq = ++confirmSeq
    try {
      const list = await review.searchPersons(text)
      if (seq !== confirmSeq) return
      confirmHits.value = list
    } catch {
      if (seq !== confirmSeq) return
      confirmHits.value = []
    }
    confirmSearched.value = true
  }, 200)
})

/**
 * 列表内容：**有关键词 = 纯搜索结果；没关键词 = 相似度候选**。
 * ⚠️ 不能把候选与搜索结果混排（那是「输入了字却还是一张按相似度排的表」，
 *    搜索框看着像坏了）；命中的人如果本来在候选里，借用他的 similarity。
 */
const confirmList = computed(() => {
  const text = String(confirmKeyword.value || '').trim()
  if (!text) return confirmCandidates.value
  // 还没拿到服务端结果（或这次搜了 0 个）之前，先在候选池里本地筛一次，
  // 别让搜索框看起来是死的
  if (!confirmSearched.value) {
    const low = text.toLowerCase()
    return confirmCandidates.value.filter((c) =>
      String(c.displayName || c.personCode || '').toLowerCase().includes(low),
    )
  }
  const byCode = new Map(confirmCandidates.value.map((c) => [c.personCode, c]))
  return confirmHits.value
    .filter((one) => one?.personCode)
    .map((one) => {
      const hit = byCode.get(one.personCode)
      return hit ? { ...one, similarity: hit.similarity } : { similarity: null, ...one }
    })
})

function openConfirm(face) {
  clearTimeout(confirmTimer)
  confirmHits.value = []
  confirmSearched.value = false
  confirmFace.value = face
  confirmCandidates.value = candidatesOf(face)
  confirmKeyword.value = ''
  confirmVisible.value = true
}

async function doConfirm(candidate) {
  if (!confirmFace.value || !candidate?.personCode) return
  confirmLoading.value = true
  try {
    const data = await review.fix('assign', [confirmFace.value.faceCode], candidate.personCode)
    review.applyCounts(data)
    await reloadPhoto()
    confirmVisible.value = false
    ElMessage.success(`已确认为「${candidate.displayName}」，照片数与角标已更新`)
  } finally {
    confirmLoading.value = false
  }
}

// ---- 标记重复（选一张作为主照片）----
const dupVisible = ref(false)
const dupKeyword = ref('')
const dupCandidates = ref([])
const dupLoading = ref(false)
const dupSaving = ref(false)

async function searchDupCandidates() {
  dupLoading.value = true
  try {
    const data = await listPhotos({ page: 1, size: 20, keyword: dupKeyword.value || undefined })
    dupCandidates.value = (data?.items || []).filter((one) => one.photoCode !== photoCode.value)
  } finally {
    dupLoading.value = false
  }
}

function openDup() {
  dupKeyword.value = ''
  dupCandidates.value = []
  dupVisible.value = true
  searchDupCandidates()
}

async function saveDup(target) {
  dupSaving.value = true
  try {
    await markDuplicate(photoCode.value, target.photoCode)
    await reloadPhoto()
    dupVisible.value = false
    ElMessage.success('已标记为重复：不删除任何文件，随时可取消')
  } finally {
    dupSaving.value = false
  }
}

async function doUnmarkDup() {
  dupSaving.value = true
  try {
    await unmarkDuplicate(photoCode.value)
    await reloadPhoto()
    ElMessage.success('已取消重复标记')
  } finally {
    dupSaving.value = false
  }
}

// ---- 软删除（两段式：先看影响面，再 confirm）----
const delVisible = ref(false)
const delImpact = ref(null)
const delLoading = ref(false)

async function openDelete() {
  delLoading.value = true
  try {
    delImpact.value = await getDeleteImpact(photoCode.value)
    delVisible.value = true
  } finally {
    delLoading.value = false
  }
}

async function doDelete() {
  delLoading.value = true
  try {
    const data = await softDelete(photoCode.value, true)
    review.applyCounts(data)
    delVisible.value = false
    ElMessage.success(
      `已软删除：${data?.facesDeleted ?? 0} 张人脸一并从队列移出。磁盘上的原图没有被改动。`,
    )
  } finally {
    delLoading.value = false
  }
}

// ---- 年代修正（DR-42）：老相册翻拍件 / 扫描件的「拍摄年代」----
/**
 * 为什么这个入口必须留在照片详情页
 * ------------------------------
 *   `pb_photo.shotYear` 是**扫描时**从 EXIF / 文件名 / mtime 读出来的，而老相册的
 *   翻拍件三条链子给的都只是"翻拍那一刻"。用户看到的现象是「这个人的时间轴里
 *   混进了一张 2019」—— 而他要修的是**这张照片是哪一年拍的**，
 *   所以修的地方就该是这张照片的详情页（而不是某个人物页的拖拽）。
 */
/** 年代是不是被人工改过（决定"人工修正"角标显不显示） */
const shotYearOverridden = computed(() => {
  const value = photo.value?.shotYearOverride
  return value !== null && value !== undefined && value !== ''
})

const shotYearVisible = ref(false)

/**
 * 修正成功后：刷新本页 + 让队列角标失效。
 * ⚠️ 修正会**重算质心**（这张照片里每个人在当前年代档的向量），
 *    所以待确认队列的候选顺序也可能变了 —— 不能只刷新本页。
 * ⚠️ `warnings` 非空表示「修正本身成功了，但那个人的**其它**照片还有旧口径年代档键」
 *    （DR-22 前置检查挡下的）。这时必须说出来：静默吞掉它，
 *    用户会在几天后发现"某个人怎么认不准了"而毫无线索。
 */
async function onShotYearFixed(data) {
  await reloadPhoto()
  review.fetchBadge()
  const warnings = data?.warnings || []
  if (warnings.length) {
    ElMessage.warning(
      `年代已修正；但有 ${warnings.length} 个人的历史年代档口径待重算，`
        + '请按其提示跑一次刷新年代档（否则那个人的匹配会静默失准）。',
    )
  }
}

// ---- 缩放查看（CSS 缩放 + 原生滚动，不再发请求）----
const zoomVisible = ref(false)
const zoomPercent = ref(100)
const zoomStyle = computed(() => ({ width: `${zoomPercent.value}%` }))

// ============================================================
// 左右翻页（DR-31）
// ============================================================
// 数据源：默认是全库照片流（`store.photos.items`）；入口声明了 `?scope=` 时
// 改沿**那一批照片**翻（某人的 / 某地点的），约定见 utils/photoReturn.js 顶部。

/** 作用域列表的页大小：与照片流默认页一致（翻页是一次一张，不需要更大） */
const SCOPE_PAGE_SIZE = 60
/** 距已加载末尾还剩这么多张就补下一页，避免「箭头忽然禁用一下再恢复」 */
const SCOPE_PREFETCH_MARGIN = 12

const scopeSpec = computed(() => parseScope(route.query.scope))
const scopeItems = ref([])
const scopeTotal = ref(0)
const scopePage = ref(0)
const scopeLoading = ref(false)
/** 这一批没取到（接口失败）：翻页退回全库列表，而不是留一个全禁用的空列表 */
const scopeFailed = ref(false)

/** 翻页用的列表：有作用域就用那一批，否则用全库照片流 */
const pagerItems = computed(() =>
  (scopeSpec.value && !scopeFailed.value ? scopeItems.value : photos.items),
)

/**
 * 作用域列表按需分页：一个人可能有几千张照片，一次拉完既慢，也把「翻页」
 * 变成一次全表扫描。翻到接近末尾再补一页（见 ensureScopeWindow）。
 */
async function loadScopePage(page) {
  const spec = scopeSpec.value
  if (!spec || scopeLoading.value) return
  scopeLoading.value = true
  try {
    const params = { page, size: SCOPE_PAGE_SIZE, orderBy: 'takenAt', desc: 1 }
    const data = spec.type === 'person'
      ? await listPhotos({ ...params, personCode: spec.code })
      : await listPlacePhotos(spec.code, { ...params, placeCodes: spec.extra || undefined })
    const list = data?.items ?? []
    scopeItems.value = page <= 1 ? list : [...scopeItems.value, ...list]
    scopeTotal.value = Number(data?.total) || scopeItems.value.length
    scopePage.value = Number(data?.page) || page
  } catch (e) {
    // ⚠️ 失败**不能静默**：列表空着时两个箭头全禁用，看起来就是「坏了」。
    //    标记失败 → 翻页退回全库列表，并如实说明。
    scopeFailed.value = true
    scopeItems.value = []
    ElMessage.warning('这一批照片没取到，左右翻页已退回全库列表')
  } finally {
    scopeLoading.value = false
  }
}

/** 换了一个人 / 换了一个地点 → 重新拉第一批（`key` 是字符串，避免 watch 对象引用） */
const scopeKey = computed(() => String(route.query.scope || ''))

watch(
  scopeKey,
  async (key) => {
    scopeItems.value = []
    scopeTotal.value = 0
    scopePage.value = 0
    scopeFailed.value = false
    if (!key) return
    await loadScopePage(1)
    // 从深链直接进来时可能已经在第 50 张：第一批到手就顺势补下一页
    ensureScopeWindow()
  },
  { immediate: true },
)

/**
 * 翻到接近末尾时补下一页。
 * ⚠️ 只在**已经进入这一批**（列表非空）时补：否则一个不在列表里的 photoCode
 *    会触发一次没人要的请求。
 */
async function ensureScopeWindow() {
  if (!scopeSpec.value || scopeFailed.value || scopeLoading.value) return
  if (!scopeItems.value.length) return
  if (scopeItems.value.length >= scopeTotal.value) return
  if (scopeItems.value.length - 1 - currentIndex.value > SCOPE_PREFETCH_MARGIN) return
  await loadScopePage(scopePage.value + 1)
}

/**
 * 当前照片在**已加载列表**里的下标；-1 = 不在列表里。
 *
 * ⚠️ 用 `route.params.photoCode` 而不是 `photo.value?.photoCode`：
 *   路由参数在 push 的那一刻就同步变了，而 `photos.current` 要等 fetchPhoto
 *   回来才变。用后者的话，连按 → 时第二次算出来的 target 还是**同一张**
 *   （上一页还没回来），按了没反应且不报错。
 */
const currentIndex = computed(() =>
  pagerItems.value.findIndex((one) => one.photoCode === photoCode.value),
)

const canPrev = computed(() => currentIndex.value > 0)

/**
 * 有下一张吗。
 * ⚠️ 作用域模式下列表是**按需分页**的：下标到了末尾不代表真到头，可能只是
 *    下一页还没取 —— 此时仍算「有下一张」，由 gotoOffset() 去补一页。
 */
const canNext = computed(() => {
  if (currentIndex.value < 0) return false
  if (currentIndex.value < pagerItems.value.length - 1) return true
  return Boolean(scopeSpec.value)
    && !scopeFailed.value
    && scopeItems.value.length < scopeTotal.value
})

/**
 * 分母是**已加载条数**而不是后端 `total`（DR-31 纪律③）。
 * 只加载 60 张而 total=2137 时显示「12 / 2137」，用户会以为后面还能翻 2125 张，
 * 结果翻两下就到头 —— 那不叫「到头了」，那叫「被骗了」。
 */
const pagerTotal = computed(() => pagerItems.value.length)

/** 补充提示：不在列表里 / 只加载了一部分（到上限时说实话，不写「可继续加载」） */
const pagerHint = computed(() => {
  if (currentIndex.value < 0) {
    if (scopeSpec.value) {
      return scopeLoading.value
        ? '正在读取这一批照片…'
        : '这一批照片里没有这张（可能已被筛掉），可用上面的返回键离开'
    }
    return '这张照片不在当前已加载的列表里，无法连续翻页（可回照片流或待确认队列重新进入）'
  }
  const loaded = pagerItems.value.length
  // 作用域模式：这一批有它自己的 total，滚动不参与，往下翻会自动续下一页
  if (scopeSpec.value && !scopeFailed.value) {
    if (loaded >= scopeTotal.value) return ''
    return `已加载 ${loaded} / ${scopeTotal.value}，往下翻会自动继续加载`
  }
  if (loaded >= photos.total) return ''
  return photos.appendCapped
    ? `已加载 ${loaded} / ${photos.total}，继续加载已达上限，请回照片流改用年份筛选或分页器`
    : `已加载 ${loaded} / ${photos.total}，滚动可继续加载`
})

/** 正在翻页：路由 push 期间挡住第二次按键，否则一次按键能连跳两格 */
const navigating = ref(false)

/**
 * 本页「有写操作在飞」。
 *
 * ⚠️ 为什么改判进行中必须禁翻（DR-31 × DR-16，本步最容易漏的一条）
 *   改判会改掉这张脸的 personCode / isStranger 与 pb_photo_person
 *   ⇒ 人脸框描边（实线/虚线/点线三态）与侧栏「出现的人」列表**都必须重画**。
 *   改判请求还没回来就按 → 跳走，回来时看到的是**陈旧状态，而且不报错** ——
 *   症状是「我明明改判了，人脸框还是绿的」。
 *   ⇒ 改判 / 确认归属 / 标记重复 / 软删除 / 翻页期间一律 busy：
 *     两个箭头禁用 + 键盘不响应；改判成功后**先 reloadPhoto() 刷当前图，再解禁**
 *     （submitFix 里已经是这个顺序，不要把它挪到 reloadPhoto 之前）。
 */
const busy = computed(
  () =>
    navigating.value ||
    fixLoading.value ||
    confirmLoading.value ||
    dupSaving.value ||
    delLoading.value,
)

/** 延后到浏览器空闲时再发预取；没有 requestIdleCallback 就退化成 setTimeout */
function idle(fn) {
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(fn, { timeout: 1500 })
    return
  }
  window.setTimeout(fn, 200)
}

/**
 * 预取**下一张**的首块（Range），不整张拉 —— /api/original 已支持 Range（步骤 4）。
 * ⚠️ 只预取下一张：用户多为往下看；每按一次 → 最多一个请求，不会堆成一片。
 * ⚠️ 失败**静默忽略**：用户并没有要求看下一张，为一个他没要的结果弹错误提示是噪声。
 * ⚠️ 用裸 fetch 而不是 axios，与本文件既有的 probeRange() 一致（图片类请求不走 axios）。
 */
function prefetchNext() {
  const next = pagerItems.value[currentIndex.value + 1]
  if (!next) return
  idle(() => {
    fetch(originalUrl(next.photoCode), { headers: { Range: 'bytes=0-65535' } }).catch(() => {})
  })
}

/**
 * 返回目标：入口带了 `?from=` 就按来源回，没有就回照片流。
 * 主按钮走**浏览器回退语义**（能 back 就 back，没历史才 replace 兜底），
 * 完整约定见 `utils/photoReturn.js` 顶部。
 */
const backTarget = computed(() => photoReturnTarget(route))

function goBack() {
  goBackOr(router, backTarget.value.path)
}

async function gotoOffset(delta) {
  if (busy.value) return
  navigating.value = true
  try {
    let target = pagerItems.value[currentIndex.value + delta]
    // 作用域列表按需分页：往后翻到已加载的末尾时，先把下一页取回来再判断
    if (!target && delta === 1 && scopeSpec.value && !scopeFailed.value) {
      await loadScopePage(scopePage.value + 1)
      target = pagerItems.value[currentIndex.value + delta]
    }
    if (!target) return
    // 路由参数用 photoCode（/photos/:photoCode）：URL 可分享、可刷新
    // ⚠️ replace 而不是 push：翻 N 张会在历史里叠 N 条，
    //    页面上的「返回」和浏览器后退键都得按 N 次才回得去来源页。
    // ⚠️ query 必须一起带过去：from / fromName / scope 是「返回」与「翻哪一批」
    //    的全部依据，丢了下一次刷新就只能退回照片流。
    await router.replace({
      name: 'photoDetail',
      params: { photoCode: target.photoCode },
      query: route.query,
    })
    prefetchNext()
  } finally {
    navigating.value = false
  }
}

/**
 * ⚠️ 下面两条判据**照抄 `ReviewView.vue`**（同一套纪律在两处各写一遍，
 *    别的地方要复用就照它抄，不要另写一套 —— 判据不一致会出现「在输入框里按 ← 翻页了」）。
 */
function isTypingTarget(target) {
  if (!target) return false
  const tag = String(target.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable
}

/** 任何 EP 浮层打开时都不响应（放大查看 / 改判 / 确认归属打开时按 ← 不该翻页） */
function overlayOpen() {
  return Boolean(document.querySelector('.el-overlay:not([style*="display: none"])'))
}

function onKeydown(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return
  if (isTypingTarget(event.target)) return
  if (overlayOpen()) return
  // ⚠️ preventDefault 必须调：不调 ← / → 会触发横向滚动（验收第 10 条）
  // ⚠️ 只占用这两个键：Esc 关闭、F 全屏留给将来；↑↓ 不占用（会与缩放/滚动打架）
  if (event.key === 'ArrowLeft' && canPrev.value) {
    event.preventDefault()
    gotoOffset(-1)
    return
  }
  if (event.key === 'ArrowRight' && canNext.value) {
    event.preventDefault()
    gotoOffset(1)
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  review.ensurePersonDirectory()
  reloadPhoto()
  probeRange()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})

watch(photoCode, (code) => {
  if (!code) return
  reloadPhoto()
  probeRange()
  ensureScopeWindow()
  zoomVisible.value = false
  zoomPercent.value = 100
})
</script>

<template>
  <div class="pb-page space-y-4">
    <div
      v-if="photos.currentLoading && !photo"
      class="pb-card p-8 text-center text-body text-ink-weak"
    >
      正在加载照片…
    </div>

    <p
      v-else-if="!photo"
      class="pb-card px-4 py-12 text-center text-body text-ink-weak"
    >
      没有找到这张照片（可能已被软删除，或链接里的编码不对）。
    </p>

    <template v-else>
      <!-- 页头 -->
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="min-w-0">
          <!-- 两个返回，各管一件事：
               ① 主按钮 = 回退到**来的那一页**（人物详情 / 地点详情 / 待确认…），
                  和浏览器后退键同语义；没有站内上一页（刷新/分享链接）才回照片流。
               ② 次级 = 固定入口，不关心来源、就是要看全库。 -->
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-btn text-caption text-ink-sub transition-colors duration-150 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
              @click="goBack"
            >
              <ArrowLeft class="h-3.5 w-3.5" aria-hidden="true" />{{ backTarget.label }}
            </button>
            <span class="text-caption text-ink-weak" aria-hidden="true">|</span>
            <RouterLink
              to="/photos"
              class="text-caption text-ink-sub transition-colors duration-150 hover:text-ink"
              >照片流</RouterLink
            >
          </div>
          <h2 class="mt-1 flex flex-wrap items-baseline gap-x-2 text-title text-ink">
            <span>{{ formatDate(photo.takenAt) }}</span>
            <span class="font-mono text-body text-ink-sub">{{ fileLabel }}</span>
            <span
              v-if="Number(photo.isMissing) === 1"
              class="rounded-btn bg-danger-soft px-2 py-0.5 text-caption text-danger-ink"
              >原图不在磁盘上</span
            >
          </h2>
        </div>
        <div class="flex items-center gap-2">
          <el-button @click="zoomVisible = true">
            <Maximize2 class="mr-1 h-4 w-4" aria-hidden="true" />放大查看
          </el-button>
          <!-- 下载也只是「读」原图：拿一份副本，不动磁盘上那份 -->
          <a
            :href="originalUrl(photo.photoCode)"
            :download="fileLabel"
            class="inline-flex h-8 items-center rounded-btn border border-line bg-card px-3 text-body text-ink transition-colors duration-150 hover:bg-surface"
          >
            <Download class="mr-1 h-4 w-4" aria-hidden="true" />下载原图
          </a>
        </div>
      </div>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <!-- ============ 主图区 ============ -->
        <section class="pb-card p-4" aria-label="照片">
          <!-- ⚠️ 这里**不能**套 .pb-photo-frame（它 overflow:hidden）：
               人脸框的标签与「不是他」按钮要溢出框外，否则会被裁掉 -->
          <!-- group/photo：PhotoPager 的箭头靠它 hover 显形（⚠️ 用具名 group，
               不能用 group —— FaceBox 已经占了 group/face，具名 group 互不干扰） -->
          <div class="group/photo relative mx-auto" :style="frameStyle">
            <img
              :src="originalUrl(photo.photoCode)"
              :alt="`照片 ${fileLabel} 的原图`"
              class="block h-full w-full rounded-thumb border border-line object-cover"
              draggable="false"
            />
            <FaceBox
              v-for="face in faces"
              :key="face.faceCode"
              :face="face"
              :display-name="nameOf(face)"
              @fix="openFix($event, 'assign')"
            />
            <!-- 左右翻页：DR-31。不在列表里时两个箭头自动禁用并给说明 -->
            <PhotoPager
              :index="currentIndex + 1"
              :total="pagerTotal"
              :can-prev="canPrev"
              :can-next="canNext"
              :disabled="busy"
              :loaded-hint="pagerHint"
              @nav="gotoOffset"
            />
          </div>

          <div class="mt-3 flex flex-wrap items-center gap-3">
            <p class="pb-hint">
              <span class="mr-2 inline-flex items-center gap-1">
                <span
                  class="inline-block h-0 w-4 border-t-2 border-solid border-success align-middle"
                  aria-hidden="true"
                />
                实线 = 人工确认
              </span>
              <span class="mr-2 inline-flex items-center gap-1">
                <span
                  class="inline-block h-0 w-4 border-t-2 border-dashed border-success align-middle"
                  aria-hidden="true"
                />
                虚线 = 机器自动认的（可否决）
              </span>
              <span class="inline-flex items-center gap-1">
                <span
                  class="inline-block h-0 w-4 border-t-2 border-dotted border-[var(--pb-warning-line)] align-middle"
                  aria-hidden="true"
                />
                点线 = 待确认
              </span>
            </p>
            <el-button size="small" @click="zoomVisible = true">
              <Maximize2 class="mr-1 h-3.5 w-3.5" aria-hidden="true" />放大
            </el-button>
          </div>
          <p class="pb-hint mt-1">
            悬停人脸框即出「✗ 不是他」，<b>一次点击</b>就能改判。照片按真实色彩呈现，
            界面不叠加任何滤镜。
          </p>

          <!-- 四态计数：图例之外再给一次数字，色弱/灰度下也能核对 -->
          <dl class="mt-3 flex flex-wrap gap-4 border-t border-line pt-3 text-caption">
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">✓ 已确认</dt>
              <dd class="tabular-nums text-success-ink">{{ photo.stateCounts?.confirmed ?? 0 }}</dd>
            </div>
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">◐ 机器认的</dt>
              <dd class="tabular-nums text-success-ink">{{ photo.stateCounts?.disputed ?? 0 }}</dd>
            </div>
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">⚠ 待确认</dt>
              <dd class="tabular-nums text-warning-ink">{{ photo.stateCounts?.pending ?? 0 }}</dd>
            </div>
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">⊘ 陌生人</dt>
              <dd class="tabular-nums text-ink-weak">{{ photo.stateCounts?.stranger ?? 0 }}</dd>
            </div>
          </dl>
        </section>

        <!-- ============ 侧栏 ============ -->
        <aside class="space-y-4">
          <section class="pb-card p-4" aria-labelledby="pd-exif-title">
            <h3 id="pd-exif-title" class="pb-section-title">拍摄信息</h3>
            <dl class="mt-3 space-y-2 text-body">
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">时间</dt>
                <dd class="text-right tabular-nums text-ink">{{ formatDateTime(photo.takenAt) }}</dd>
              </div>
              <!-- R8 / DR-42：年代 + 人工修正入口。
                   老相册翻拍件的 shotYear 是"翻拍那一刻"，所以这里必须能改。 -->
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">年代</dt>
                <dd class="text-right text-ink">
                  <span class="tabular-nums">{{ photo.shotYear ?? EMPTY }}</span>
                  <span
                    v-if="shotYearOverridden"
                    class="ml-1 text-caption text-brand-ink"
                    :title="`扫描时读到的年份：${photo.shotYearExif ?? '未读到'}（已人工修正）`"
                    >人工修正</span
                  >
                  <el-button size="small" text @click="shotYearVisible = true">
                    <Pencil class="h-3.5 w-3.5" aria-hidden="true" />修正
                  </el-button>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">相机</dt>
                <dd class="truncate text-right text-ink">{{ photo.cameraModel || EMPTY }}</dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">尺寸</dt>
                <dd class="text-right tabular-nums text-ink">
                  {{ displaySize(photo).width || EMPTY }} × {{ displaySize(photo).height || EMPTY }}
                  <span class="text-ink-weak">（{{ formatFileSize(photo.fileSize) }}）</span>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">地点</dt>
                <!-- R5：显示**中文**（nameZh ?? placeName）；英文原值进 tooltip -->
                <dd class="text-right text-ink" :title="placeTooltip">
                  {{ placeText }}
                  <span v-if="photo.gps?.lat" class="block text-caption tabular-nums text-ink-weak">
                    {{ photo.gps.lat }}, {{ photo.gps.lon }}
                  </span>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">路径</dt>
                <dd
                  class="truncate text-right font-mono text-caption text-ink-sub"
                  :title="photo.relPath"
                >
                  {{ photo.relPath }}
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">文件 hash</dt>
                <dd
                  class="truncate text-right font-mono text-caption text-ink-sub"
                  :title="photo.fileHash"
                >
                  {{ shortHash(photo.fileHash, 10, 8) }}
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">原图分段</dt>
                <dd class="text-right text-caption text-ink-sub">
                  <template v-if="rangeProbe.status === '206'">
                    <span class="text-success-ink">Range 生效</span>
                    <span class="tabular-nums"> · {{ rangeProbe.contentRange }}</span>
                  </template>
                  <template v-else-if="rangeProbe.status">状态 {{ rangeProbe.status }}</template>
                  <template v-else>探测中…</template>
                </dd>
              </div>
            </dl>
          </section>

          <!-- 出现的人 -->
          <section class="pb-card p-4" aria-labelledby="pd-persons-title">
            <h3 id="pd-persons-title" class="pb-section-title">出现的人</h3>

            <ul v-if="(photo.persons || []).length" class="mt-3 space-y-2">
              <li
                v-for="person in photo.persons"
                :key="person.personCode"
                class="flex items-center gap-2"
              >
                <img
                  v-if="person.thumbUrl"
                  :src="person.thumbUrl"
                  class="h-8 w-8 shrink-0 rounded-full border border-line object-cover"
                  alt=""
                />
                <span
                  v-else
                  class="h-8 w-8 shrink-0 rounded-full border border-line bg-surface"
                  aria-hidden="true"
                />
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-body text-ink">{{ person.displayName }}</span>
                  <span class="block text-caption text-ink-weak">
                    <span v-if="person.confirmedFaceCount" class="text-success-ink"
                      >✓ 已确认 {{ person.confirmedFaceCount }}</span
                    >
                    <span v-if="person.autoFaceCount" class="ml-1 text-success-ink"
                      >◐ 机器认的 {{ person.autoFaceCount }}</span
                    >
                    <span v-if="!person.confirmedFaceCount && !person.autoFaceCount"
                      >这张照片里没检测到脸</span
                    >
                  </span>
                </span>
                <RouterLink
                  :to="`/people/${person.personCode}`"
                  class="shrink-0 text-caption text-brand-ink hover:underline"
                  >详情</RouterLink
                >
                <el-button
                  size="small"
                  text
                  type="danger"
                  :aria-label="`改判 ${person.displayName} 在这张照片里的脸`"
                  @click="openFixForPerson(person)"
                >
                  <X class="h-3.5 w-3.5" aria-hidden="true" />改判
                </el-button>
              </li>
            </ul>
            <p v-else class="pb-hint mt-2">这张照片里还没有已归属的人。</p>

            <!-- 未归属的脸：单独列出并给「确认归属」入口 -->
            <div v-if="pendingFaces.length" class="mt-3 border-t border-line pt-3">
              <p class="pb-hint">还有 {{ pendingFaces.length }} 张未归属的人脸</p>
              <ul class="mt-2 space-y-1.5">
                <li
                  v-for="face in pendingFaces"
                  :key="face.faceCode"
                  class="flex items-center gap-2 rounded-btn bg-warning-soft px-2 py-1.5"
                >
                  <img
                    v-if="face.thumbUrl"
                    :src="face.thumbUrl"
                    class="h-6 w-6 shrink-0 rounded-full border border-line object-cover"
                    alt=""
                  />
                  <span class="min-w-0 flex-1 truncate text-caption text-warning-ink">
                    <span aria-hidden="true">⚠</span> 待确认
                    <span v-if="face.shotBucket" class="ml-1 tabular-nums"
                      >（{{ formatBucketKey(face.shotBucket) }}）</span
                    >
                  </span>
                  <el-button size="small" :loading="confirmLoading" @click="openConfirm(face)">
                    确认归属
                  </el-button>
                  <el-button size="small" text @click="openFix(face, 'assign')">改判</el-button>
                </li>
              </ul>
            </div>
          </section>

          <!-- 操作 -->
          <section class="pb-card p-4" aria-labelledby="pd-actions-title">
            <h3 id="pd-actions-title" class="pb-section-title">操作</h3>
            <div class="mt-3 flex flex-wrap gap-2">
              <el-button
                v-if="Number(photo.duplicates?.isDuplicate) === 1"
                :loading="dupSaving"
                @click="doUnmarkDup"
              >
                <Undo2 class="mr-1 h-4 w-4" aria-hidden="true" />取消重复标记
              </el-button>
              <el-button v-else @click="openDup">
                <Copy class="mr-1 h-4 w-4" aria-hidden="true" />标记重复
              </el-button>
              <el-button type="danger" plain :loading="delLoading" @click="openDelete">
                <Trash2 class="mr-1 h-4 w-4" aria-hidden="true" />软删除
              </el-button>
            </div>
            <p v-if="photo.duplicates?.dupOfPhotoCode" class="pb-hint mt-2">
              已标记为
              <!-- 同页跳转：replace + 沿用当前来源，返回时仍回「来的那一页」 -->
              <RouterLink
                :to="photoDetailLink(photo.duplicates.dupOfPhotoCode, inheritSource(route))"
                replace
                class="text-brand-ink hover:underline"
                >另一张照片</RouterLink
              >
              的副本。
            </p>
            <p class="pb-hint mt-3">
              本工具不提供编辑或覆盖原图的入口：所有操作只改数据库与缓存，磁盘上的原图始终只读。
              软删除也只标记库里的记录，重新扫描即可恢复。
            </p>
          </section>
        </aside>
      </div>
    </template>

    <!-- ============ 改判浮层（三种 action 共用）============ -->
    <FixFaceDialog
      v-model="fixVisible"
      :faces="fixFaces"
      :siblings="fixSiblings"
      :candidates="fixCandidates"
      :search-fn="review.searchPersons"
      :mode="fixMode"
      :loading="fixLoading"
      :photo-label="fixPhotoLabel"
      :threshold-low="settings.matchThresholdLow"
      :threshold-high="settings.matchThresholdHigh"
      @submit="submitFix"
      @create-person="createPersonAndAssign"
    />

    <!-- ============ 年代修正（DR-42）============ -->
    <ShotYearFixDialog
      v-model="shotYearVisible"
      :photo="photo"
      @done="onShotYearFixed"
    />

    <!-- ============ 确认归属（未归属的脸）============ -->
    <el-dialog v-model="confirmVisible" title="确认这张脸属于谁" width="480px">
      <!-- 与「改判人脸」里的搜索同一形态（独立区块 + 大号输入框）：
           确认归属靠的也是**搜全库**，候选那前几条常常不是要找的人 -->
      <div class="rounded-btn border border-line bg-surface p-3">
        <div class="flex items-baseline justify-between gap-2">
          <label class="text-body font-medium text-ink" for="pd-confirm-search">搜索人物</label>
          <span class="pb-hint">搜全库，不受相似度前 5 名限制</span>
        </div>
        <el-input
          id="pd-confirm-search"
          v-model="confirmKeyword"
          class="mt-2"
          size="large"
          placeholder="例：王小明 / wangxiaoming / wxm"
          clearable
          aria-label="搜索人物"
        >
          <template #prefix>
            <Search class="h-4 w-4 text-ink-weak" aria-hidden="true" />
          </template>
        </el-input>
      </div>
      <!-- 空态两种：候选池本来就是空 / 搜了没搜到。合成一句话会出现
           「库里还没有任何人物档案可对照」这种完全误导的话。 -->
      <p
        v-if="!confirmList.length && String(confirmKeyword || '').trim() && confirmSearched"
        class="mt-3 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        全库里没有匹配「<b class="break-all">{{ String(confirmKeyword).trim() }}</b>」的人物。
        检查拼写，或只输姓氏试试；确认这个人还没建档的话，用「改判 → 新建人物」建一个并归属。
      </p>
      <p
        v-else-if="!confirmCandidates.length"
        class="mt-3 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        库里还没有任何人物档案可对照。先用「改判 → 新建人物」建一个并归属，
        之后同类照片就能自动比对了。
      </p>
      <ul
        v-else-if="confirmList.length"
        class="mt-3 max-h-72 divide-y divide-line overflow-y-auto rounded-btn border border-line"
      >
        <li v-for="candidate in confirmList" :key="candidate.personCode">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface"
            @click="doConfirm(candidate)"
          >
            <img
              v-if="candidate.avatarFaceCode"
              :src="faceUrl(candidate.avatarFaceCode)"
              class="h-7 w-7 shrink-0 rounded-full border border-line object-cover"
              alt=""
            />
            <span class="min-w-0 flex-1 truncate text-body text-ink">{{ candidate.displayName }}</span>
            <span class="shrink-0 text-caption tabular-nums text-ink-sub">{{
              similarityText(candidate.similarity)
            }}</span>
            <span class="shrink-0 text-caption text-brand-ink">确认</span>
          </button>
        </li>
      </ul>
      <p class="pb-hint mt-2">
        确认后会立即重算这个人的年代档质心，并<b>自动前进到队列的下一条</b>。
      </p>
    </el-dialog>

    <!-- ============ 标记重复（选一张作为主照片）============ -->
    <el-dialog v-model="dupVisible" :title="`把「${fileLabel}」标成谁的副本？`" width="560px">
      <el-input
        v-model="dupKeyword"
        placeholder="按路径 / 机型 / 地点搜主照片"
        clearable
        size="small"
        aria-label="搜索主照片"
        @keyup.enter="searchDupCandidates"
      >
        <template #prefix>
          <Search class="h-3.5 w-3.5 text-ink-weak" aria-hidden="true" />
        </template>
      </el-input>
      <ul class="mt-3 max-h-72 divide-y divide-line overflow-y-auto rounded-btn border border-line">
        <li v-for="candidate in dupCandidates" :key="candidate.photoCode">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface"
            :disabled="dupSaving"
            @click="saveDup(candidate)"
          >
            <img
              :src="thumbUrl(candidate.photoCode, 200)"
              class="h-10 w-10 shrink-0 rounded-thumb border border-line object-cover"
              alt=""
              loading="lazy"
            />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-body text-ink">{{ baseName(candidate.relPath) }}</span>
              <span class="block truncate text-caption text-ink-weak">
                {{ formatDate(candidate.takenAt) }} · {{ placeDisplayName(candidate) }}
              </span>
            </span>
            <span class="shrink-0 text-caption text-brand-ink">选它</span>
          </button>
        </li>
      </ul>
      <p v-if="!dupLoading && !dupCandidates.length" class="pb-hint mt-2">没有搜到候选照片。</p>
      <p class="pb-hint mt-2">
        重复标记只是打一个指针（<code>dupOfPhotoCode</code>），
        <b>不会删除任何文件</b>，随时可以取消。
      </p>
      <template #footer>
        <el-button @click="dupVisible = false">取消</el-button>
        <el-button :loading="dupLoading" @click="searchDupCandidates">重新搜索</el-button>
      </template>
    </el-dialog>

    <!-- ============ 软删除：两段式（先看影响面，再确认）============ -->
    <el-dialog
      v-model="delVisible"
      :title="`确认软删除「${fileLabel}」？`"
      width="480px"
      :close-on-click-modal="false"
    >
      <p class="text-body text-ink-sub">软删除后会发生：</p>
      <ul class="mt-2 list-disc space-y-1 pl-5 text-body text-ink-sub">
        <li>这张照片不再出现在照片流与人物时间轴里，库中的记录被标记为已删除</li>
        <li>
          <b>{{ delImpact?.faceCount ?? 0 }}</b> 张人脸一并从队列移出（其中待确认
          <b>{{ delImpact?.pendingFaces ?? 0 }}</b> 张、机器认的
          <b>{{ delImpact?.disputedFaces ?? 0 }}</b> 张、已确认
          <b>{{ delImpact?.confirmedFaces ?? 0 }}</b> 张）
        </li>
        <li v-if="delImpact?.personCodes?.length">
          出现过的 <b>{{ delImpact.personCodes.length }}</b> 个人物的照片数会各减 1
        </li>
        <li>
          磁盘上的原图<b>不会被删除或改动</b> —— 重新扫描一次就能完整恢复（人脸也一起回来）
        </li>
      </ul>
      <template #footer>
        <el-button :disabled="delLoading" @click="delVisible = false">取消</el-button>
        <el-button type="danger" :loading="delLoading" @click="doDelete">
          确认软删除（不动原图）
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 放大查看：CSS 缩放 + 原生滚动 ============ -->
    <el-dialog v-model="zoomVisible" title="放大查看原图" width="88%" top="4vh">
      <div class="mb-2 flex items-center gap-2">
        <span class="pb-hint">缩放</span>
        <el-radio-group v-model="zoomPercent" size="small" aria-label="缩放比例">
          <el-radio-button :value="50">50%</el-radio-button>
          <el-radio-button :value="100">适应</el-radio-button>
          <el-radio-button :value="200">200%</el-radio-button>
          <el-radio-button :value="400">400%</el-radio-button>
        </el-radio-group>
        <span class="pb-hint ml-2">
          拖动滚动查看局部（放大后不再发请求，用的是已解码的这张图）
        </span>
      </div>
      <div class="max-h-[76vh] overflow-auto rounded-thumb border border-line bg-photo">
        <img
          :src="originalUrl(photo?.photoCode || '')"
          :alt="`照片 ${fileLabel} 的原图（放大）`"
          class="block"
          :style="zoomStyle"
          draggable="false"
        />
      </div>
    </el-dialog>
  </div>
</template>
