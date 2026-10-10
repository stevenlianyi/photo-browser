<!--
  P-06 待确认队列（设计稿 §4.6）★ 核心人工环节
  ------------------------------------------------------------
  双 Tab（DR-16）：
    Tab1「待确认 N」  = personCode IS NULL AND isStranger=0        没归属的脸
    Tab2「我不同意 M」= personCode IS NOT NULL AND isConfirmed=0   机器自动认的
  两者处理的问题不同，角标分两个数字显示，**不合并**。

  Tab1：**并排比对**（未知人脸 vs 候选人物头像），候选按相似度降序，
  落在灰区（T_low–T_high）的条目标橙色提示。确认后**自动前进到下一条**。
  Tab2：按照片分组，整张一键否决 ≤2 次点击（详见 DisputedList 的注释）。

  键盘快捷键（设计稿 §4.6）：1/2/3 选候选、N 新建、S 跳过、I 忽略。
  ⚠️ 快捷键只在**待确认 Tab** 生效，且输入框聚焦时必须让位 ——
     否则在输入框里打「1」会顺手确认掉一张脸。
  ⚠️「I 忽略」是**不可逆语义**（标记陌生人 = 永久排除），所以它走
     一道确认弹窗，不能一个按键直接生效。

  「合并 / 拆分」入口在手边（不藏三级菜单）
  ----------------------------------------
  设计稿明确要求。取舍：**合并**放在待确认 Tab 顶部的「与某人合并」
  （那里同时握着「当前被认成谁」和「候选是谁」，是发起合并信息量最大的位置）；
  **拆分**放在「我不同意」列表每条脸旁边（「这张脸不该属于他 → 拆出来」
  是最自然的语境）。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowRightLeft,
  Keyboard,
  Layers,
  Merge,
  TriangleAlert,
  UserPlus,
  X,
} from 'lucide-vue-next'
import BucketTimeline from '@/components/photo/BucketTimeline.vue'
import CandidateRow from '@/components/review/CandidateRow.vue'
import ConfirmMerge from '@/components/review/ConfirmMerge.vue'
import DisputedList from '@/components/review/DisputedList.vue'
import FixFaceDialog from '@/components/common/FixFaceDialog.vue'
import { useReviewStore } from '@/store/review'
import { useSettingsStore } from '@/store/settings'
import { batchFix, mergePersons } from '@/api/review'
import { faceUrl, thumbUrl } from '@/api/static'
import { confidenceOf, faceBoxStyle, faceStateOf } from '@/utils/faceState'
import { EMPTY, baseName, formatBucketKey, formatCount } from '@/utils/format'
// 进照片详情要**声明来源**：详情页的「返回」才知道该回哪（见 utils/photoReturn.js）
import { photoDetailLink } from '@/utils/photoReturn'

const review = useReviewStore()
const settings = useSettingsStore()

const T_LOW = computed(() => settings.matchThresholdLow)
const T_HIGH = computed(() => settings.matchThresholdHigh)

const activeTab = computed({
  get: () => review.activeTab,
  set: (value) => review.setTab(value),
})

const current = computed(() => review.current)
const candidates = computed(() => review.currentCandidates)

/** 左侧大图：待确认条目只给 photoCode，文件名与人脸框要另取一次照片详情 */
const currentPhoto = ref(null)
watch(
  () => current.value?.photoCode,
  async (photoCode) => {
    if (!photoCode) {
      currentPhoto.value = null
      return
    }
    await review.ensurePhoto(photoCode)
    currentPhoto.value = review.photoCache[photoCode] || null
  },
  { immediate: true },
)

/** 当前这张脸在照片里的那一行（拿 bbox 才能在人脸框里画出来） */
const currentFaceRow = computed(() => {
  if (!current.value || !currentPhoto.value) return null
  return (
    (currentPhoto.value.faces || []).find((f) => f.faceCode === current.value.faceCode) || null
  )
})

const currentMeta = computed(() => faceStateOf(currentFaceRow.value || current.value))

/**
 * 当前这张脸「最高相似度」的档位（高可靠 / 待判断 / 大概不是 / 基本不是）。
 * 光给数字不行：0.22 和 0.05 都是两位小数，含义却差着一档 ——
 * 用户要的是「敢不敢直接按确认处理」，那就把机器的把握程度说出来。
 */
const currentConfidence = computed(() =>
  confidenceOf(current.value?.similarity, { low: T_LOW.value, high: T_HIGH.value }),
)
const photoLabel = computed(
  () => baseName(currentPhoto.value?.relPath) || current.value?.photoCode || '',
)

function faceBoxOf(face) {
  return faceBoxStyle(face?.bbox) || {}
}

/**
 * DR-43：来源照片的人工旋转角度。**大图与人脸框必须一起转**。
 *
 * ⚠️ 为什么这里不能「只转 <img>」（与详情页同一个坑）
 * ------------------------------------------------
 *   人脸框是 `pb_face.bbox` 归一化 x,y,w,h 转成的百分比、**绝对定位**在图上，
 *   百分比基座是它的 offsetParent。只转 <img>、框留在外面 ⇒ 框全部错位。
 *   所以这里同样用「外层吃旋转后比例 + 内层包住 img 与框一起转」的结构
 *   （见模板里那段注释），bbox 语义一个字都不用改。
 *
 * 角度取自 `currentPhoto`（`GET /api/photos/{code}`，含 rotateDeg）；
 * 兜底取队列条目自带的 rotateDeg（`_attachRotateDeg` 补的），两者都不在时 0。
 */
const rotateDeg = computed(
  () => ((Number(currentPhoto.value?.rotateDeg ?? current.value?.rotateDeg) || 0) % 360 + 360) % 360,
)

/** 外层比例：90/270 时 4:3 -> 3:4（否则 4:3 的框装 3:4 的内容会两边露底色） */
const frameClass = computed(() =>
  rotateDeg.value === 90 || rotateDeg.value === 270 ? 'aspect-[3/4]' : 'aspect-[4/3]',
)

/**
 * 内层：宽高用**未旋转**的 4:3（这是人脸框百分比的基座，也是缩略图裁切的形状），
 * 居中 + 整体 rotate；90/270 时宽度取 `100% * 4/3` 才能转过来正好覆盖外层。
 * 外层 `.pb-photo-frame` 自带 `overflow-hidden` → 转完四周**没有空隙**。
 */
const rotateInnerStyle = computed(() => {
  const deg = rotateDeg.value
  const swapped = deg === 90 || deg === 270
  return {
    aspectRatio: '4 / 3',
    width: swapped ? 'calc(100% * 4 / 3)' : '100%',
    transform: `translate(-50%, -50%) rotate(${deg}deg)`,
  }
})

/** 进度：「剩余 N · 共 M 条」（跳过的是排到最后、不是完成，所以计入剩余） */
const progressText = computed(() => {
  const base = `剩余 ${formatCount(review.remaining)} 条 · 共 ${formatCount(review.pendingTotal)} 条待确认`
  return review.deferredCount
    ? `${base} · 已跳过 ${formatCount(review.deferredCount)} 条（排到最后）`
    : base
})

// ============================================================
// 确认 / 跳过 / 忽略
// ============================================================

async function onConfirm(candidate) {
  if (!candidate) return
  const data = await review.confirmCandidate(candidate)
  if (data?.person) {
    ElMessage.success(
      `已确认：${candidate.displayName} 现在 ${data.person.photoCount} 张照片 / ${data.person.faceCount} 张脸`,
    )
  }
}

function onSkip() {
  return review.skip()
}

const ignoreVisible = ref(false)
const ignoreLoading = ref(false)

function onIgnore() {
  ignoreVisible.value = true
}

async function doIgnore() {
  ignoreLoading.value = true
  try {
    const data = await review.ignoreCurrent()
    review.applyCounts(data)
    ignoreVisible.value = false
    ElMessage.success('已标记为陌生人：这张脸不再出现在任何队列里')
  } finally {
    ignoreLoading.value = false
  }
}

// ============================================================
// 改判浮层（Tab1 的「换一个人」/ Tab2 的「去改判」共用）
// ============================================================

const fixVisible = ref(false)
const fixFaces = ref([])
const fixCandidates = ref([])
const fixPhotoLabel = ref('')
const fixLoading = ref(false)
const fixMode = ref('assign')

function openFix(faces, options = {}) {
  const { mode = 'assign', candidates: picked = null, label = '' } = options
  fixFaces.value = Array.isArray(faces) ? faces : [faces]
  fixMode.value = mode
  fixCandidates.value = picked || candidates.value || []
  fixPhotoLabel.value = label || photoLabel.value
  fixVisible.value = true
}

/** Tab1：把当前这张脸放进改判浮层（候选就是队列给的那几个） */
function openFixForCurrent() {
  if (!current.value) return
  openFix(
    {
      faceCode: current.value.faceCode,
      photoCode: current.value.photoCode,
      similarity: current.value.similarity,
      bbox: currentFaceRow.value?.bbox,
      state: 'pending',
    },
    { candidates: candidates.value },
  )
}

/** Tab1 候选行上的「改判」：把这张脸换给别的人（当前那条从候选里剔掉） */
function openFixWithCandidates(candidate) {
  if (!current.value) return
  const others = candidates.value.filter((c) => c.personCode !== candidate?.personCode)
  openFix(
    {
      faceCode: current.value.faceCode,
      photoCode: current.value.photoCode,
      similarity: current.value.similarity,
      bbox: currentFaceRow.value?.bbox,
      state: 'pending',
    },
    { candidates: others },
  )
}

const fixSiblings = computed(() => {
  if (fixFaces.value.length !== 1 || !currentPhoto.value) return []
  const code = fixFaces.value[0]?.faceCode
  return (currentPhoto.value.faces || []).filter((face) => face.faceCode !== code)
})

async function submitFix({ action, faceCodes, personCode }) {
  fixLoading.value = true
  try {
    const data = await review.fix(action, faceCodes, personCode)
    review.applyCounts(data)
    fixVisible.value = false
    ElMessage.success(
      action === 'unknown'
        ? '已置为未知：这些脸回到「待确认」队列'
        : action === 'stranger'
          ? '已标记为陌生人：这些脸从所有队列与聚类中消失'
          : '改判完成：照片数与两个角标已同步更新',
    )
  } finally {
    fixLoading.value = false
  }
}

async function createPersonAndAssign({ form, faceCodes }, onDone) {
  fixLoading.value = true
  try {
    await review.createPersonAndAssign(form, faceCodes)
    fixVisible.value = false
    onDone?.()
    ElMessage.success('已新建人物并把该脸归属给他')
  } finally {
    fixLoading.value = false
  }
}

/** Tab2：从「我不同意」列表点「去改判」—— 这张脸没有现成相似度，候选用全量人物 */
async function openFixFromDisputed(face) {
  await review.ensurePersonDirectory()
  openFix([{ ...face, state: 'disputed' }], {
    candidates: review.decorateCandidates(
      Object.values(review.personDirectory).map((person) => ({
        personCode: person.personCode,
        displayName: person.displayName,
        avatarFaceCode: person.avatarFaceCode,
        // 头像带上服务端解析好的那张（DR-40）：只给 avatarFaceCode 的话，
        // 「我不同意」里改判时浮层一整列人只有少数几个有头像，其余是空圆
        coverFaceCode: person.coverFaceCode,
        similarity: null,
      })),
    ),
    label: face.photoCode,
  })
}

// ============================================================
// Tab2 的操作
// ============================================================

async function onRejectGroup(group) {
  review.busyPhotoCode = group.photoCode
  try {
    const data = await review.fixWholePhoto(group)
    review.applyCounts(data)
    ElMessage.success(`已整张否决：${group.faces.length} 张脸回到「待确认」队列`)
  } finally {
    review.busyPhotoCode = ''
  }
}

async function onRejectFace(face) {
  review.busyPhotoCode = face.photoCode
  try {
    const data = await review.fix('unknown', [face.faceCode])
    review.applyCounts(data)
    ElMessage.success('已否决：这张脸回到「待确认」队列')
  } finally {
    review.busyPhotoCode = ''
  }
}

/** 簇内批量改判：一次点击解决 N 张（服务端一个事务 + 一次重算） */
async function onBatchCluster(clusterCode) {
  const result = await batchFix({ clusterCode, action: 'unknown' })
  review.applyCounts(result)
  await review.fetchDisputed({ reset: true })
  ElMessage.success(`已把该簇的 ${result?.fixed ?? 0} 张脸批量退回「待确认」队列`)
}

/**
 * 拆分：**真调 POST /review/split**，先问「它属于谁」
 * ---------------------------------------------------
 * 早先这里是 `review.fix('unknown', [...])`，与「否决」在库效果上完全一样，
 * 只换了层文案 —— 那是把两个不同的事说成同一件事：拆分要记「谁本来属于谁」
 * 并可撤销，否决不记。
 *
 * 浮层复用 FixFaceDialog（variant="split"）：三块 UI（人名复述 / 动作单选 /
 * 人选列表）与改判完全同构，拆开写第二份必然漂移。
 */
const splitVisible = ref(false)
const splitFaces = ref([])
const splitLoading = ref(false)
/** 复述用：这张脸来自哪个文件（disputed 的 face 行只有 photoCode） */
const splitPhotoLabel = ref('')

/** 「它属于谁」的候选 = 全库人物，按姓名排。必须先 ensure 过，否则是空列表。 */
const splitPersons = computed(() =>
  Object.values(review.personDirectory)
    .filter((person) => person.delFlag !== '1')
    .sort((a, b) => String(a.displayName || '').localeCompare(String(b.displayName || ''), 'zh')),
)

async function openSplit(face) {
  // 候选来自全库目录，接口没打过就现打一份（store 内有缓存，只打一次）
  await review.ensurePersonDirectory()
  // ⚠️ 必须带 displayName：FixFaceDialog 的复述要写「当前被认成 <谁>」，
  //    而 disputed 接口的 faces[] 里只有 personCode，姓名在 personDirectory 里。
  splitFaces.value = [
    {
      ...face,
      displayName: face.displayName || review.personDirectory[face.personCode]?.displayName || '',
    },
  ]
  // 复述要写**文件名**（「来自 DSC00801.JPG」），而 disputed 的 face 行只有 photoCode。
  // ⚠️ relPath 在 ensurePhoto 的返回值上，不在 store 的某个 label 字段上 ——
  //    写成 review.photoLabel 会静默拿到 undefined，最后退回显示 photoCode。
  const photo = await review.ensurePhoto(face.photoCode)
  splitPhotoLabel.value = baseName(photo?.relPath) || face.photoCode || ''
  splitVisible.value = true
}

function closeSplit() {
  splitVisible.value = false
  splitFaces.value = []
}

/**
 * 三条出路各自对应 merger.split 的一个分支：
 *   assign  -> 拆给库里已有的那个人（personCode 传下去）
 *   stranger-> 标陌生人（走 fix，因为 split 没有 stranger 分支）
 *   unknown -> 退回待确认（personCode 传空，但**仍然可撤销**）
 * 新建人物走 FixFaceDialog 的 create-person：把 PersonForm 的 displayName /
 * birthday 交给 splitOne，服务端自动建档 —— 不用先去联系人页建人再回来。
 */
async function onSplitSubmit(payload) {
  splitLoading.value = true
  try {
    const faceCode = payload.faceCodes[0]
    if (payload.action === 'stranger') {
      const data = await review.fix('stranger', payload.faceCodes)
      review.applyCounts(data)
      ElMessage.success('已标记为陌生人：这张脸不再出现在任何队列里')
    } else {
      const data = await review.splitOne(faceCode, payload.personCode || '')
      const to = data.toPersonCode
      const name = to
        ? review.personDirectory[to]?.displayName || to
        : '「待确认」队列'
      if (!to) review.personDirectory = {}
      ElMessage.success(
        data.revertible
          ? `已从「${splitFaces.value[0]?.displayName || '原主人'}」拆出这张脸 → ${name}（可撤销）`
          : `已从「${splitFaces.value[0]?.displayName || '原主人'}」拆出这张脸 → ${name}`,
      )
    }
    closeSplit()
    if (review.activeTab === 'disputed') await review.fetchDisputed({ reset: true })
    await review.fetchBadge()
  } finally {
    splitLoading.value = false
  }
}

async function onSplitCreatePerson({ form, faceCodes }, done) {
  splitLoading.value = true
  try {
    const data = await review.splitOne(faceCodes[0], '', form)
    ElMessage.success(
      `已新建档案「${form.displayName}」并把 ${faceCodes.length} 张脸拆给他`
      + (data.revertible ? '（可撤销）' : ''),
    )
    review.personDirectory = {}
    done?.()
    closeSplit()
    if (review.activeTab === 'disputed') await review.fetchDisputed({ reset: true })
    await review.fetchBadge()
  } finally {
    splitLoading.value = false
  }
}

// ============================================================
// 合并（手边入口，不藏三级菜单）
// ============================================================

const mergeVisible = ref(false)
const mergeTarget = ref('')
const mergeLoading = ref(false)
const mergePickerVisible = ref(false)

/** 被合并方 = 队列给出的最高分候选（机器认为最像的那个人） */
const mergeFrom = computed(() => {
  const owner = (current.value?.topCandidates || [])[0]
  if (!owner) return null
  const person = review.personDirectory[owner.personCode]
  return person ? { personCode: person.personCode, ...person } : { ...owner }
})

const mergeTo = computed(() => {
  // ⚠️ 必须从 **personDirectory** 取，不能从队列的 candidates 取 ——
  //    合并目标是用户从「全部人物」里挑的（同名不同人往往不在相似度候选里），
  //    从 candidates 里找会永远返回 null，表现是「选完了但下一步按钮一直点不动」。
  const person = review.personDirectory[mergeTarget.value]
  if (!person) return null
  return { personCode: person.personCode, ...person }
})

/**
 * 合并的**目标候选 = 全库人物**，不是队列给的那几个候选。
 * ------------------------------------------------------------
 * 队列的 topCandidates 回答的是「这张脸最像谁」（合并**源**），
 * 而合并要回答的是「跟谁并成一个人」（合并**目标**）—— 两者是不同的问题。
 * 真实场景恰恰是「库里有两个同名的张三」，目标那个人**根本不在候选里**
 * （候选是按向量相似度给的，而重名的人向量几乎一样，多半排在很后面）。
 * 早先只列队列候选，结果「与某人合并」点开是一列表「被合并方」且全部禁用 ——
 * 一个走不通的入口。
 */
const mergeKeyword = ref('')

const mergeCandidates = computed(() => {
  const text = String(mergeKeyword.value || '').trim().toLowerCase()
  const all = Object.values(review.personDirectory).filter(
    (person) => person.personCode !== mergeFrom.value?.personCode && person.delFlag !== '1',
  )
  const list = text
    ? all.filter((person) => String(person.displayName || '').toLowerCase().includes(text))
    : all
  return list.slice(0, 100)
})

function openMerge() {
  mergeTarget.value = ''
  mergeKeyword.value = ''
  mergeVisible.value = false
  mergePickerVisible.value = Boolean(mergeFrom.value)
}

/** 选好目标 → 进入复述确认 */
function toMergeConfirm() {
  if (!mergeTo.value) return
  mergePickerVisible.value = false
  mergeVisible.value = true
}

async function doMerge() {
  if (!mergeTo.value || !mergeFrom.value) return
  mergeLoading.value = true
  try {
    const data = await mergePersons({
      fromPersonCode: mergeFrom.value.personCode,
      toPersonCode: mergeTo.value.personCode,
    })
    review.applyCounts(data)
    mergeVisible.value = false
    mergePickerVisible.value = false
    ElMessage.success(
      `已合并：${data?.faces ?? 0} 张脸归到「${mergeTo.value.displayName}」名下，质心已重算（可撤销）`,
    )
    await review.fetchPending({ reset: true })
  } finally {
    mergeLoading.value = false
  }
}

// ============================================================
// 批量确认（同一聚类簇下所有人脸一次全确认）
// ============================================================

const clusterDialogVisible = ref(false)
const clusterPick = ref('')
const batchLoading = ref(false)

/** BucketTimeline 要 [{bucketKey, photos}]；簇成员没有照片摘要，
 *  这里用 photoCode 拼出最小可渲染的 photo 对象（BucketTimeline 只用它取缩略图） */
const clusterGroups = computed(() => {
  const map = {}
  for (const member of review.clusterMembers) {
    const key = member.shotBucket || 'ALL'
    if (!map[key]) map[key] = { bucketKey: key, photos: [] }
    map[key].photos.push({
      photoCode: member.photoCode,
      relPath: member.photoCode,
      faceCount: 1,
      isDuplicate: 0,
      hasGps: false,
    })
  }
  return Object.values(map)
})

async function openCluster(clusterCode) {
  await review.openCluster(clusterCode)
  clusterPick.value = candidates.value[0]?.personCode || ''
  clusterDialogVisible.value = true
}

async function doBatchConfirm() {
  if (!clusterPick.value) return
  batchLoading.value = true
  try {
    const data = await review.batchConfirm(clusterPick.value)
    review.applyCounts(data)
    clusterDialogVisible.value = false
    ElMessage.success(
      `已一次性确认 ${data?.assigned ?? 0} 张脸（一次事务 + 一次质心重算 + 一条日志）`,
    )
  } finally {
    batchLoading.value = false
  }
}

// ============================================================
// 分页（Tab2）
// ============================================================

async function onDisputedPage(value) {
  review.disputedPage = value
  await review.fetchDisputed({ reset: false })
}

// ============================================================
// 键盘快捷键
// ============================================================

/** 输入类元素聚焦时必须让位快捷键 */
function isTypingTarget(target) {
  if (!target) return false
  const tag = String(target.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable
}

/** 任何 EP 浮层打开时都不响应（否则按 S 想「取消」反而会跳过一条） */
function overlayOpen() {
  return Boolean(document.querySelector('.el-overlay:not([style*="display: none"])'))
}

function onKeydown(event) {
  if (activeTab.value !== 'pending') return
  if (event.metaKey || event.ctrlKey || event.altKey) return
  if (isTypingTarget(event.target)) return
  if (overlayOpen()) return

  const key = String(event.key)
  if (key === '1' || key === '2' || key === '3') {
    const candidate = candidates.value[Number(key) - 1]
    if (!candidate) return
    event.preventDefault()
    onConfirm(candidate)
    return
  }
  const lower = key.toLowerCase()
  if (lower === 's') {
    event.preventDefault()
    onSkip()
    return
  }
  if (lower === 'i') {
    event.preventDefault()
    onIgnore()
    return
  }
  if (lower === 'n') {
    event.preventDefault()
    openFixForCurrent()
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  await review.fetchBadge()
  await Promise.all([review.fetchPending({ reset: true }), review.ensurePersonDirectory()])
  if (review.activeTab === 'disputed') review.fetchDisputed({ reset: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  review.closeCluster()
})

watch(activeTab, (tab) => {
  if (tab === 'disputed') review.fetchDisputed({ reset: true })
  else if (!review.pendingItems.length) review.fetchPending({ reset: true })
})
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">待确认</h2>
        <p class="pb-hint mt-1">
          {{ progressText }} ·
          {{ formatCount(review.disputedCount) }} 条机器自动认的待你复核
        </p>
      </div>
      <div class="flex items-center gap-2">
        <el-button
          :disabled="!current"
          title="跳过这张 = 把它排到待确认队列的最后，不会马上又问你一次"
          @click="onSkip"
        >
          跳过这张
        </el-button>
        <el-button type="danger" plain :disabled="!current" @click="onIgnore">
          <X class="mr-1 h-4 w-4" aria-hidden="true" />忽略此人脸
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="pb-card px-4 pb-4">
      <!-- ================= Tab1 待确认 ================= -->
      <el-tab-pane name="pending">
        <template #label>
          <span class="inline-flex items-center gap-1">
            待确认
            <span class="tabular-nums font-medium text-warning-ink">{{
              formatCount(review.pendingCount)
            }}</span>
          </span>
        </template>

        <div v-if="!current && !review.pendingLoading" class="py-10 text-center">
          <p class="text-body text-ink-weak">
            待确认队列已清空 —— 所有未归属的人脸都已被人工确认或标记为陌生人。
          </p>
          <template v-if="review.deferredCount">
            <p class="pb-hint mt-2">
              另有 <b class="tabular-nums">{{ formatCount(review.deferredCount) }}</b>
              条是你<b>跳过</b>的，已按「排到最后」放在队列末尾。
            </p>
            <el-button size="small" class="mt-3" @click="review.showDeferred()">
              重新显示已跳过的 {{ formatCount(review.deferredCount) }} 条
            </el-button>
          </template>
        </div>

        <div v-else class="grid grid-cols-1 gap-6 pt-2 md:grid-cols-[340px_minmax(0,1fr)]">
          <!-- 左：未知人脸（并排比对的第一半） -->
          <section aria-label="待确认的人脸">
            <p class="pb-hint">未知人脸</p>
            <!-- ⚠️ 缩略图（不是原图）：队列页一次只看一张脸，
                 拉 3~6MB 的原图没有意义（红线：网格/队列绝不加载原图） -->
            <!-- DR-43：外层吃**旋转后**的比例（90/270 时 4:3 -> 3:4），
                 内层包住 img 与那个人脸框**一起转** —— 只转 img 的话
                 人脸框的百分比基座没变，框会全部错位。
                 `.pb-photo-frame` 自带 overflow-hidden，所以转完四周无空隙。 -->
            <div v-if="current" class="pb-photo-frame relative mt-2 w-full" :class="frameClass">
              <div class="absolute left-1/2 top-1/2" :style="rotateInnerStyle">
                <img
                  :src="thumbUrl(current.photoCode, 400)"
                  :alt="`来源照片 ${photoLabel} 的缩略图`"
                  class="block h-full w-full object-cover"
                  decoding="async"
                />
                <!-- 人脸框：橙色点线 = 待确认（与详情页同一套编码） -->
                <span
                  v-if="currentFaceRow"
                  class="absolute border-2 border-dotted border-[var(--pb-warning-line)]"
                  :style="faceBoxOf(currentFaceRow)"
                  aria-hidden="true"
                />
              </div>
            </div>
            <p v-else class="pb-card mt-2 px-3 py-8 text-center text-body text-ink-weak">
              正在加载…
            </p>

            <!-- 脸裁剪图（160px 正方形 = 后台 FACE_CROP_SIZE 原生尺寸，可直接看五官；
                 再放大只会糊，所以不上到 200+） -->
            <div v-if="current" class="mt-3 flex items-center gap-3">
              <img
                :src="faceUrl(current.faceCode)"
                class="h-40 w-40 shrink-0 rounded-thumb border border-line object-cover"
                alt="待确认人脸的裁剪图"
              />
              <div class="min-w-0 text-caption text-ink-weak">
                <p>
                  <span aria-hidden="true">{{ currentMeta.icon }}</span>
                  {{ currentMeta.hint }}
                </p>
                <p v-if="current.shotBucket" class="mt-1 tabular-nums">
                  年代档 {{ formatBucketKey(current.shotBucket) }}
                </p>
                <p v-if="current.detScore" class="mt-1 tabular-nums">
                  检测置信度 {{ Number(current.detScore).toFixed(2) }}
                </p>
                <p v-if="current.similarity !== null" class="mt-1 tabular-nums">
                  最高相似度
                  <b :class="currentConfidence.textClass">{{
                    Number(current.similarity).toFixed(2)
                  }}</b>
                  <span :class="currentConfidence.textClass" :title="currentConfidence.hint"
                    >（{{ currentConfidence.label }}）</span
                  >
                </p>
                <p v-else class="mt-1 text-warning-ink">
                  库里还没有可比质心（质心只由人工确认的样本生成）
                </p>
              </div>
            </div>

            <p class="mt-2 text-caption text-ink-weak">
              来源：{{ photoLabel || EMPTY }}
              <RouterLink
                v-if="current?.photoCode"
                :to="photoDetailLink(current.photoCode, { path: '/review', name: '待确认' })"
                class="ml-1 text-brand-ink hover:underline"
                >在照片里看这张脸 →</RouterLink
              >
            </p>

            <!-- 批量确认：同一聚类簇下所有人脸一次全确认（2 次点击） -->
            <div
              v-if="current?.clusterCode"
              class="mt-3 rounded-btn border border-line bg-surface p-2"
            >
              <p class="text-caption text-ink-sub">
                这张脸属于一个聚类簇 —— 同簇的人脸多半是同一个人。
              </p>
              <el-button
                size="small"
                class="mt-2"
                :loading="review.clusterLoading"
                @click="openCluster(current.clusterCode)"
              >
                <Layers class="mr-1 h-3.5 w-3.5" aria-hidden="true" />批量确认这一簇
              </el-button>
            </div>
          </section>

          <!-- 右：候选人物（并排比对的第二半） -->
          <section aria-label="候选人物">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="pb-hint">候选人物（按相似度降序）</p>
              <div class="flex items-center gap-2">
                <el-button size="small" :disabled="!mergeFrom" @click="openMerge">
                  <Merge class="mr-1 h-3.5 w-3.5" aria-hidden="true" />与某人合并
                </el-button>
                <el-button
                  size="small"
                  type="primary"
                  title="常用操作：把这张脸改判给别的人"
                  :disabled="!current"
                  @click="openFixForCurrent"
                >
                  <ArrowRightLeft class="mr-1 h-3.5 w-3.5" aria-hidden="true" />改判到…
                </el-button>
              </div>
            </div>

            <ul
              v-if="candidates.length"
              class="mt-2 divide-y divide-line rounded-btn border border-line"
            >
              <CandidateRow
                v-for="(candidate, index) in candidates"
                :key="candidate.personCode"
                :candidate="candidate"
                :index="index"
                :threshold-low="T_LOW"
                :threshold-high="T_HIGH"
                :loading="review.busyFaceCode === current?.faceCode"
                @confirm="onConfirm"
                @fix="openFixWithCandidates"
              />
            </ul>

            <p
              v-else
              class="mt-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
            >
              还没有可比对的人物档案。质心<b>只由人工确认的样本生成</b> ——
              在你确认过某个人 3 张脸之前，这里永远是空的。用下面的
              「新建人物」建一个档案并归属，之后同类照片就能自动比对了。
            </p>

            <!-- 灰区提示：解释「为什么这里要你动手」 -->
            <p
              class="mt-3 flex items-start gap-2 rounded-btn bg-info-soft px-3 py-2 text-caption text-info-ink"
            >
              <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>
                相似度落在灰区（{{ T_LOW.toFixed(2) }}–{{ T_HIGH.toFixed(2) }}）的候选
                <b>不做自动归属</b>，标橙色提示 —— 那正是机器没把握、最需要你判断的。
                灰区一律进这个队列，不静默归属。
              </span>
            </p>

            <!-- 无匹配出路 -->
            <div class="mt-3 flex flex-wrap items-center gap-2">
              <span class="pb-hint">都不像？</span>
              <el-button size="small" :disabled="!current" @click="openFixForCurrent">
                <UserPlus class="mr-1 h-3.5 w-3.5" aria-hidden="true" />新建人物并归属
              </el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :disabled="!current"
                @click="onIgnore"
              >
                标记为陌生人
              </el-button>
            </div>
            <p class="pb-hint mt-2">
              标记为陌生人会<b>永久排除</b>这张脸（不再出现在任何队列、不参与聚类），
              所以点下去还有一道确认。新建人物后会<b>自动把当前这张脸归属给他</b>，
              不需要建完再回去点一次。
            </p>
          </section>
        </div>

        <!-- 键盘快捷键 -->
        <p class="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3 pb-hint">
          <Keyboard class="h-3.5 w-3.5" aria-hidden="true" />
          键盘快捷键：<kbd class="rounded border border-line px-1">1</kbd> /
          <kbd class="rounded border border-line px-1">2</kbd> /
          <kbd class="rounded border border-line px-1">3</kbd>
          选择候选 · <kbd class="rounded border border-line px-1">N</kbd> 新建人物 ·
          <kbd class="rounded border border-line px-1">S</kbd> 跳过（排到队列最后） ·
          <kbd class="rounded border border-line px-1">I</kbd> 忽略
          <span class="w-full">
            输入框里打字时快捷键自动让位；任何确认浮层打开时也不响应。
          </span>
        </p>
      </el-tab-pane>

      <!-- ================= Tab2 我不同意 ================= -->
      <el-tab-pane name="disputed">
        <template #label>
          <span class="inline-flex items-center gap-1">
            我不同意
            <span class="tabular-nums font-medium">{{
              formatCount(review.disputedCount)
            }}</span>
          </span>
        </template>

        <div class="pt-2">
          <p class="pb-hint mb-3">
            按照片分组 · 整张一键否决 ≤2 次点击 · 支持簇内批量改判。
            否决后这些脸回到「待确认」Tab。
          </p>
          <DisputedList
            :groups="review.disputedGroups"
            :loading="review.disputedLoading"
            :busy-photo-code="review.busyPhotoCode"
            @reject-group="onRejectGroup"
            @reject-face="onRejectFace"
            @fix="openFixFromDisputed"
            @split="openSplit"
            @batch-cluster="onBatchCluster"
          />
          <div v-if="review.disputedPhotoTotal" class="mt-3 flex justify-end">
            <el-pagination
              layout="prev, pager, next"
              background
              :total="review.disputedPhotoTotal"
              :page-size="20"
              :current-page="review.disputedPage"
              @current-change="onDisputedPage"
            />
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 改判浮层（三种 action 共用） -->
    <FixFaceDialog
      v-model="fixVisible"
      :faces="fixFaces"
      :siblings="fixSiblings"
      :candidates="fixCandidates"
      :search-fn="review.searchPersons"
      :mode="fixMode"
      :loading="fixLoading"
      :photo-label="fixPhotoLabel"
      :threshold-low="T_LOW"
      :threshold-high="T_HIGH"
      @submit="submitFix"
      @create-person="createPersonAndAssign"
    />

    <!-- 「忽略此人脸」= 标记陌生人：不可逆语义，必须二次确认 -->
    <el-dialog
      v-model="ignoreVisible"
      title="确认忽略这张人脸？"
      width="460px"
      :close-on-click-modal="false"
    >
      <p class="text-body text-ink-sub">
        忽略 = 标记为<b>陌生人</b>：这张脸会被永久排除，不再出现在待确认队列、
        不参与聚类、也不会再被自动归属。只有手动改判才回得来。
      </p>
      <p v-if="current" class="mt-2 text-caption text-ink-weak">
        来源：{{ photoLabel || current.photoCode }}
      </p>
      <template #footer>
        <el-button :disabled="ignoreLoading" @click="ignoreVisible = false">取消</el-button>
        <el-button type="danger" :loading="ignoreLoading" @click="doIgnore">
          确认忽略（永久排除）
        </el-button>
      </template>
    </el-dialog>

    <!-- 拆分：复用 FixFaceDialog（variant="split"），先问「它属于谁」再执行 -->
    <FixFaceDialog
      v-model="splitVisible"
      variant="split"
      mode="assign"
      :faces="splitFaces"
      :candidates="[]"
      :persons="splitPersons"
      :search-fn="review.searchPersons"
      :loading="splitLoading"
      :photo-label="splitPhotoLabel"
      @submit="onSplitSubmit"
      @create-person="onSplitCreatePerson"
    />

    <!-- 合并：先选目标，再由 ConfirmMerge 复述双方 + 照片数，最后执行 -->
    <ConfirmMerge
      v-model="mergeVisible"
      :from="mergeFrom"
      :to="mergeTo"
      :loading="mergeLoading"
      @confirm="doMerge"
    />
    <el-dialog
      v-model="mergePickerVisible"
      title="合并到谁名下？（被合并方的档案会消失）"
      width="480px"
    >
      <p class="pb-hint mb-2">
        被合并方：<b>{{ mergeFrom?.displayName || '（无）' }}</b> ——
        选好保留方后会再复述一次双方姓名与照片数，确认无误才真正执行。
        目标取自<b>全部人物</b>（同名不同人往往不在相似度候选里）。
      </p>
      <el-input
        v-model="mergeKeyword"
        placeholder="搜索要合并到谁"
        clearable
        size="small"
        class="mb-2"
        aria-label="搜索合并目标人物"
      />
      <ul class="max-h-72 divide-y divide-line overflow-y-auto rounded-btn border border-line">
        <li v-for="person in mergeCandidates" :key="person.personCode">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface"
            @click="mergeTarget = person.personCode"
          >
            <img
              v-if="person.thumbUrl"
              :src="person.thumbUrl"
              class="h-7 w-7 shrink-0 rounded-full border border-line object-cover"
              alt=""
            />
            <span class="min-w-0 flex-1 truncate text-body text-ink">{{ person.displayName }}</span>
            <span class="shrink-0 text-caption tabular-nums text-ink-weak">
              {{ person.photoCount }} 张照片
            </span>
            <span
              v-if="person.personCode === mergeTarget"
              class="shrink-0 text-caption text-brand-ink"
              >已选</span
            >
            <span v-else class="shrink-0 text-caption text-ink-weak">选它</span>
          </button>
        </li>
      </ul>
      <p v-if="!mergeCandidates.length" class="pb-hint mt-2">
        没有可合并的人物（库里只有被合并方自己）。
      </p>
      <template #footer>
        <el-button @click="mergePickerVisible = false">取消</el-button>
        <el-button type="danger" :disabled="!mergeTo" @click="toMergeConfirm">
          下一步：复述双方信息
        </el-button>
      </template>
    </el-dialog>

    <!-- 批量确认：确认弹窗（验收第 6 条：≤2 次点击） -->
    <el-dialog
      v-model="clusterDialogVisible"
      :title="`一次性确认这一簇的 ${review.clusterMembers.length} 张脸？`"
      width="720px"
    >
      <p class="pb-hint">
        一次事务 + 一次质心重算 + <b>一条</b>日志（不是逐张落 N 条）。
        确认完这 {{ review.clusterMembers.length }} 张脸会一起从待确认队列消失。
      </p>

      <div class="mt-3 max-h-56 overflow-y-auto">
        <BucketTimeline :groups="clusterGroups" :max-per-bucket="8" />
      </div>

      <div class="mt-4">
        <p class="pb-hint mb-1">全部确认给谁？</p>
        <p
          v-if="!candidates.length"
          class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
        >
          这一簇还没有候选人物可选。请先确认其中一张脸（或新建一个档案），
          再回来做批量确认。
        </p>
        <ul
          v-else
          class="max-h-48 divide-y divide-line overflow-y-auto rounded-btn border border-line"
        >
          <li v-for="(candidate, index) in candidates" :key="candidate.personCode">
            <label class="flex items-center gap-3 px-3 py-2">
              <input
                v-model="clusterPick"
                type="radio"
                name="cluster-pick"
                :value="candidate.personCode"
                class="h-4 w-4"
              />
              <span
                class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-btn border border-line text-caption tabular-nums"
                >{{ index + 1 }}</span
              >
              <span class="min-w-0 flex-1 truncate text-body text-ink">
                {{ candidate.displayName }}
              </span>
              <span class="shrink-0 text-caption tabular-nums text-ink-weak">
                {{
                  candidate.similarity === null
                    ? EMPTY
                    : Number(candidate.similarity).toFixed(2)
                }}
              </span>
            </label>
          </li>
        </ul>
      </div>

      <template #footer>
        <el-button :disabled="batchLoading" @click="clusterDialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :disabled="!clusterPick"
          :loading="batchLoading"
          @click="doBatchConfirm"
        >
          确认这一簇的 {{ review.clusterMembers.length }} 张脸
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>




