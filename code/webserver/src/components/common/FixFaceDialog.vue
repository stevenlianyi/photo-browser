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
import { faceStateOf, similarityText } from '@/utils/faceState'
import { baseName } from '@/utils/format'

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
const keyword = ref('')
const alsoSiblings = ref(false)
const confirmStranger = ref(false)
const showNewPerson = ref(false)

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    action.value = props.mode
    pickedPerson.value = ''
    keyword.value = ''
    alsoSiblings.value = false
    confirmStranger.value = false
    showNewPerson.value = false
  },
)

const single = computed(() => (props.faces || []).length === 1)
const primary = computed(() => props.faces?.[0] || null)
const primaryMeta = computed(() => faceStateOf(primary.value))

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

const filteredCandidates = computed(() => {
  const text = String(keyword.value || '').trim().toLowerCase()
  if (!text) return picker.value
  return picker.value.filter((c) => String(c.displayName || '').toLowerCase().includes(text))
})

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

const picked = computed(
  () => picker.value.find((c) => c.personCode === pickedPerson.value) || null,
)
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

function pick(personCode) {
  pickedPerson.value = pickedPerson.value === personCode ? '' : personCode
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
      <el-input
        v-model="keyword"
        :placeholder="isSplit ? '搜索人物（他可能没有质心，不在相似度候选里）' : '搜索人物'"
        clearable
        size="small"
        aria-label="搜索候选人物"
      >
        <template #prefix>
          <Search class="h-3.5 w-3.5 text-ink-weak" aria-hidden="true" />
        </template>
      </el-input>

      <p
        v-if="!picker.length"
        class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        <template v-if="isSplit">
          库里没有别的档案可选。下面的「新建人物」可以直接建一个并归属
          —— 你多半已经知道这是谁了。
        </template>
        <template v-else>
          这张脸没有可比对的人物档案 —— 库里还没有任何人的质心（质心只由
          <b>人工确认</b>的样本生成，还没确认过就一个都没有）。
          用下面的「新建人物」建一个档案并归属，之后同类照片就能自动比对了。
        </template>
      </p>

      <ul
        v-else
        class="max-h-64 divide-y divide-line overflow-y-auto rounded-btn border border-line"
      >
        <li v-for="candidate in filteredCandidates" :key="candidate.personCode">
          <button
            type="button"
            class="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface"
            :aria-pressed="pickedPerson === candidate.personCode"
            @click="pick(candidate.personCode)"
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
                {{ candidate.relation }}，注意区分
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
      不用建完再回去点一次确认。生日会直接决定分桶，能填就填。
    </p>
    <PersonForm
      :person="null"
      :families="[]"
      @cancel="showNewPerson.value = false"
      @submit="submitNewPerson"
    />
  </el-dialog>
</template>
