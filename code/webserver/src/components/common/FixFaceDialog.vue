<!--
  FixFaceDialog —— 改判 / 拆出浮层（设计稿 §五 + DR-16②）
  ------------------------------------------------------------
  **三种 action 共用一个浮层**：改判到某人 / 置为未知 / 标记为陌生人。
  写成三个弹窗的话，「改判」与「否决」的口径、文案、二次确认一定会漂移，
  而这三个动作的后果等级完全不同 —— 用户需要能对照着看。

  为什么是浮层而不是「直接改」
  --------------------------
  「✗ 不是他」只有一次点击（P0-6）。第二次点击要能承载**判断依据**：
  候选是谁、相似度多少、要不要连这张照片里其他脸一起改。所以第一步直接改、
  第二步再问「改成谁」，而不是第一步就把候选列表糊在照片上。

  不可逆动作的复述 + 二次确认（P0-3 / P0-7）
  ------------------------------------------
  「标记为陌生人」是**永久排除**（不再进队列、不参与聚类、不会被自动归属），
  只能靠手动改判回来。所以点主按钮之后**还有一道**确认，且确认框里必须
  写清是哪几张脸、来自哪张照片 —— 只写「确定吗？」等于让用户盲确认。

  mode="split" —— 「它属于谁」（本轮新增）
  --------------------------------------
  拆分**不是**「否决」的另一种叫法：
    · 否决（fix unknown）= 丢回待确认队列，**不记「谁本来属于谁」**；
    · 拆分（POST /review/split）= 真的换一个主人，落一条 isRevertible=1 的
      SPLIT 日志，撤销能把双方归属和质心一起还原。
  所以它必须走独立端点、给独立的动词与文案，而不是复用 fix 打一个 flag。

  拆分时的人选来自**全库人物**（props.persons），不是 props.candidates：
  「这张脸其实是我表弟」—— 表弟多半**没有质心**（从没被人工确认过），
  压根不会出现在相似度候选里。只列候选等于让用户在自己知道答案的
  场景里找不到答案。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { Search, Split, TriangleAlert, UserPlus, X } from 'lucide-vue-next'
import PersonForm from '@/components/common/PersonForm.vue'
import { faceUrl } from '@/api/static'
import { faceStateOf, faceStrokeStyle, similarityText } from '@/utils/faceState'
import { baseName } from '@/utils/format'
import { relationLabelOf, usePersonsStore } from '@/store/persons'

/**
 * ⚠️ 变量名不能叫 `persons`：本组件的 props 里就有一个 `persons`
 *    （mode="split" 的全库人物）。同名的话 `props.persons` 与 store
 *    在模板里长得一模一样，改一行就可能取错。
 */
const personsStore = usePersonsStore()

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 被改判的脸（1 张 = 单张改判；>1 = 整张照片或整簇批量） */
  faces: { type: Array, default: () => [] },
  /** 同一张照片里的其它脸（供「这张照片的其他脸也否决」勾选） */
  siblings: { type: Array, default: () => [] },
  /** 候选人物 [{personCode, displayName, avatarFaceCode, similarity, relation}] */
  candidates: { type: Array, default: () => [] },
  /** 全库人物（**仅 mode="split" 用**）：「它属于谁」的答案常常不在候选里 */
  persons: { type: Array, default: () => [] },
  /**
   * 全库人物搜索（可选注入，形如 `(keyword) => Promise<Person[]>`）。
   *
   * ⚠️ 为什么必须有它：picker 在改判模式下只是**相似度前 5 名**的候选。
   *    用户在这 5 条里搜名字，几乎必然搜不到 —— 那不是搜索坏了，是搜索范围错了。
   *    传进来就查全库（GET /persons?keyword=）；不传则退化为纯前端过滤。
   */
  searchFn: { type: Function, default: null },
  /** 打开时的默认动作 */
  mode: { type: String, default: 'assign' },
  /** 对话框形态：fix = 改判；split = 拆出（多一段「从谁拆到谁」的复述） */
  variant: { type: String, default: 'fix' },
  /** 灰区阈值（候选落在 T_low–T_high 时标橙提示） */
  thresholdLow: { type: Number, default: 0.3 },
  thresholdHigh: { type: Number, default: 0.45 },
  loading: { type: Boolean, default: false },
  /** 照片文件名（复述用，来自 /api/photos/{code}） */
  photoLabel: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'submit', 'create-person'])

const isSplit = computed(() => props.variant === 'split')

/** 三条出路。拆分时「置为未知」的措辞要改成「退回待确认」，否则与否决混为一谈 */
const ACTIONS = computed(() => {
  if (!isSplit.value) return [
    { key: 'assign', label: '改判到某人', hint: '这张脸应该是库里已有的人' },
    { key: 'unknown', label: '置为未知', hint: '退回待确认队列，以后再认' },
    { key: 'stranger', label: '标记为陌生人', hint: '永久排除，不再出现在任何队列' },
  ]
  return [
    { key: 'assign', label: '它属于库里的人', hint: '从已有档案里选一个' },
    { key: 'unknown', label: '还说不准，先退回待确认', hint: '不记归属，等下次再看' },
    { key: 'stranger', label: '它根本不是库里的人', hint: '永久排除，不再出现在任何队列' },
  ]
})

const action = ref(props.mode)
const pickedPerson = ref('')
/** 已选中的那个人（选中那一刻的对象，见下方 picked 的说明） */
const pickedItem = ref(null)
/**
 * 裁剪图**可能不在磁盘上**（`/api/face` 对查不到的图回 404）——
 * 浮层最上方挂一个浏览器的破图图标，比什么都不显示更难解释，所以回退成占位块。
 */
const brokenFaces = ref({})
const keyword = ref('')
const alsoSiblings = ref(false)
const confirmStranger = ref(false)
const showNewPerson = ref(false)

/** 服务端搜索的结果（全库）与状态 */
const searchHits = ref([])
const searching = ref(false)
/** 本次关键词是否已经问过服务端（决定"没找到"该说谁的话） */
const searched = ref(false)
let searchTimer = null
let searchSeq = 0

watch(
  () => props.modelValue,
  (open) => {
    clearTimeout(searchTimer)
    searchSeq += 1
    searchHits.value = []
    searching.value = false
    searched.value = false
    if (!open) return
    // 「新建人物并归属」里的家庭组下拉要有内容（store 失败时静默降级成空列表，
    // 不会拦住主流程）；建组动作本身由 PersonForm 直接调 store，不用管刷新
    personsStore.fetchFamilies()
    action.value = props.mode
    pickedPerson.value = ''
    pickedItem.value = null
    keyword.value = ''
    alsoSiblings.value = false
    confirmStranger.value = false
    showNewPerson.value = false
    brokenFaces.value = {}
  },
)

const single = computed(() => (props.faces || []).length === 1)
const primary = computed(() => props.faces?.[0] || null)
const primaryMeta = computed(() => faceStateOf(primary.value))

/**
 * 预览用的人脸 = 本次要改判的那几张（**不含**勾选「其他脸一起改判」后追加的兄弟脸）：
 * 这块预览回答的是「我点的是哪张脸」，不是「最后会改到几张」——
 * 后者由复述文案里的 targetFaces.length 负责。
 */
const previewFaces = computed(() => (props.faces || []).filter((face) => face?.faceCode))

function markFaceBroken(faceCode) {
  if (!faceCode) return
  brokenFaces.value = { ...brokenFaces.value, [faceCode]: true }
}

/** 实际会被改判的那些脸（勾了「其他脸也否决」就把同照片的其它脸带上） */
const targetFaces = computed(() => {
  const list = props.faces || []
  if (!single.value || !alsoSiblings.value) return list
  const codes = new Set(list.map((f) => f.faceCode))
  return [...list, ...(props.siblings || []).filter((f) => !codes.has(f.faceCode))]
})

const targetFaceCodes = computed(() => targetFaces.value.map((f) => f.faceCode))

/**
 * 「他属于谁」的可选列表。
 * 拆分走**全库人物**；改判走相似度候选（顺序即相似度降序，附带相似度与关系提示）。
 */
const picker = computed(() => {
  if (!isSplit.value) return props.candidates || []
  const self = primary.value?.personCode || ''
  return (props.persons || []).filter((p) => p.personCode !== self)
})

/**
 * 列表内容：**有关键词 = 纯搜索结果；没关键词 = 相似度候选**。
 *
 * ⚠️⚠️ 这里原来写成「候选在前、搜索结果补在后面」，是错的：
 *   用户输入 luwen 之后看到的仍然是一张**按相似度降序**的候选表
 *   （Steven Lian 0.15 / ZhongWen Lian 0.10 / BaoRui Zhang 0.04 …），
 *   搜索框像是完全没起作用 —— 而它明明已经搜到 8 个人了。
 *   「搜索」的意义就是**换掉那张表**，不是往表里加几行。
 *
 * 但命中的人如果本来就在候选里，要把他的 similarity / relation **借过来**：
 *   「我搜到的这个人，机器也认，而且只有 0.15」是这条列表里最有用的一句话，
 *   抹掉它等于把搜索结果降级成一张通讯录。
 */
const options = computed(() => {
  const text = String(keyword.value || '').trim()
  if (!text) return picker.value

  const self = isSplit.value ? primary.value?.personCode || '' : ''
  const byCode = new Map(picker.value.map((c) => [c.personCode, c]))
  const out = []
  for (const one of searchHits.value) {
    if (!one?.personCode) continue
    // 拆分不能「拆给自己」：picker 已经剔掉本人，搜索结果也要跟同一把尺
    if (self && one.personCode === self) continue
    const hit = byCode.get(one.personCode)
    out.push(
      hit
        ? { ...one, similarity: hit.similarity, relation: one.relation || hit.relation }
        : { similarity: null, ...one },
    )
  }
  return out
})

const filteredCandidates = computed(() => {
  const text = String(keyword.value || '').trim()
  if (!text) return picker.value

  // 服务端已经按关键词搜过全库：结果即列表，**不要再按候选池过滤一遍**，
  // 否则「机器没排进前 5 但用户认识」的人会被二次筛掉，搜索又白做了。
  if (searched.value) return options.value

  // 没注入 searchFn 时的兜底：只在候选池里筛，但**要说清**筛的是候选
  // 关系两个形态都进匹配串：库里是 `sibling`，用户想敲的是「兄弟」
  const low = text.toLowerCase()
  return picker.value.filter((c) =>
    [c.displayName, c.familyName, c.personCode, c.relation, relationLabelOf(c.relation)].some(
      (field) =>
        String(field || '')
          .toLowerCase()
          .includes(low),
    ),
  )
})

/** 「没找到」与「候选池本来就是空的」是两回事，文案必须分开 */
const noMatchByKeyword = computed(() => {
  const text = String(keyword.value || '').trim()
  return Boolean(text) && !searching.value && !filteredCandidates.value.length
})
const emptyPicker = computed(() => !picker.value.length && !String(keyword.value || '').trim())

/**
 * 搜索请求：防抖 200ms + **序号丢弃过期响应**。
 * 不丢弃的话慢的那次会覆盖快的那次 —— 输入「王小明」时结果会来回跳。
 */
async function runSearch(text) {
  if (typeof props.searchFn !== 'function') {
    searched.value = false
    return
  }
  const seq = ++searchSeq
  searching.value = true
  try {
    const list = await props.searchFn(text)
    if (seq !== searchSeq) return
    searchHits.value = Array.isArray(list) ? list : []
    searched.value = true
  } catch {
    if (seq !== searchSeq) return
    searchHits.value = []
    searched.value = false
  } finally {
    if (seq === searchSeq) searching.value = false
  }
}

watch(keyword, (value) => {
  clearTimeout(searchTimer)
  const text = String(value || '').trim()
  searchSeq += 1
  if (!text) {
    searchHits.value = []
    searching.value = false
    searched.value = false
    return
  }
  searchTimer = setTimeout(() => runSearch(text), 200)
})

function clearKeyword() {
  keyword.value = ''
}

function inGreyZone(similarity) {
  if (similarity === null || similarity === undefined) return false
  const num = Number(similarity)
  return num >= props.thresholdLow && num <= props.thresholdHigh
}

const canSubmit = computed(() => {
  if (props.loading) return false
  if (action.value === 'assign') return Boolean(pickedPerson.value)
  if (action.value === 'stranger') return confirmStranger.value
  return true
})

/**
 * 已选中的那个人。
 * ⚠️ 不能只从 options 里 find：搜索结果会在用户点完主按钮前就可能被下一次输入顶掉，
 *    那时 submitLabel 会退化成「改判到某人」—— 用户按下按钮前才发现刚才选的人不见了。
 * 所以把选中那一刻的对象存下来。
 */
const picked = computed(() => {
  const code = pickedPerson.value
  if (!code) return null
  return pickedItem.value?.personCode === code
    ? pickedItem.value
    : options.value.find((c) => c.personCode === code) || null
})
const pickedName = computed(() => picked.value?.displayName || '')

/** 主按钮的动词必须与动作一致（设计稿：动词按钮） */
const submitLabel = computed(() => {
  if (action.value === 'assign') {
    if (!pickedName.value) return isSplit.value ? '选一个去处' : '改判到某人'
    return isSplit.value ? `拆给「${pickedName.value}」` : `改判为「${pickedName.value}」`
  }
  if (action.value === 'unknown') return isSplit.value ? '退回待确认队列' : '置为未知'
  return '标记为陌生人'
})

/** 复述用：这张脸当前被认成谁 —— 改判前必须让用户看到「原来是谁」 */
const currentOwner = computed(() => {
  const name = String(primary.value?.displayName || '').trim()
  if (name) return name
  if (primaryMeta.value.key === 'pending') return '未归属（待确认队列）'
  if (primaryMeta.value.key === 'stranger') return '陌生人'
  return primary.value?.personCode || '未归属（待确认队列）'
})

/** 拆分复述用：拆出方与去处各自的规模（人/照片），让人看得见这一步的分量 */
const sourceStats = computed(() => {
  const code = primary.value?.personCode || ''
  return (props.persons || []).find((p) => p.personCode === code) || null
})

const sourceLabel = computed(() => baseName(props.photoLabel) || '这张照片')

function pick(option) {
  const code = typeof option === 'string' ? option : option?.personCode
  if (pickedPerson.value === code) {
    pickedPerson.value = ''
    pickedItem.value = null
    return
  }
  pickedPerson.value = code
  pickedItem.value = typeof option === 'string' ? null : option
}

function submit() {
  if (!canSubmit.value) return
  emit('submit', {
    variant: props.variant,
    action: action.value,
    faceCodes: targetFaceCodes.value,
    personCode: action.value === 'assign' ? pickedPerson.value : undefined,
  })
}

/**
 * 新建人物：建完**自动把当前这张脸归属给他**，不用建完再回去点一次
 * （设计稿 §4.8 联系人维护第 ⑧ 条）。
 * onDone 由父组件在接口成功后调用（父组件负责关 loading）。
 */
function submitNewPerson(form) {
  emit(
    'create-person',
    { form, faceCodes: targetFaceCodes.value, photoCode: primary.value?.photoCode || '' },
    () => {
      showNewPerson.value = false
    },
  )
}

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="isSplit ? '拆出这张脸 —— 它属于谁？' : '改判人脸'"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <!-- 人脸预览**必须排在最上方**：用户是「这张脸认错了」进来的，
         浮层里第一件该看见的东西就是那张脸。先摆候选列表、让人读完相似度
         再回头核对脸，等于让人凭记忆比对 —— 队列里翻过几屏之后记忆并不可靠。
         预览用**带状态描边的裁剪图**（与 FaceBox / AvatarPicker 同一套四态编码）：
         「待确认」与「机器认的」在缩略图上就得能分辨，否则用户会把
         一张「机器刚认的」脸当成「还没归属的」脸来改判。 -->
    <section
      v-if="previewFaces.length"
      class="mb-3 rounded-btn border border-line bg-card p-3"
      aria-label="待改判的人脸预览"
    >
      <div class="flex items-baseline justify-between gap-2">
        <span class="text-body font-medium text-ink">
          {{ isSplit ? '要拆出的脸' : '要改判的脸' }}
        </span>
        <span class="pb-hint">
          <span aria-hidden="true">{{ primaryMeta.icon }}</span> {{ primaryMeta.label }}
          <template v-if="previewFaces.length > 1"> · 共 {{ previewFaces.length }} 张</template>
        </span>
      </div>
      <div class="mt-2 flex flex-wrap items-start gap-2">
        <template v-for="face in previewFaces" :key="face.faceCode">
          <img
            v-if="!brokenFaces[face.faceCode]"
            :src="faceUrl(face.faceCode)"
            class="shrink-0 rounded-thumb border object-cover"
            :class="[single ? 'h-32 w-32' : 'h-16 w-16', faceStateOf(face).borderClass]"
            :style="faceStrokeStyle(faceStateOf(face))"
            :alt="`要改判的人脸裁剪图（${face.faceCode}）`"
            loading="lazy"
            @error="markFaceBroken(face.faceCode)"
          />
          <span
            v-else
            class="inline-flex shrink-0 items-center justify-center rounded-thumb border bg-surface px-1 text-center text-caption text-ink-weak"
            :class="[single ? 'h-32 w-32' : 'h-16 w-16', faceStateOf(face).borderClass]"
            :style="faceStrokeStyle(faceStateOf(face))"
            :title="`裁剪图暂不可用（${face.faceCode}）`"
          >
            裁剪图暂不可用
          </span>
        </template>
      </div>
    </section>

    <!-- 复述：改的是哪几张、来自哪张照片、现在被认成谁 -->
    <div class="rounded-btn bg-surface p-3 text-body">
      <p class="text-ink">
        <template v-if="isSplit">将拆出 <b class="tabular-nums">{{ targetFaces.length }}</b> 张人脸<template
          v-if="photoLabel"
        >（{{ photoLabel }}）</template></template>
        <template v-else
          >将改判 <b class="tabular-nums">{{ targetFaces.length }}</b> 张人脸<template
            v-if="photoLabel"
          >（{{ photoLabel }}）</template></template
        >
      </p>
      <p v-if="primary" class="mt-1 text-caption text-ink-sub">
        <span class="mr-1">{{ primaryMeta.icon }} {{ primaryMeta.label }}</span>
        当前被认成 <b class="text-ink">{{ currentOwner }}</b>
        <template v-if="primary.similarity !== null && primary.similarity !== undefined">
          （相似度 {{ similarityText(primary.similarity) }}）
        </template>
      </p>

      <!-- 拆分的复述要比「改判」多一层：从谁挪到谁，各自的规模看得见 -->
      <div
        v-if="isSplit && action === 'assign' && picked"
        class="mt-2 flex items-stretch gap-2 text-caption"
      >
        <div class="flex-1 rounded border border-line bg-card px-2 py-1.5">
          <div class="text-ink-weak">拆出方</div>
          <div class="truncate text-body text-ink">{{ currentOwner }}</div>
          <div v-if="sourceStats" class="tabular-nums text-ink-weak">
            拆后 {{ Math.max(0, (sourceStats.photoCount || 0) - 1) }} 张照片
          </div>
          <div v-else class="text-ink-weak">（当前无人）</div>
        </div>
        <div class="flex items-center text-ink-weak" aria-hidden="true">→</div>
        <div class="flex-1 rounded border border-line bg-card px-2 py-1.5">
          <div class="text-ink-weak">去处</div>
          <div class="truncate text-body text-ink">{{ picked.displayName }}</div>
          <div class="tabular-nums text-ink-weak">
            拆后 {{ picked.photoCount ?? 0 }} 张照片
          </div>
        </div>
      </div>

      <p
        v-if="isSplit && action === 'assign' && picked"
        class="mt-2 text-caption text-info-ink"
      >
        拆分是<b>可撤销</b>的：会落一条 SPLIT 日志，撤销能把双方的归属和质心一起还原
        （普通确认做不到 —— 它不记「谁本来属于谁」）。
      </p>
    </div>

    <!-- 三种动作：并排可比，用户需要对照着看后果等级 -->
    <ul class="mt-4 space-y-2" role="radiogroup" :aria-label="isSplit ? '它属于谁' : '改判方式'">
      <li v-for="item in ACTIONS" :key="item.key">
        <button
          type="button"
          role="radio"
          :aria-checked="action === item.key"
          class="flex w-full items-start gap-2 rounded-btn border px-3 py-2 text-left transition-colors duration-150"
          :class="
            action === item.key
              ? 'border-brand bg-brand-soft'
              : 'border-line bg-card hover:bg-surface'
          "
          @click="action = item.key"
        >
          <span
            class="mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border"
            :class="action === item.key ? 'border-brand bg-brand' : 'border-line-strong'"
            aria-hidden="true"
          >
            <span v-if="action === item.key" class="h-1.5 w-1.5 rounded-full bg-card" />
          </span>
          <span class="min-w-0">
            <span class="block text-body text-ink">{{ item.label }}</span>
            <span class="block text-caption text-ink-weak">{{ item.hint }}</span>
          </span>
        </button>
      </li>
    </ul>

    <!-- ① 它属于谁 / 改判到某人 -->
    <section v-if="action === 'assign'" class="mt-4 space-y-2" aria-label="选择改判给谁">
      <!-- 搜索是这个浮层里**唯一能突破候选前 5 名**的入口（表弟、外婆压根没有质心，
           不会出现在相似度候选里），所以它要比下面的列表更显眼：
           带标题的独立区块 + 大号输入框，而不是一行和列表同级的小输入框。 -->
      <div class="rounded-btn border border-line bg-surface p-3">
        <div class="flex items-baseline justify-between gap-2">
          <label class="text-body font-medium text-ink" for="fx-person-search">搜索人物</label>
          <span class="pb-hint">
            {{ isSplit ? '搜全库' : '搜全库，不受相似度前 5 名限制' }}
          </span>
        </div>
        <el-input
          id="fx-person-search"
          v-model="keyword"
          class="mt-2"
          size="large"
          placeholder="例：王小明 / wangxiaoming / wxm"
          clearable
          aria-label="搜索候选人物"
        >
          <template #prefix>
            <Search class="h-4 w-4 text-ink-weak" aria-hidden="true" />
          </template>
          <template v-if="searching" #suffix>
            <span class="text-caption text-ink-weak">搜索中…</span>
          </template>
        </el-input>
      </div>

      <!-- 空态分三种，不能共用一句话：候选池本来就空 / 搜了没搜到 / 有结果 -->
      <p
        v-if="emptyPicker"
        class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        <template v-if="isSplit">
          库里没有别的档案可选。下面的「新建人物」可以直接建一个并归属
          —— 你多半已经知道这是谁了。
        </template>
        <template v-else>
          这张脸没有可比对的人物档案 —— 库里还没有任何人的质心（质心只由
          <b>人工确认</b>的样本生成，还没确认过就一个都没有）。
          <b>但你可以直接搜索全库姓名</b>：没进过相似候选的人（表弟、外婆…）照样搜得到。
          确实没有的话，用下面的「新建人物」建一个档案并归属，之后同类照片就能自动比对了。
        </template>
      </p>

      <p
        v-else-if="noMatchByKeyword"
        class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        {{ searched ? '全库里' : '候选里' }}没有匹配「<b class="break-all">{{
          String(keyword || '').trim()
        }}</b
        >」的人物。检查一下拼写，或只输姓氏试试；搜索支持汉字、全拼与拼音首字母
        （王小明 → wangxiaoming / wxm）。确认这个人确实还没建档的话，用下面的「新建人物」建一个并归属。
        <el-button
          link
          size="small"
          class="ml-1 !h-auto !p-0 align-baseline"
          @click="clearKeyword"
        >
          清空搜索
        </el-button>
      </p>

      <template v-else>
        <p v-if="String(keyword || '').trim()" class="pb-hint">
          <template v-if="searched">
            「{{ String(keyword).trim() }}」在全库搜到 <b>{{ filteredCandidates.length }}</b> 人
            —— 这里<b>只列搜索结果</b>，相似度候选不再混进来。带相似度的是恰好也在机器候选里的那个。
          </template>
          <template v-else>
            在相似候选里筛到 <b>{{ filteredCandidates.length }}</b> 人（没接全库搜索，只能筛候选）。
          </template>
        </p>
        <ul
          class="max-h-64 divide-y divide-line overflow-y-auto rounded-btn border border-line"
        >
          <li v-for="candidate in filteredCandidates" :key="candidate.personCode">
            <button
              type="button"
              class="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface"
              :aria-pressed="pickedPerson === candidate.personCode"
              @click="pick(candidate)"
            >
              <img
                v-if="candidate.avatarFaceCode"
                :src="faceUrl(candidate.avatarFaceCode)"
                class="h-8 w-8 shrink-0 rounded-full border border-line object-cover"
                alt=""
              />
              <span
                v-else
                class="h-8 w-8 shrink-0 rounded-full border border-line bg-surface"
                aria-hidden="true"
              />
              <span class="min-w-0 flex-1">
                <span class="block truncate text-body text-ink">{{ candidate.displayName }}</span>
                <span
                  v-if="candidate.relation"
                  class="block truncate text-caption text-warning-ink"
                >
                  {{ relationLabelOf(candidate.relation) }}，注意区分
                </span>
              </span>
              <!-- 改判看相似度；拆分看规模（相似度对「它属于谁」这个问题没有意义） -->
              <span
                v-if="!isSplit"
                class="w-12 text-right text-body tabular-nums"
                :class="inGreyZone(candidate.similarity) ? 'text-warning-ink' : 'text-ink-sub'"
              >
                {{ similarityText(candidate.similarity) }}
              </span>
              <span
                v-else
                class="w-14 text-right text-caption tabular-nums text-ink-weak"
              >
                {{ candidate.photoCount ?? 0 }} 张照片
              </span>
              <span
                class="w-12 text-right text-caption"
                :class="pickedPerson === candidate.personCode ? 'text-brand-ink' : 'text-ink-weak'"
              >
                {{ pickedPerson === candidate.personCode ? '已选' : '选TA' }}
              </span>
            </button>
          </li>
        </ul>
      </template>

      <el-button size="small" @click="showNewPerson = true">
        <UserPlus class="mr-1 h-3.5 w-3.5" aria-hidden="true" />
        {{ isSplit ? '新建一个档案并拆给他' : '新建人物并归属' }}
      </el-button>
    </section>

    <!-- ② 置为未知 / 退回待确认 -->
    <section v-else-if="action === 'unknown'" class="mt-4">
      <p class="rounded-btn bg-info-soft px-3 py-2 text-caption text-info-ink">
        <template v-if="isSplit">
          退回「待确认」队列后角标 +{{ targetFaces.length }}，但<b>不会</b>记下它原来属于谁
          —— 下次想找回这条线索就找不回来了。如果只是暂时拿不准，
          「改判到某人」里那几条候选也值得先看看。
        </template>
        <template v-else>
          置为未知后，这些脸会退回「待确认」队列（<b>不会</b>被自动归属），
          角标 +{{ targetFaces.length }}。也可以随时在「待确认」里重新认。
        </template>
      </p>
    </section>

    <!-- ③ 标记陌生人（不可逆，**必须二次确认**） -->
    <section v-else class="mt-4 space-y-2">
      <p class="flex items-start gap-2 rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink">
        <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>
          标记为陌生人后，这 {{ targetFaces.length }} 张脸会被<b>永久排除</b>：
          不再出现在待确认队列、不参与聚类、也不会再被自动归属。只有手动改判才回得来。
        </span>
      </p>
      <el-checkbox v-model="confirmStranger">
        我确认：「{{ sourceLabel }}」里的 {{ targetFaces.length }} 张脸不是库里的人
      </el-checkbox>
    </section>

    <!-- 「就这张照片的其他脸也否决」—— 逐张改判之外的等价批量入口。
         拆分只作用于**这一张**脸：勾它等于把同照片其它脸也一起拆，
         这通常不是用户的意思（他只对某一张脸有把握），所以拆分模式不提供。 -->
    <label
      v-if="single && siblings.length && !isSplit"
      class="mt-4 flex items-start gap-2 rounded-btn border border-line bg-card px-3 py-2"
    >
      <el-checkbox v-model="alsoSiblings" class="mt-0.5" />
      <span class="text-caption text-ink-sub">
        把「{{ sourceLabel }}」里另外 {{ siblings.length }} 张脸一起改判
      </span>
    </label>

    <template #footer>
      <div class="flex items-center justify-between gap-3">
        <p class="pb-hint">
          {{ targetFaces.length > 1 ? `将影响 ${targetFaces.length} 张脸` : '' }}
        </p>
        <div class="flex gap-2">
          <el-button @click="close">取消</el-button>
          <el-button
            :type="action === 'stranger' ? 'danger' : 'primary'"
            :disabled="!canSubmit"
            :loading="loading"
            @click="submit"
          >
            <Split v-if="isSplit" class="mr-1 h-4 w-4" aria-hidden="true" />
            <X v-else-if="action === 'stranger'" class="mr-1 h-4 w-4" aria-hidden="true" />
            {{ submitLabel }}
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>

  <!-- 新建人物：复用唯一表单组件 PersonForm（设计稿 §4.8 硬要求：不得有第二份表单） -->
  <el-dialog
    v-model="showNewPerson"
    :title="isSplit ? '新建一个档案并拆给他' : '新建人物并归属这张脸'"
    width="640px"
    :close-on-click-modal="false"
    append-to-body
  >
    <p class="pb-hint mb-3">
      建完会<b>自动把「{{ sourceLabel }}」里的 {{ targetFaceCodes.length }} 张脸归属给他</b>，
      不用建完再回去点一次确认。生日会直接决定年代档划分，能填就填。
    </p>
    <!-- families 走同一个 store：以前传的是空数组，这个对话框里
         「家庭组」下拉永远是空的（连刚建的组也看不见） -->
    <PersonForm
      :person="null"
      :families="personsStore.families"
      @cancel="showNewPerson.value = false"
      @submit="submitNewPerson"
    />
  </el-dialog>
</template>
