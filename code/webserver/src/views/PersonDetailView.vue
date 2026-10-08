<!--
  P-05 人物详情（设计稿 §4.5）
  ------------------------------------------------------------
  头部：头像 + 姓名 + 家庭关系 + 分类 + 照片数 / 年代跨度
  操作：合并到… ｜ **撤销上次合并·拆分** ｜ **改头像（DR-41）** ｜ 编辑资料 ｜ 操作历史 ｜ 停用
  Tab1 时间轴：**按年代档分组**（对齐 S0 结论：分年代档是刚需），空年代档不显示
  Tab2 人脸样本：**必须分两段** —— 人工确认（实线）/ 自动归属（虚线）。
        混在一起用户无法判断哪些值得复核；自动段每张带「✗ 移除」。
        **每张可设为默认头像**（DR-41）：加**品牌色环 + ★ 默认角标**，
        但**不改编描边** —— 描边承担「归属来源」（实线=确认 / 虚线=机器认的）
        这一维，拿它表示「这是默认」会把两种语义混成一种。
  质心健康度：自动 ≫ 确认时主动提示，用数据引导用户去纠错（DR-16）

  五条不能省的语义
  --------------
  ① **撤销只对 SPLIT / MERGE / BUCKET_FIX 开放**（DR-16 / DR-42）。
     普通确认的逆操作是「再点一次改判」，状态机自己回得去；把撤销按钮在无候选时
     置灰并写明原因，而不是让它变成一个点了必报错的按钮。
  ② 撤销对话框里**必须**写「质心的原值回不来，只能按现在的样本重算」。
  ③ Tab2 的描边/图标/文字全部走 `utils/faceState.js`，与照片详情页的 FaceBox 同源。
     这里另写一套配色的话，用户在两页看到同一张脸会得到两种说法。
  ④ 停用是**先看影响面 + 二次确认**（DR-19），且不提供删除。
  ⑤ **头像两级**（DR-40）：用户指定的默认（`avatarFaceCode`）→ 服务端算好的
     **代表脸**（`coverFaceCode`）。设默认只写 `pb_person.avatarFaceCode` 一列，
     **不进质心、不改 `pb_face`、不写 `pb_review_log`** —— 它是展示，不是归属纠错。
     选中那张脸被移除/合并走时**不清理**该列，靠这条回退链自然降级。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleCheck,
  CircleSlash,
  History,
  Merge,
  Pencil,
  TriangleAlert,
  Undo2,
  UserRound,
  X,
} from 'lucide-vue-next'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import BucketTimeline from '@/components/photo/BucketTimeline.vue'
import AvatarPicker from '@/components/common/AvatarPicker.vue'
import PersonForm from '@/components/common/PersonForm.vue'
import PersonPlaces from '@/components/common/PersonPlaces.vue'
import { disableContact, getImpact, patchContact } from '@/api/contacts'
import { fixFace, getRevertible, getReviewLog, undoOperation } from '@/api/review'
import { faceUrl } from '@/api/static'
import { faceStateOf, similarityText } from '@/utils/faceState'
import { baseName, formatBucketKey, formatCount } from '@/utils/format'
// 进照片详情要**声明来源与翻页范围**：否则详情页的「返回」只会回照片流、
// 左右箭头会翻到全库（见 utils/photoReturn.js 顶部）
import { currentSource, goBackOr, photoDetailLink, scopeParam } from '@/utils/photoReturn'
import { categoryLabelOf, relationLabelOf, usePersonsStore } from '@/store/persons'
import { useReviewStore } from '@/store/review'

const route = useRoute()
const router = useRouter()
const persons = usePersonsStore()
const review = useReviewStore()

const personCode = computed(() => String(route.params.personCode || ''))
const person = computed(() => persons.current)

const activeTab = ref('timeline')

const editVisible = ref(false)
const submitting = ref(false)

const disableVisible = ref(false)
const disableImpact = ref(null)
const disableSubmitting = ref(false)

/** 可撤销的最近一条 SPLIT / MERGE（null = 没有可撤销的） */
const revertible = ref(null)
const undoVisible = ref(false)
const undoSubmitting = ref(false)

const historyVisible = ref(false)
const historyRows = ref([])
const historyLoading = ref(false)

const families = computed(() => persons.families)

/**
 * 头像 = 服务端解析好的 `coverFaceCode`（DR-40）：用户指定的默认（**得还活着**）
 * → 代表脸。两级判断都在 `personCoversOf()` 里做完了。
 *
 * ⚠️ 不再自己取「第一张确认样本」：那与人物库卡片的口径不一致
 *    （列表是「确认优先 + 质量最高的一张」，详情是「样本列表里的第一张」），
 *    同一张脸在两处显示不同，用户会以为自己看错了人。
 * ⚠️ 也**不要**拿 `avatarFaceCode` 兜底 —— 它可能是失效的那张（见 DR-41④）。
 * ⚠️ 裁剪图仍可能 404（脸在库里但裁剪图还没落盘）—— 由 `@error` 回退图标兜底，
 *    服务端不做文件系统探测。
 */
const avatarFaceCode = computed(() => person.value?.coverFaceCode || '')
const avatarSrc = computed(() => (avatarFaceCode.value ? faceUrl(avatarFaceCode.value) : ''))
const avatarBroken = ref(false)
watch(avatarSrc, () => {
  avatarBroken.value = false
})
const showAvatar = computed(() => Boolean(avatarSrc.value) && !avatarBroken.value)

const photoCount = computed(() => Number(person.value?.photoCount) || 0)
const faceCount = computed(() => Number(person.value?.faceCount) || 0)
const confirmedCount = computed(() => Number(person.value?.confirmedFaceCount) || 0)
const autoCount = computed(() => Number(person.value?.autoFaceCount) || 0)

const isDisabled = computed(() => String(person.value?.delFlag) === '1')

const yearSpan = computed(() => {
  const low = person.value?.yearLow
  const high = person.value?.yearHigh
  if (!low && !high) return '—'
  if (!low || !high || String(low) === String(high)) return String(low || high)
  return `${low}–${high}`
})

/** 生日决定的年龄跨度（年代档划分的直接依据），没生日就说「无生日：按等宽 5 年划分年代档」 */
const ageSpanText = computed(() => {
  const birthday = String(person.value?.birthday || '')
  const low = person.value?.yearLow
  const high = person.value?.yearHigh
  if (!birthday || !low || !high) return '无生日，按等宽 5 年划分年代档'
  const born = Number(String(birthday).slice(0, 4))
  if (!born) return '无生日，按等宽 5 年划分年代档'
  return `${birthday} · ${Math.max(0, Number(low) - born)}–${Math.max(0, Number(high) - born)} 岁`
})

// ============================================================
// Tab2 两段：**用 faceStateOf 推四态**，不另写配色
// ============================================================

const faceRows = computed(() => persons.faces?.items || [])

const facesConfirmed = computed(() =>
  faceRows.value.filter((one) => faceStateOf(one).key === 'confirmed'),
)
const facesAuto = computed(() =>
  faceRows.value.filter((one) => faceStateOf(one).key === 'disputed'),
)

/** 两段的条数取服务端 counts（不是本页 items.length —— 有 limit 时两者不同） */
const countConfirmed = computed(
  () => Number(persons.faces?.counts?.confirmed ?? facesConfirmed.value.length),
)
const countAuto = computed(
  () => Number(persons.faces?.counts?.disputed ?? facesAuto.value.length),
)

/**
 * 质心健康度（DR-16 硬约束）
 * 「自动 ≫ 人工确认」时主动提示 —— 用户在照片流里逛几个月都不会主动来复核，
 * 等他自己发现「有个人越看越不像」早就晚了。
 */
const healthHint = computed(() => {
  if (confirmedCount.value === 0) {
    if (faceCount.value === 0) {
      return '还没有任何脸归属给他。质心只由**人工确认**的样本生成（防污染），'
        + '所以先在待确认队列里认几张，这里才会开始变准。'
    }
    return `确认样本为 0：质心不存在，他**不参与自动匹配**（${faceCount.value} 张脸全走队列）。`
  }
  if (autoCount.value >= confirmedCount.value * 3) {
    const suggest = Math.min(autoCount.value, Math.max(1, autoCount.value - confirmedCount.value))
    return `自动归属 ${autoCount.value} 张远多于人工确认 ${confirmedCount.value} 张，`
      + `识别质量偏低，建议人工确认 ${suggest} 张 —— 认错的那张会把质心拉偏，越错越错。`
  }
  return ''
})

// ============================================================
// 数据加载
// ============================================================

async function load() {
  if (!personCode.value) return
  await Promise.all([
    persons.fetchPersonDetail(personCode.value),
    persons.fetchFamilies(),
    loadRevertible(),
  ])
}

async function loadRevertible() {
  try {
    const data = await getRevertible({ size: 20 })
    const items = data?.items || []
    // 只认 SPLIT / MERGE：只判 MERGE 会漏掉「拆分」这一半（DR-16 补充）
    const usable = items.filter(
      (one) => one.opType === 'SPLIT' || one.opType === 'MERGE',
    )
    // 只显示与当前人物相关的（一方是 TA 就该在这里出现）
    revertible.value = usable.find(
      (one) =>
        String(one.fromPersonCode || '') === personCode.value
        || String(one.toPersonCode || '') === personCode.value,
    ) || null
  } catch (e) {
    revertible.value = null
  }
}

onMounted(load)
watch(personCode, load)
onBeforeUnmount(() => {
  persons.clearPerson()
})

// ============================================================
// Tab1 时间轴
// ============================================================

const timelineGroups = computed(() => persons.timeline?.groups || [])

/**
 * 进照片详情的来源声明：
 *   `path` 是本页（含 tab 等状态）；`name` 用**人名** —— 详情页的返回按钮会显示
 *   「← 返回 白瑞琴」，比「← 返回上一页」有信息量；
 *   `scope` 让左右箭头沿**这个人的照片**翻，而不是翻进全库照片流。
 */
const photoSource = computed(() => ({
  ...currentSource(route, person.value?.displayName || ''),
  scope: scopeParam('person', personCode.value),
}))

/**
 * 页头返回：与照片详情页**同一语义**（见 utils/photoReturn.js 顶部）——
 * 能回退就回退（从人物库点进来的就回人物库），没有站内上一页才 replace 兜底。
 * 写死 RouterLink 的毛病（页面按钮与浏览器后退键行为不一致）在这里一模一样。
 */
function goBack() {
  goBackOr(router, '/people')
}

function onTimelineSelect({ photo }) {
  router.push(photoDetailLink(photo.photoCode, photoSource.value))
}

// ============================================================
// Tab2：移除一个自动样本
// ============================================================

const removingFaceCode = ref('')

/**
 * 移除一个自动归属样本 = `fix('unknown')`：脸退回**待确认队列**。
 *
 * ⚠️ 为什么这里用 unknown 而不是 split：
 *   「移除」表达的是「我不确定这是他」，那是**否决**；
 *   「他其实是我表弟」才是拆分（走 /review/split，可撤销）。
 *   两者混用会让撤销入口也一起失真。
 */
async function removeAutoSample(sample) {
  removingFaceCode.value = sample.faceCode
  try {
    const data = await fixFace({ faceCodes: [sample.faceCode], action: 'unknown' })
    review.applyCounts(data)
    ElMessage.success('已移除：这张脸回到待确认队列')
    await Promise.all([
      persons.fetchFaces(personCode.value),
      persons.fetchPerson(personCode.value),
      review.fetchBadge(),
    ])
  } finally {
    removingFaceCode.value = ''
  }
}

/** 一键把某个自动样本确认下来（最常见的正向操作，不该逼他去队列页） */
const confirmingFaceCode = ref('')
async function confirmAutoSample(sample) {
  confirmingFaceCode.value = sample.faceCode
  try {
    const data = await fixFace({
      faceCodes: [sample.faceCode],
      action: 'assign',
      personCode: personCode.value,
    })
    review.applyCounts(data)
    ElMessage.success('已人工确认：这张脸的质心已重算')
    await Promise.all([
      persons.fetchFaces(personCode.value),
      persons.fetchPerson(personCode.value),
      review.fetchBadge(),
    ])
  } finally {
    confirmingFaceCode.value = ''
  }
}

// ============================================================
// 撤销上次合并 / 拆分
// ============================================================

function openUndo() {
  if (!revertible.value) return
  undoVisible.value = true
}

async function confirmUndo() {
  if (!revertible.value) return
  undoSubmitting.value = true
  try {
    const isYearFix = revertible.value?.opType === 'BUCKET_FIX'
    const data = await undoOperation({ logCode: revertible.value.logCode })
    // ⚠️ 两类撤销的产物完全不同：归属类搬人脸，年代修正改的是**照片的年份**
    //    （facesRestored 恒为 0）。用同一句话报"还原了 0 张脸"会让用户
    //    以为撤销没生效。
    ElMessage.success(
      isYearFix
        ? `已撤销年代修正：拍摄年代还原为 `
          + `${data.restoredOverride ?? '自动（扫描时读到的年份）'}，`
          + `${data.bucketsChanged || 0} 张人脸的年代档已重刷`
        : `已撤销：${data.facesRestored || 0} 张脸的归属、`
          + `${data.linksRestored || 0} 条照片关联与双方质心都已还原`,
    )
    undoVisible.value = false
    await Promise.all([load(), review.fetchBadge()])
  } finally {
    undoSubmitting.value = false
  }
}

/**
 * 撤销按钮的文案：按**这次到底是什么操作**说。
 *
 * ⚠️ 不能一律叫「撤销上次合并」：`/review/revertible` 是**通用的**可撤销列表，
 *    按时间倒序取最新一条。这个人的某张照片刚被修正过年代时，最新那条就是
 *    BUCKET_FIX —— 按钮却写着"撤销上次合并"，点下去做的是别的事。
 */
const undoButtonLabel = computed(() =>
  revertible.value?.opType === 'BUCKET_FIX' ? '撤销上次年代修正' : '撤销上次合并',
)

function describeRevertible() {
  const one = revertible.value
  if (!one) return ''
  if (one.opType === 'BUCKET_FIX') {
    // 年代修正的主日志没有 from/to 人（一次修正可能牵动多人，写谁都不对），
    // 所以这里改用照片编码说清「撤的是哪一张」。
    return `把这张照片的年代修正还原（照片 ${one.photoCode || '—'}）`
  }
  const isMerge = one.opType === 'MERGE'
  return isMerge
    ? `把「${one.fromDisplayName || one.fromPersonCode}」合并进「${one.toDisplayName || one.toPersonCode}」`
    : `把 1 张脸从「${one.fromDisplayName || one.fromPersonCode || '（未归属）'}」拆出`
}

// ============================================================
// 操作历史（pb_review_log）
// ============================================================

const OP_LABEL = {
  ASSIGN: '确认归属',
  FIX: '改判',
  UNKNOWN: '置为未知',
  STRANGER: '标记陌生人',
  MERGE: '合并',
  SPLIT: '拆分',
  UNDO: '撤销',
  DISABLE: '停用',
  ENABLE: '恢复',
  // DR-42：某张照片的**拍摄年代**被人工修正（这张脸换了年代档）
  BUCKET_FIX: '修正年代',
}

async function openHistory() {
  historyVisible.value = true
  historyLoading.value = true
  try {
    // 按人物查：这个人的全部纠错记录（这张脸当初怎么被认成这个人的）
    const data = await getReviewLog({ personCode: personCode.value, size: 50 })
    historyRows.value = data?.items || []
  } finally {
    historyLoading.value = false
  }
}

function opLabel(opType) {
  return OP_LABEL[opType] || opType || '—'
}

// ============================================================
// 编辑资料 / 停用
// ============================================================

async function submitEdit(form) {
  submitting.value = true
  try {
    const data = await patchContact(personCode.value, {
      displayName: String(form.displayName || '').trim(),
      familyName: form.familyName || undefined,
      relation: form.relation || undefined,
      familyGroupCode: form.familyGroupCode || undefined,
      birthday: form.birthday || undefined,
      email: form.email || undefined,
      phone: form.phone || undefined,
      categories: Array.isArray(form.categories) ? form.categories : [],
    })
    editVisible.value = false
    // ⚠️ 只有真的重算了才提示（DR-18）：对无影响的改动弹「质心已重算」是噪声
    if (data?.centroidRebuilt) {
      ElMessage.success('生日已改，该人的年代档与质心已重算')
      await persons.fetchTimeline(personCode.value)
    } else {
      ElMessage.success('已保存')
    }
    await persons.fetchPerson(personCode.value)
  } catch (e) {
    if (e?.code === 'DUPLICATE_DISPLAY_NAME') {
      const existing = e?.payload?.existing || {}
      ElMessage.warning(
        `已存在「${existing.displayName || ''}」，你可能是想合并到 TA？`
        + `（${existing.photoCount ?? 0} 张照片 / ${existing.faceCount ?? 0} 张脸）`
        + ' —— 点下面的「合并到…」把它并过来',
      )
    }
  } finally {
    submitting.value = false
  }
}

async function openDisable() {
  disableVisible.value = true
  disableImpact.value = null
  try {
    disableImpact.value = await getImpact(personCode.value)
  } catch (e) {
    disableImpact.value = null
  }
}

async function confirmDisable() {
  disableSubmitting.value = true
  try {
    await disableContact(personCode.value, true)
    ElMessage.success('已停用：人脸已退回待确认队列')
    disableVisible.value = false
    await review.fetchBadge()
    router.push('/people')
  } finally {
    disableSubmitting.value = false
  }
}

// ============================================================
// 默认头像（DR-41）—— **展示**字段：不进质心、不改 pb_face、不写 review 日志
// ============================================================
//
// 老实现（本步删掉的 `setAvatar`）只弹一句「不提供编辑入口」的提示 ——
// 它是设计稿里那个「改头像」按钮的占位符，从来没有任何入口调它。

const avatarPickerVisible = ref(false)
const avatarSubmitting = ref(false)
/** 正在提交的那一张（按钮级 loading，避免整片样本一起转圈） */
const settingFaceCode = ref('')

/** 当前默认头像的 faceCode（空 = 没设过，展示的是服务端回退的代表脸） */
const defaultFaceCode = computed(() => String(person.value?.avatarFaceCode || ''))

/** 选择器用的全量样本（与 Tab2 同一个数据源，含四态） */
const avatarSamples = computed(() => faceRows.value)

function isDefaultAvatar(sample) {
  return Boolean(sample?.faceCode) && String(sample.faceCode) === defaultFaceCode.value
}

/**
 * 提交默认头像（`faceCode` 传空串 = 清空，回退到自动代表脸）。
 *
 * ⚠️ 自动归属段（未确认）的样本**也可以**设：头像只影响展示，
 *    不进质心、不改 `pb_face` —— 没必要逼用户先去确认那张脸。
 * ⚠️ 失败**不留假状态**也不重复报错：axios 拦截器已经弹过后端 message
 *    （含「这张脸不属于他」那条 400）；不 catch 的话 Vue 会把它记成
 *    「未处理的事件处理器错误」，反而把那条真正有用的 Toast 淹掉。
 */
async function applyAvatar(faceCode) {
  avatarSubmitting.value = true
  try {
    await persons.setAvatar(personCode.value, faceCode)
    avatarPickerVisible.value = false
    ElMessage.success(faceCode ? '已设为默认头像' : '已清除默认头像，卡片回到自动代表脸')
  } catch {
    // 见函数头：错误已经由拦截器报过，这里只收尾
  } finally {
    avatarSubmitting.value = false
  }
}

async function setDefaultAvatar(sample) {
  if (isDefaultAvatar(sample)) return
  settingFaceCode.value = sample.faceCode
  try {
    await applyAvatar(sample.faceCode)
  } finally {
    settingFaceCode.value = ''
  }
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 两个返回，与照片详情页同一套：
         ① 主按钮 = 回退到**来的那一页**（和浏览器后退键同语义）；
         ② 次级 = 固定入口，不关心来源、就是要看列表。 -->
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
      <button
        type="button"
        class="inline-flex items-center gap-1 rounded-btn text-caption text-ink-sub transition-colors duration-150 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
        @click="goBack"
      >
        <ArrowLeft class="h-3.5 w-3.5" aria-hidden="true" />返回上一页
      </button>
      <span class="text-caption text-ink-weak" aria-hidden="true">|</span>
      <RouterLink
        to="/people"
        class="text-caption text-ink-sub transition-colors duration-150 hover:text-ink"
        >人物库</RouterLink
      >
    </div>

    <!-- 加载中骨架 -->
    <section v-if="persons.currentLoading && !person" class="pb-card animate-pulse p-4">
      <div class="flex items-center gap-4">
        <div class="h-16 w-16 shrink-0 rounded-full bg-skeleton" />
        <div class="flex-1 space-y-2">
          <div class="h-5 w-40 rounded-btn bg-skeleton" />
          <div class="h-3 w-56 rounded-btn bg-skeleton" />
        </div>
      </div>
    </section>

    <section v-else-if="!person" class="pb-card px-4 py-12 text-center text-body text-ink-weak">
      找不到这个人（可能已被合并或停用）。
      <RouterLink class="ml-2 text-brand-ink hover:underline" to="/people">回人物库</RouterLink>
    </section>

    <template v-else>
      <!-- 头部 -->
      <section class="pb-card p-4">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div class="flex min-w-0 items-center gap-4">
            <!-- 头像：与人物库卡片同口径（DR-40 的 coverFaceCode）；照片**铺满**圆框，
                 不做「留环/双圈」（2026-10-08 试过留环缩小，用户否决后回退） -->
            <span
              class="flex h-16 w-16 shrink-0 items-center justify-center overflow-hidden rounded-full border-2 bg-surface"
              :class="isDisabled ? 'border-line opacity-60' : 'border-brand-soft'"
              aria-hidden="true"
            >
              <img
                v-if="showAvatar"
                :src="avatarSrc"
                alt=""
                class="h-full w-full object-cover"
                @error="avatarBroken = true"
              />
              <UserRound v-else class="h-7 w-7 text-ink-weak" />
            </span>
            <div class="min-w-0">
              <h2 class="truncate text-title text-ink">{{ person.displayName }}</h2>
              <p class="mt-1 text-body text-ink-sub">
                <template v-if="person.relation">{{ relationLabelOf(person.relation) }} · </template>
                {{ person.family?.familyName || person.familyName || '未归入家庭组' }}
                <span class="mx-1 text-ink-weak">|</span>
                {{ photoCount }} 张照片 · {{ faceCount }} 张脸
                <span class="mx-1 text-ink-weak">|</span>
                {{ yearSpan }}
              </p>
              <p class="pb-hint mt-0.5">{{ ageSpanText }}</p>
              <ul class="mt-2 flex flex-wrap gap-1.5">
                <li
                  v-for="name in person.categories || []"
                  :key="name"
                  class="rounded-btn bg-accent-soft px-2 py-0.5 text-caption text-ink-sub"
                >
                  {{ categoryLabelOf(name) }}
                </li>
                <li
                  v-if="isDisabled"
                  class="inline-flex items-center gap-1 rounded-btn bg-surface px-2 py-0.5 text-caption text-ink-weak"
                >
                  <CircleSlash class="h-3 w-3" aria-hidden="true" />已停用
                </li>
                <li
                  v-if="person.totalConfirmedBuckets"
                  class="rounded-btn bg-surface px-2 py-0.5 text-caption text-ink-sub"
                  :title="`该人有 ${person.totalConfirmedBuckets} 个年代档的质心可用（样本 >= 3）`"
                >
                  {{ person.totalConfirmedBuckets }} 个年代档参与匹配
                </li>
              </ul>
            </div>
          </div>

          <!-- 操作都在手边，不藏三级菜单 -->
          <div class="flex flex-wrap gap-2">
            <el-button type="primary" @click="router.push(`/people?mergeInto=${person.personCode}`)">
              <Merge class="mr-1 h-4 w-4" aria-hidden="true" />合并到…
            </el-button>
            <el-tooltip
              placement="bottom"
              :disabled="Boolean(revertible)"
            >
              <template #content>
                {{
                  revertible
                    ? `撤销：${describeRevertible()}`
                    : '没有可撤销的操作。只有「合并」「拆分」与「照片年代修正」可撤销 —— 它们会抹掉一个只能从日志里恢复的事实。'
                }}
              </template>
              <span>
                <el-button :disabled="!revertible" @click="openUndo">
                  <Undo2 class="mr-1 h-4 w-4" aria-hidden="true" />{{ undoButtonLabel }}
                </el-button>
              </span>
            </el-tooltip>
            <!-- 改头像（DR-41）：只影响展示，不碰原图、不生成任何图片 -->
            <el-tooltip placement="bottom" :disabled="Boolean(person.faceCount)">
              <template #content>
                这个人还没有任何人脸样本 —— 先去待确认队列认几张脸，才能选头像。
              </template>
              <span>
                <el-button
                  :disabled="!person.faceCount"
                  @click="avatarPickerVisible = true"
                >
                  <UserRound class="mr-1 h-4 w-4" aria-hidden="true" />改头像
                </el-button>
              </span>
            </el-tooltip>
            <el-button @click="editVisible = true">
              <Pencil class="mr-1 h-4 w-4" aria-hidden="true" />编辑资料
            </el-button>
            <el-button @click="openHistory">
              <History class="mr-1 h-4 w-4" aria-hidden="true" />操作历史
            </el-button>
            <el-button v-if="!isDisabled" type="danger" plain @click="openDisable">
              <CircleSlash class="mr-1 h-4 w-4" aria-hidden="true" />停用
            </el-button>
          </div>
        </div>

        <!-- 质心健康度（DR-16） -->
        <p
          v-if="healthHint && !isDisabled"
          class="mt-4 flex items-start gap-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
        >
          <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>{{ healthHint }}</span>
        </p>
        <p
          v-else-if="isDisabled"
          class="mt-4 flex items-start gap-2 rounded-btn bg-surface px-3 py-2 text-caption text-ink-sub"
        >
          <CircleSlash class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>
            已停用：质心已删除、人脸已退回待确认队列。恢复后需重新确认人脸才能自动匹配。
          </span>
        </p>
      </section>

      <el-tabs v-model="activeTab" class="pb-card px-4 pb-4">
        <!-- ============ Tab1 时间轴（按年代档分组） ============ -->
        <el-tab-pane :label="`时间轴（${formatCount(persons.timeline?.groups?.length || 0)} 个年代档）`" name="timeline">
          <div class="space-y-4 pt-2">
            <p v-if="persons.timelineLoading" class="pb-hint">正在按年代档分组…</p>
            <BucketTimeline
              v-else
              :groups="timelineGroups"
              :max-per-bucket="12"
              @select="onTimelineSelect"
            />
            <p class="pb-hint">
              时间轴按<b>年代档</b>分组（童年 3 年 / 成年 10 年）——
              人脸跨年龄漂移是这个工具的核心难点（S0 实测：不分年代档时误认率 32.75%，
              分年代档后约 19%）。空年代档不显示；每个年代档最多 12 张，其余折成「+N」。
            </p>

            <!--
              ↓↓↓ R5：「去过的地方」↓↓↓
              **用户点名要的位置**：Tab1 内、`BucketTimeline` 与它的说明**之后**，
              **不新开 Tab**（空 Tab 比空区块更难看）。
              ⚠️ 只做**插入**：上面的时间轴一行都没动 —— 这个文件踩过
                 「一次重排把 Tab2 人脸样本的实线/虚线语义搞混」的坑。
              ⚠️ 组件内部按「有数据 / 没数据 / 加载中」三态自己决定渲染什么，
                 这里只负责把 store 里的四个值传下去。
            -->
            <PersonPlaces
              :person-code="personCode"
              :places="persons.places?.places || []"
              :photo-total="persons.places?.photoTotal || 0"
              :located-photo-total="persons.places?.locatedPhotoTotal || 0"
              :loading="persons.placesLoading"
            />
          </div>
        </el-tab-pane>

        <!-- ============ Tab2 人脸样本（两段） ============ -->
        <el-tab-pane
          :label="`人脸样本（人工 ${formatCount(countConfirmed)} · 自动 ${formatCount(countAuto)}）`"
          name="faces"
        >
          <div class="space-y-6 pt-2">
            <p v-if="persons.facesLoading" class="pb-hint">正在读取人脸样本…</p>

            <template v-else>
              <!-- ① 人工确认：实线框（faceStateOf 的 confirmed 语义） -->
              <section aria-labelledby="pd-confirmed-title">
                <h3 id="pd-confirmed-title" class="flex items-center gap-2 text-body text-ink">
                  <span
                    class="inline-block h-3 w-4 border-t-2 border-solid border-success"
                    aria-hidden="true"
                  />
                  <span aria-hidden="true">✓</span>
                  人工确认
                  <span class="tabular-nums text-ink-weak">{{ formatCount(countConfirmed) }}</span>
                </h3>
                <ul v-if="facesConfirmed.length" class="mt-3 flex flex-wrap gap-3">
                  <li v-for="sample in facesConfirmed" :key="sample.faceCode" class="w-16">
                    <RouterLink
                      :to="photoDetailLink(sample.photoCode, photoSource)"
                      class="block"
                      :aria-label="`${sample.shotYear || ''} 年照片，已人工确认`"
                    >
                      <img
                        :src="sample.thumbUrl"
                        :alt="`已人工确认的人脸样本${sample.shotYear ? `，${sample.shotYear} 年` : ''}`"
                        class="h-16 w-16 rounded-full border-2 border-solid border-success object-cover"
                        :class="isDefaultAvatar(sample) ? 'ring-2 ring-brand' : ''"
                        loading="lazy"
                      />
                      <span class="mt-1 block text-center text-caption tabular-nums text-ink-weak">
                        {{ similarityText(sample.similarity) }}
                      </span>
                    </RouterLink>
                    <!-- 设为默认头像（DR-41）：**加环不改描边** —— 描边样式承担
                         「归属来源」这一维（实线=确认/虚线=机器认的），
                         用它表示「这是默认」会把两种语义混成一种。 -->
                    <button
                      type="button"
                      class="mt-0.5 block w-full rounded-btn px-1 py-0.5 text-caption transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                      :class="
                        isDefaultAvatar(sample)
                          ? 'bg-brand-soft font-medium text-brand-ink'
                          : 'text-ink-sub hover:bg-surface'
                      "
                      :aria-label="`把这张脸设为默认头像（${sample.shotYear || '未知年份'}）`"
                      :aria-pressed="isDefaultAvatar(sample)"
                      :disabled="settingFaceCode === sample.faceCode"
                      @click="setDefaultAvatar(sample)"
                    >
                      <template v-if="isDefaultAvatar(sample)">
                        <span aria-hidden="true">★</span> 默认
                      </template>
                      <template v-else>设为默认</template>
                    </button>
                  </li>
                </ul>
                <p v-else class="pb-hint mt-2">
                  还没有人工确认的样本。质心只用这些样本生成 ——
                  <b>确认得越多，自动归属越准</b>。
                </p>
                <p v-if="facesConfirmed.length" class="pb-hint mt-2">
                  实线框 = 你亲自确认过，这些脸<b>不会再出现在任何队列里</b>。
                  带彩色环的是<b>当前默认头像</b>（只影响展示）。
                </p>
              </section>

              <!-- ② 自动归属：虚线框，每张带「✗ 移除」 -->
              <section aria-labelledby="pd-auto-title">
                <h3 id="pd-auto-title" class="flex items-center gap-2 text-body text-ink">
                  <span
                    class="inline-block h-3 w-4 border-t-2 border-dashed border-success"
                    aria-hidden="true"
                  />
                  <span aria-hidden="true">◐</span>
                  自动归属
                  <span class="tabular-nums text-ink-weak">{{ formatCount(countAuto) }}</span>
                </h3>
                <ul v-if="facesAuto.length" class="mt-3 flex flex-wrap gap-3">
                  <li v-for="sample in facesAuto" :key="sample.faceCode" class="w-16">
                    <RouterLink
                      :to="photoDetailLink(sample.photoCode, photoSource)"
                      class="block"
                      :aria-label="`${sample.shotYear || ''} 年照片，机器自动认的`"
                    >
                      <img
                        :src="sample.thumbUrl"
                        :alt="`机器自动归属的人脸样本${sample.shotYear ? `，${sample.shotYear} 年` : ''}`"
                        class="h-16 w-16 rounded-full border-2 border-dashed border-success object-cover"
                        :class="isDefaultAvatar(sample) ? 'ring-2 ring-brand' : ''"
                        loading="lazy"
                      />
                      <span class="mt-1 block text-center text-caption tabular-nums text-ink-weak">
                        {{ similarityText(sample.similarity) }}
                      </span>
                    </RouterLink>
                    <div class="mt-1 flex flex-wrap gap-1">
                      <!-- 机器认的也能当默认头像（DR-41）：头像只管展示，
                           与「这张脸是不是他」是两件事，不必先确认再选 -->
                      <el-button
                        v-if="!isDefaultAvatar(sample)"
                        size="small"
                        text
                        :loading="settingFaceCode === sample.faceCode"
                        :aria-label="`把这张脸设为默认头像（${sample.shotYear || '未知年份'}）`"
                        @click="setDefaultAvatar(sample)"
                      >
                        设为默认
                      </el-button>
                      <span
                        v-else
                        class="inline-flex items-center gap-0.5 px-1 text-caption text-brand-ink"
                      >
                        <span aria-hidden="true">★</span>默认
                      </span>
                      <el-button
                        size="small"
                        text
                        :loading="confirmingFaceCode === sample.faceCode"
                        :aria-label="`确认这张脸确实是他（${sample.shotYear || '未知年份'}）`"
                        @click="confirmAutoSample(sample)"
                      >
                        确认
                      </el-button>
                      <el-button
                        size="small"
                        text
                        type="danger"
                        :loading="removingFaceCode === sample.faceCode"
                        :aria-label="`把这张脸的归属移除（${sample.shotYear || '未知年份'}）`"
                        @click="removeAutoSample(sample)"
                      >
                        <X class="mr-1 h-3 w-3" aria-hidden="true" />移除
                      </el-button>
                    </div>
                  </li>
                </ul>
                <p v-else class="pb-hint mt-2">
                  没有自动归属的样本 —— 要么还没跑匹配，要么全部被你确认/否决过了。
                </p>
                <p v-if="facesAuto.length" class="pb-hint mt-2">
                  虚线框 = 机器认的、<b>还没你确认过</b>。它们同时出现在
                  「待确认 › 我不同意」列表里；发现认错可以就地移除 ——
                  移除后这张脸<b>回到待确认队列</b>，他的质心会立刻重算。
                </p>
              </section>

              <p v-if="persons.facesTruncated" class="rounded-btn bg-info-soft px-3 py-2 text-caption text-info-ink">
                样本较多，这里只列了前 400 张；完整名单请用「照片流 → 筛选该人物」查看。
              </p>

              <p class="pb-hint">
                当前待确认队列剩余 {{ review.pendingCount }} 条 ·
                我不同意 {{ review.disputedCount }} 条。
                <RouterLink class="ml-1 text-brand-ink hover:underline" to="/review">
                  去处理
                </RouterLink>
              </p>
            </template>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>

    <!-- ============ 编辑资料抽屉（与 P-04 共用 PersonForm） ============ -->
    <el-drawer v-model="editVisible" title="编辑资料" size="480px">
      <PersonForm
        v-if="person"
        :person="person"
        :families="families"
        :submitting="submitting"
        @cancel="editVisible = false"
        @submit="submitEdit"
      />
    </el-drawer>

    <!-- ============ 选择默认头像（DR-41） ============ -->
    <!-- 与 Tab2 的「设为默认」是同一个动作的两个入口：Tab2 就地选，
         头部按钮给一个「先看全部样本再决定」的位置（样本可能上百张，
         Tab2 要滚动才看得到）。逻辑只有 applyAvatar 一处。 -->
    <AvatarPicker
      v-model="avatarPickerVisible"
      :person="person"
      :samples="avatarSamples"
      :truncated="persons.facesTruncated"
      :submitting="avatarSubmitting"
      @select="applyAvatar"
      @clear="applyAvatar('')"
    />

    <!-- ============ 撤销确认 ============ -->
    <el-dialog v-model="undoVisible" title="撤销这次合并 / 拆分？" width="520px">
      <div v-if="revertible" class="space-y-3">
        <p class="text-body text-ink-sub">{{ describeRevertible() }}。</p>
        <ul class="space-y-1 text-caption text-ink-sub">
          <li>· 人脸归属（<code class="font-mono">pb_face</code>）按日志里的原值<b>逐位</b>还原</li>
          <li>· 照片-人员关联（<code class="font-mono">pb_photo_person</code>）双向还原</li>
          <li>· <b>双方质心立即重算</b>（否则旧质心会继续参与匹配）</li>
          <li>· 原记录回填 <code class="font-mono">revertedByLogCode</code>，不能再撤第二次</li>
        </ul>
        <p class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink">
          <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
          <b>撤销只能回到合并 / 拆分前的归属，质心的原值回不来</b> ——
          重算后的质心反映的是「现在剩下的确认样本」，
          与当初那一份不逐位相同（样本搬走了，不可逆）。
        </p>
      </div>
      <template #footer>
        <el-button @click="undoVisible = false">取消</el-button>
        <el-button type="danger" :loading="undoSubmitting" @click="confirmUndo">
          确认撤销
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 操作历史 ============ -->
    <el-drawer v-model="historyVisible" title="操作历史（这张脸当初怎么被认成这个人的）" size="560px">
      <div class="space-y-3">
        <p v-if="historyLoading" class="pb-hint">正在读取…</p>
        <p v-else-if="!historyRows.length" class="pb-hint">
          还没有任何纠错记录。这个人的脸要么从没被动过，要么全在别的档案里 ——
          那正是「照片详情 → 人脸框 → ✗ 不是他」要记的东西。
        </p>
        <ol v-else class="space-y-2">
          <li v-for="row in historyRows" :key="row.logCode" class="pb-card p-3">
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded-btn bg-surface px-2 py-0.5 text-caption text-ink-sub">
                {{ opLabel(row.opType) }}
              </span>
              <span class="text-caption text-ink-weak">{{ row.opYMDHMS }}</span>
              <span
                v-if="Number(row.isRevertible) === 1"
                class="rounded-btn bg-info-soft px-2 py-0.5 text-caption text-info-ink"
              >
                可撤销
              </span>
              <span
                v-if="row.revertedByLogCode"
                class="rounded-btn bg-surface px-2 py-0.5 text-caption text-ink-weak"
                :title="`已被 ${row.revertedByLogCode} 撤销`"
              >
                已撤销
              </span>
            </div>
            <p class="mt-1.5 text-caption text-ink-sub">
              <template v-if="row.fromPersonCode">
                {{ row.fromDisplayName || row.fromPersonCode }}
              </template>
              <template v-else>（未归属）</template>
              <span class="mx-1 text-ink-weak">→</span>
              <template v-if="row.toPersonCode">
                {{ row.toDisplayName || row.toPersonCode }}
              </template>
              <template v-else>（未归属）</template>
            </p>
            <p class="mt-1 font-mono text-caption text-ink-weak">
              {{ row.logCode }}<template v-if="row.faceCode"> · {{ row.faceCode }}</template>
            </p>
            <p v-if="row.detail" class="mt-1 text-caption text-ink-weak">{{ row.detail }}</p>
          </li>
        </ol>
      </div>
    </el-drawer>

    <!-- ============ 停用（影响面 + 二次确认） ============ -->
    <el-dialog v-model="disableVisible" title="停用这个人？" width="460px">
      <div class="space-y-3">
        <p class="text-body text-ink-sub">停用「<b>{{ person?.displayName }}</b>」会发生：</p>
        <ul v-if="disableImpact" class="space-y-1 text-caption text-ink-sub">
          <li>· <b>{{ disableImpact.centroidCount ?? 0 }}</b> 个年代档质心将被删除（不再参与匹配）</li>
          <li>· <b>{{ disableImpact.faceCount ?? 0 }}</b> 张人脸将退回<b>待确认队列</b></li>
          <li>· 共 {{ disableImpact.photoCount ?? 0 }} 张照片的关联将按剩余人脸情况重新整理</li>
        </ul>
        <p v-else class="pb-hint">正在读取影响面…</p>
        <p
          v-if="(disableImpact?.pendingAfter ?? 0) > (disableImpact?.pendingCount ?? 0)"
          class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
        >
          <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
          待确认队列将从 {{ disableImpact.pendingCount }} 涨到 {{ disableImpact.pendingAfter }}。
        </p>
        <p class="pb-hint">停用<b>可恢复</b>（不提供删除）；恢复后需重新确认人脸才能自动匹配。</p>
      </div>
      <template #footer>
        <el-button @click="disableVisible = false">取消</el-button>
        <el-button
          type="danger"
          :loading="disableSubmitting"
          :disabled="!disableImpact"
          @click="confirmDisable"
        >
          确认停用
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>