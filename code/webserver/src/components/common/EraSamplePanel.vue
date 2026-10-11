<!--
  EraSamplePanel —— 人物详情 Tab3「年代抽样」（R11 · DR-45 ~ DR-50）
  ----------------------------------------------------------------------
  给它一个人，从「他出现过的每一年」里抽一张照片，按年份排成一条队列；
  可以把这条队列打包成 zip 带走。

  六条不能省的语义
  --------------
  ① **按「年」不是按「年代档」**：Tab1 的时间轴按年代档分组（桶宽随生日
     3/10 年），这里一年一张。两者粒度不同但**年份口径同源**（有效拍摄年，
     含人工修正），所以顶部必须写一句说明 —— 否则用户会以为两个 Tab 打架。
  ② **每次「重新抽」都是新批次**，不覆盖旧的（加权随机本来就允许再抽一次）。
     历史批次用下拉切回去看，**不重抽**。
  ③ **种子输入框 = 下一次要用的种子**（空 = 随机）；当前批次**实际用的**种子
     在下面单独一行显示（可复制）。两者混成一个框的话，用户填了种子再点
     「重新抽」会抽出一模一样的一批，然后以为按钮坏了。
  ④ **四种空状态**（都必须是信息性的，DR-39 的纪律）：
     没抽过 / 他名下没有照片 / 有照片但一张都没有可用年份 / 年份区间筛掉了。
     只写「暂无数据」会让用户分不清「没做」与「真没有」。
  ⑤ **队列可键盘走**：`←`/`→` 在上一年/下一年之间移动焦点（`Enter` 打开），
     但要照抄 ReviewView 的三条忽略纪律 —— **面板不是当前 Tab 时不响应**
     （el-tab-pane 默认把非活动面板也渲染出来，不判断就会在 Tab1 上抢按键）。
  ⑥ **点卡片进照片详情必须声明来源与作用域**：否则详情页的「返回」会回照片流、
     左右箭头会翻到全库（DR-31 的教训）。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import { ChevronLeft, ChevronRight, Dices, Download, ImageOff } from 'lucide-vue-next'
import { buildEraSampleZip, eraSampleZipUrl } from '@/api/eraSample'
import { formatCount, formatFileSize } from '@/utils/format'
import { currentSource, photoDetailLink, scopeParam } from '@/utils/photoReturn'

const props = defineProps({
  personCode: { type: String, required: true },
  displayName: { type: String, default: '' },
  /** 该人的照片总数（三种空状态的分母；0 与「没有可用年份」是两回事） */
  photoTotal: { type: Number, default: 0 },
  /** 当前展示的批次 `{ batch, items }`；**null = 从没抽过**（与空队列不同） */
  sample: { type: Object, default: null },
  /** 历史批次（不含明细），切批次用 */
  batches: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
  /** 本面板是不是当前可见的 Tab（决定要不要响应键盘，见 ⑤） */
  active: { type: Boolean, default: false },
})

const emit = defineEmits(['create', 'select', 'refresh'])

const route = useRoute()

// ============================================================
// 抽签参数（默认值与后端 basicSettings 一致）
// ============================================================

const form = reactive({
  yearFrom: '',
  yearTo: '',
  mode: 'weighted',
  seed: '',
  includeNoYear: false,
  onlyConfirmed: false,
  excludeDuplicate: true,
})

const MODES = [
  { value: 'weighted', label: '加权（优先像他、清楚的）' },
  { value: 'uniform', label: '均匀（完全等概率）' },
]

/**
 * 批次 -> 表单回填（**不回填 seed**，见 ③）。
 *
 * 只在批次切换时同步，所以用户正在改的中间状态不会被冲掉
 * （改批次只可能是「刚生成」或「切历史」，两者都该回填）。
 */
watch(
  () => props.sample?.batch,
  (batch) => {
    if (!batch) return
    form.yearFrom = batch.yearFrom ?? ''
    form.yearTo = batch.yearTo ?? ''
    form.mode = batch.mode || 'weighted'
    form.includeNoYear = Boolean(batch.includeNoYear)
    form.onlyConfirmed = Boolean(batch.onlyConfirmed)
    form.excludeDuplicate = batch.excludeDuplicate !== false
  },
  { immediate: true },
)

const batch = computed(() => props.sample?.batch || null)
const items = computed(() => props.sample?.items || [])

function submit() {
  emit('create', {
    yearFrom: form.yearFrom === '' ? null : form.yearFrom,
    yearTo: form.yearTo === '' ? null : form.yearTo,
    mode: form.mode,
    seed: String(form.seed || '').trim(),
    includeNoYear: form.includeNoYear,
    onlyConfirmed: form.onlyConfirmed,
    excludeDuplicate: form.excludeDuplicate,
  })
}

/** 骰子：给一个随机种子填进输入框（下一次生成就会用它，因而可复现） */
function rollSeed() {
  form.seed = String(Math.floor(Math.random() * 1e12))
}

// ============================================================
// 空状态（四种，文案必须能分辨）
// ============================================================

const emptyState = computed(() => {
  if (!batch.value) {
    return {
      title: '还没有抽过',
      hint: '上面的「生成抽样」会按年抽一张照片，排成一条从早到晚的队列。'
        + '抽签参数可以随时改，每次生成都是一个新批次（旧批次在下面切换）。',
    }
  }
  if (Number(props.photoTotal) === 0) {
    return {
      title: '他名下还没有照片',
      hint: '先在待确认队列里把照片里的人认给他，这里才会有内容。',
    }
  }
  const candidates = Number(batch.value.candidatePhotoCount) || 0
  if (candidates === 0) {
    return {
      title: `他名下有 ${formatCount(props.photoTotal)} 张照片，但没有可用的拍摄年份`,
      hint: '年份来自 EXIF / 文件名 / 文件时间，三者都读不出来的照片无法按年抽样。'
        + '确实有年份、但机器读错了的（老照片翻拍件），'
        + '可以在照片详情页用「修正年代」人工指定，改完这里就能抽到它。',
    }
  }
  return {
    title: `当前条件下这一年段没有抽到照片`,
    hint: `他有 ${formatCount(batch.value.candidateYearCount)} 个年份有照片，`
      + '多半是被上面的年份范围筛掉了 —— 把范围放宽即可。'
      + '「无拍摄年份」那一档默认不抽，需要就在上面勾上。',
  }
})

// ============================================================
// 卡片 / 缩略图 / 队列导航
// ============================================================

/** 缩略图 404 的 photoCode 集合：坏图不留破图，退成一个占位块 */
const brokenThumbs = ref(new Set())

function onThumbError(photoCode) {
  const next = new Set(brokenThumbs.value)
  next.add(photoCode)
  brokenThumbs.value = next
}

const cardRefs = ref([])

function setCardRef(el, index) {
  if (el) cardRefs.value[index] = el
  else delete cardRefs.value[index]
}

/**
 * 队列导航：把焦点移到上一年/下一年。
 *
 * ⚠️ 是**移动焦点**（`Enter` 才打开），不是直接跳转 ——
 *    左右键直接换页会让用户在一条自己还没看全的队列里迷路，
 *    而且与照片详情页的左右翻页语义撞车。
 */
function focusCard(index) {
  const target = cardRefs.value[index]
  if (!target) return
  const node = target.$el || target            // RouterLink 的实例 -> <a>
  if (typeof node.focus === 'function') node.focus()
  if (typeof node.scrollIntoView === 'function') {
    node.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
  }
}

/** 当前焦点在第几张（找不到就当作 -1，→ 从第一张开始） */
function currentIndex() {
  const active = document.activeElement
  return cardRefs.value.findIndex((one) => (one?.$el || one) === active)
}

function move(delta) {
  const total = items.value.length
  if (!total) return
  const from = currentIndex()
  const next = from < 0 ? (delta > 0 ? 0 : total - 1) : from + delta
  if (next < 0 || next >= total) return          // 不循环：循环会让人失去位置感
  focusCard(next)
}

function isTypingTarget(target) {
  if (!target) return false
  const tag = String(target.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable
}

/** 任何 EP 浮层打开时都不响应（与 ReviewView 逐字一致：`*=` 不能写成 `=`） */
function overlayOpen() {
  return Boolean(document.querySelector('.el-overlay:not([style*="display: none"])'))
}

function onKeydown(event) {
  // ⚠️ 面板不是当前 Tab 就完全不响应（见文件头 ⑤）
  if (!props.active) return
  if (event.metaKey || event.ctrlKey || event.altKey) return
  if (isTypingTarget(event.target)) return
  if (overlayOpen()) return
  const key = String(event.key)
  if (key === 'ArrowRight') {
    event.preventDefault()
    move(1)
  } else if (key === 'ArrowLeft') {
    event.preventDefault()
    move(-1)
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

function targetOf(item) {
  return photoDetailLink(item.photoCode, {
    ...currentSource(route, String(props.displayName || '人物详情')),
    // 沿「这个人的照片」翻页，而不是全库
    scope: scopeParam('person', props.personCode),
  })
}

/** 抽中概率的展示：唯一候选不显示（100% 没有信息量） */
function probabilityText(item) {
  const count = Number(item.candidateCount) || 0
  if (count <= 1 || item.probability == null) return ''
  return `概率 ${(Number(item.probability) * 100).toFixed(1)}%`
}

// ============================================================
// zip 下载
// ============================================================

const zipLoading = ref(false)
const zipResult = ref(null)

/**
 * 先 POST（幂等打包，拿到「缺了几张」）再让浏览器直接下载。
 *
 * ⚠️ 为什么不直接导航到下载地址：那样拿不到 `zipMissingCount` ——
 *    原图不在磁盘上时，用户会安静地拿到一个少了几张照片的 zip。
 */
async function downloadZip() {
  const code = batch.value?.batchCode
  if (!code) return
  zipLoading.value = true
  try {
    const info = await buildEraSampleZip(code, false)
    zipResult.value = info
    if (Number(info.zipMissingCount) > 0) {
      ElMessage.warning(
        `${info.zipMissingCount} 张原图在磁盘上找不到，未打包（共打包 ${info.zipPhotoCount} 张）`,
      )
    }
    const link = document.createElement('a')
    // 不加 `download` 属性：文件名由服务端 Content-Disposition 给（中文名，
    // 加了 download 会用 URL 末段那串 ASCII 覆盖掉它，见 api/eraSample.js）
    link.href = eraSampleZipUrl(code)
    link.rel = 'noopener'
    document.body.appendChild(link)
    link.click()
    link.remove()
    emit('refresh')
  } catch (e) {
    // request.js 已弹过 Toast；这里只保证不产生未处理的 rejection
  } finally {
    zipLoading.value = false
  }
}

const zipHint = computed(() => {
  const b = batch.value
  if (!b) return ''
  if (!b.zipYMDHMS && Number(b.zipSize) === 0) return ''
  return `上次打包：${formatCount(b.zipPhotoCount)} 张`
    + (Number(b.zipMissingCount) ? `（缺 ${formatCount(b.zipMissingCount)} 张）` : '')
    + `，${formatFileSize(b.zipSize)}`
})
</script>

<template>
  <div class="space-y-4 pt-2">
    <!-- 口径说明（见文件头 ①：与 Tab1 的粒度不同、口径同源，必须写清楚） -->
    <p class="pb-hint">
      按<b>年</b>抽样（每年 1 张），日期取自<b>有效拍摄年</b>（含人工修正的年代）。
      上方的「时间轴」按<b>年代档</b>分组，两者粒度不同、口径一致。
      每次生成都是一个新批次，旧批次在下面切换、不会被覆盖。
    </p>

    <!-- ============ 抽签参数 ============ -->
    <section class="pb-card space-y-3 p-3" aria-label="抽样参数">
      <div class="flex flex-wrap items-end gap-x-4 gap-y-2">
        <!-- ⚠️ 标签文字必须 `shrink-0`：flex 里裸文本节点会被输入框挤窄，
             变成「年\n份」这种逐字换行（实测踩过） -->
        <label class="flex items-center gap-1.5 text-caption text-ink-sub">
          <span class="shrink-0">年份</span>
          <el-input v-model="form.yearFrom" class="w-20" size="small" placeholder="不限" />
          <span class="shrink-0" aria-hidden="true">–</span>
          <el-input v-model="form.yearTo" class="w-20" size="small" placeholder="不限" />
        </label>

        <label class="flex items-center gap-1.5 text-caption text-ink-sub">
          <span class="shrink-0">抽法</span>
          <el-select v-model="form.mode" class="w-52" size="small">
            <el-option v-for="one in MODES" :key="one.value" :label="one.label" :value="one.value" />
          </el-select>
        </label>

        <label class="flex items-center gap-1.5 text-caption text-ink-sub">
          <span class="shrink-0">种子</span>
          <el-input
            v-model="form.seed"
            class="w-36"
            size="small"
            placeholder="留空=随机"
            title="留空 = 每次随机；填了 = 同参数下可复现（会抽出一模一样的一批）"
          />
        </label>
        <el-button size="small" title="随机生成一个种子" @click="rollSeed">
          <Dices class="mr-1 h-3.5 w-3.5" aria-hidden="true" />随机
        </el-button>
      </div>

      <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
        <el-checkbox v-model="form.includeNoYear" size="small">
          包含「无拍摄年份」的一档
        </el-checkbox>
        <el-checkbox v-model="form.onlyConfirmed" size="small">
          只取人工确认的样本
        </el-checkbox>
        <el-checkbox v-model="form.excludeDuplicate" size="small">
          排除重复照片
        </el-checkbox>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <el-button type="primary" :loading="creating" @click="submit">
          {{ batch ? '重新抽' : '生成抽样' }}
        </el-button>
        <el-button
          :disabled="!items.length"
          :loading="zipLoading"
          @click="downloadZip"
        >
          <Download class="mr-1 h-3.5 w-3.5" aria-hidden="true" />下载 zip
        </el-button>

        <!-- 历史批次（≤1 个时不给下拉：一个选项的下拉只是噪声） -->
        <label
          v-if="batches.length > 1"
          class="ml-auto flex items-center gap-1.5 text-caption text-ink-sub"
        >
          <span class="shrink-0">历史批次</span>
          <el-select
            :model-value="batch?.batchCode || ''"
            class="w-56"
            size="small"
            @update:model-value="(value) => emit('select', value)"
          >
            <el-option
              v-for="one in batches"
              :key="one.batchCode"
              :label="`${one.photoCount} 年 · 种子 ${one.randomSeed}`"
              :value="one.batchCode"
            />
          </el-select>
        </label>
      </div>

      <!-- 当前批次的「怎么抽的」：信息行的存在是为了让⑥的复现承诺**可见** -->
      <p v-if="batch" class="pb-hint">
        本批种子 <code class="select-all">{{ batch.randomSeed }}</code> ·
        {{ batch.mode === 'uniform' ? '均匀' : '加权' }} ·
        候选 {{ formatCount(batch.candidatePhotoCount) }} 张 /
        {{ formatCount(batch.candidateYearCount) }} 个年份 ·
        抽出 {{ formatCount(batch.photoCount) }} 年
        <template v-if="batch.yearFrom || batch.yearTo">
          · 年份 {{ batch.yearFrom || '不限' }}–{{ batch.yearTo || '不限' }}
        </template>
        <template v-if="batch.note"> · {{ batch.note }}</template>
        <br />
        同一个人 + 同样的参数 + 同一个种子 ⇒ 抽出完全一样的一批
        （前提：候选集没变 —— 新增照片、改判归属都会改变候选池）。
        <template v-if="zipHint"><br />{{ zipHint }}。</template>
      </p>
    </section>

    <!-- ============ 结果 ============ -->
    <p v-if="loading" class="pb-hint">正在读取抽样…</p>

    <template v-else-if="items.length">
      <!--
        横向队列。⚠️ 用 `overflow-x-auto` + 内联滚动而不是换行：
           排成一行才看得出「按年排队」这件事（换行会变成网格，年份感就没了）。
      -->
      <ol class="flex gap-3 overflow-x-auto pb-2" aria-label="按年份排队的抽样照片">
        <li v-for="(item, index) in items" :key="item.pickOrder" class="shrink-0">
          <RouterLink
            :ref="(el) => setCardRef(el, index)"
            :to="targetOf(item)"
            class="block w-36 rounded-btn p-1 transition-colors duration-150 hover:bg-surface focus-visible:bg-surface"
            :aria-label="`${item.yearText} 的照片：候选 ${item.candidateCount} 张`"
          >
            <div class="h-36 w-full overflow-hidden rounded-btn bg-surface">
              <img
                v-if="!brokenThumbs.has(item.photoCode)"
                :src="item.thumbUrl"
                :alt="`${item.yearText} 抽中的照片`"
                class="h-full w-full object-cover"
                loading="lazy"
                @error="onThumbError(item.photoCode)"
              />
              <div v-else class="flex h-full w-full items-center justify-center text-ink-weak">
                <ImageOff class="h-5 w-5" aria-hidden="true" />
              </div>
            </div>
            <div class="mt-1 flex items-baseline justify-between gap-1">
              <span class="text-body tabular-nums text-ink">{{ item.yearText }}</span>
              <span class="shrink-0 text-caption tabular-nums text-ink-weak">
                {{ formatCount(item.candidateCount) }} 张候选
              </span>
            </div>
            <div class="text-caption text-ink-sub">
              <template v-if="item.photoFaceCount > 1">合影（{{ item.photoFaceCount }} 人）</template>
              <template v-else>单人照</template>
            </div>
            <div v-if="probabilityText(item)" class="text-caption tabular-nums text-ink-weak">
              {{ probabilityText(item) }}
            </div>
          </RouterLink>
        </li>
      </ol>

      <div class="flex items-center gap-2">
        <el-button size="small" :disabled="!items.length" @click="move(-1)">
          <ChevronLeft class="mr-1 h-3.5 w-3.5" aria-hidden="true" />上一年
        </el-button>
        <el-button size="small" :disabled="!items.length" @click="move(1)">
          下一年<ChevronRight class="ml-1 h-3.5 w-3.5" aria-hidden="true" />
        </el-button>
        <span class="pb-hint">
          键盘 <kbd>←</kbd> / <kbd>→</kbd> 在年份之间移动焦点，<kbd>Enter</kbd> 打开那张照片。
        </span>
      </div>
    </template>

    <!-- 四种空状态（见 computed emptyState） -->
    <section v-else-if="!loading" class="pb-card p-4">
      <h3 class="text-body text-ink">{{ emptyState.title }}</h3>
      <p class="pb-hint mt-1">{{ emptyState.hint }}</p>
    </section>
  </div>
</template>
