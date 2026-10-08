<!--
  P-07 扫描任务（设计稿 §4.7）
  ------------------------------------------------------------
  新建扫描（根目录只读 + 批大小，默认 100）＋ 任务列表（状态 / 本批 / 累计 / 操作）

  三条必须显式表达在界面上
  ----------------------
  · **批次限流**：每处理 batchSize 张就 PAUSED，等人工点「继续下一批」——
    所以要能看到「本批 62 / 100」和「已暂停等待指示」两个信息，不能只给一个总进度
  · **进度诚实**：本批进度是服务端从 pb_scan_job 的 batchIndex/processedCount
    推导出来的真实计数（browse.batchProgressOf），不做假进度条、不做动画插值
  · **可中断**：全树遍历阶段就能停（步骤 9 补的 walker.shouldStop）；
    停完是 PAUSED 而不是 DONE —— 后者会让「扫完了」与「一张没扫」变成同一个状态
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  CircleCheck,
  CircleX,
  HardDrive,
  Pause,
  Play,
  Plus,
  RefreshCw,
  TriangleAlert,
} from 'lucide-vue-next'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import { DEFAULT_BATCH_SIZE, JOB_STATUS_META, useScanStore } from '@/store/scan'
import { useFaceStore } from '@/store/face'
import { useMatchStore } from '@/store/match'
import { useReviewStore } from '@/store/review'
import { getSettings } from '@/api/settings'
import { formatCount, formatDateTime } from '@/utils/format'

const scan = useScanStore()
const face = useFaceStore()
const match = useMatchStore()
const review = useReviewStore()
const createVisible = ref(false)
const batchSizeInput = ref(scan.batchSize || DEFAULT_BATCH_SIZE)
/** 扫描跑完是否自动识别人脸（缺省开 —— 扫描与认脸是同一次入库的两步） */
const autoFaceInput = ref(true)

/** 根目录只读：改目录等于换库，只应由后端配置决定 */
const rootPath = ref('')
const starting = ref(false)

/** 展开原始错误的 jobCode（FAILED 才可展开） */
const errorOf = ref('')
const faceErrorOf = ref('')

/** 正在操作的任务（禁用整行的按钮，避免重复点） */
const busyJob = ref('')
const busyFaceJob = ref('')
/** 轮询中的任务：只有 RUNNING 需要 */
const pollJob = ref('')
const pollFaceJob = ref('')
let pollTimer = null
let facePollTimer = null
/** 匹配不是任务（无 jobCode、无批次），只有一个阶段快照，所以只有一个定时器 */
let matchTimer = null

// ---- 状态 → 色/图标/文字三重编码（不单靠颜色）----
const STATUS_TONE = {
  neutral: 'text-ink-sub',
  info: 'text-info-ink',
  warning: 'text-warning-ink',
  success: 'text-success-ink',
  danger: 'text-danger-ink',
}
const STATUS_SOFT = {
  neutral: 'bg-surface',
  info: 'bg-info-soft',
  warning: 'bg-warning-soft',
  success: 'bg-success-soft',
  danger: 'bg-danger-soft',
}
/** 设计稿要求的四个图标：RUNNING ◐、PAUSED ⏸、DONE ✓、FAILED ✕ */
const STATUS_ICON = {
  IDLE: Play,
  RUNNING: RefreshCw,
  PAUSED: Pause,
  DONE: CircleCheck,
  FAILED: CircleX,
}
const STATUS_GLYPH = {
  IDLE: '○',
  RUNNING: '◐',
  PAUSED: '⏸',
  DONE: '✓',
  FAILED: '✕',
}

const jobs = computed(() => scan.jobs || [])
const faceJobs = computed(() => face.jobs || [])

/** 匹配的阶段快照（null = 还没取到） */
const matchStatus = computed(() => match.status)
/**
 * 没有启用质心就点不动按钮。
 * 质心只由**人工确认**的样本生成（每人至少 MIN_CENTROID_SAMPLES 张），
 * 所以「刚认完脸、一张都没确认」时这里必然是 0 —— 后端也会以 400 挡下，
 * 但把原因摆在按钮旁边比让用户点了才发现要好。
 */
const canRunMatch = computed(() => Number(matchStatus.value?.enabledCentroids) > 0)

/**
 * 判定原因码 -> 人话。
 * 为什么必须翻译出来：「进聚类」「仍需你确认」这两个数字本身没有信息量，
 * 用户最需要知道的是**为什么没认出来** —— 是库里还没有可比质心（先去确认几张脸），
 * 还是这些脸跟谁都对不上（要新建人物），两者的下一步动作完全不同。
 * 键名与 engine/match/matcher.py 的 REASON_* 常量逐字对应。
 */
const REASON_LABEL = {
  matched: '命中质心',
  low_score: '与所有质心都不像',
  no_centroid: '库里没有可比的质心',
  no_bucket: '没有拍摄年份、也没有兜底质心',
  no_embedding: '没有向量（脏数据）',
}

const reasonText = computed(() => {
  const byReason = matchStatus.value?.byReason || {}
  return Object.keys(byReason)
    .filter((key) => Number(byReason[key]) > 0)
    .map((key) => `${REASON_LABEL[key] || key} ${formatCount(byReason[key])} 张`)
    .join(' · ')
})

function metaOf(row) {
  return JOB_STATUS_META[row?.jobStatus] || JOB_STATUS_META.IDLE
}

/** 本批进度条：宽度来自真实计数；batchProcessed 为 null（DONE）时不画 */
function batchPercent(row) {
  const done = Number(row?.batchProcessed)
  const size = Number(row?.batchSize)
  if (!Number.isFinite(done) || done === null || !size) return 0
  return Math.min(100, Math.round((done / size) * 100))
}

function batchText(row) {
  const done = row?.batchProcessed
  const size = Number(row?.batchSize) || 0
  if (done === null || done === undefined) return '—'
  return `${done} / ${size}`
}

/** 「已暂停等待指示」这句必须出现在行里，不能只靠状态色块暗示 */
function statusHint(row) {
  switch (row?.jobStatus) {
    case 'RUNNING':
      return '正在扫描本批…'
    case 'PAUSED':
      return '已暂停，等待你点「继续下一批」'
    case 'DONE':
      return '全部处理完成'
    case 'FAILED':
      return '出错终止，点「查看错误」看原始报错'
    default:
      return '已创建，尚未开始'
  }
}

/** 人脸识别任务的状态说明：同样必须显式说，不能只靠颜色 */
function faceStatusHint(row) {
  switch (row?.jobStatus) {
    case 'RUNNING':
      return '正在识别人脸本批…'
    case 'PAUSED':
      return '已暂停，点「识别下一批」接着跑'
    case 'DONE':
      return '本轮识别人脸已完成'
    case 'FAILED':
      return '出错终止，点「查看错误」看原始报错'
    default:
      return '已创建，尚未开始'
  }
}

function canResume(row) {
  return row?.jobStatus === 'PAUSED' || row?.jobStatus === 'IDLE' || row?.jobStatus === 'FAILED'
}

async function load() {
  await Promise.all([
    scan.fetchJobs(),
    face.fetchJobs(),
    face.fetchOverview(),
    // 匹配快照与侧栏角标一起刷：匹配会改变「我不同意」的条数
    match.fetchStatus(),
    review.fetchBadge().catch(() => {}),
  ])
  // 有任务正在跑就自动开轮询 —— 「跑起来了但进度不动」是最容易被当成坏了的形态
  const running = jobs.value.find((one) => one.jobStatus === 'RUNNING')
  if (running) startPolling(running.jobCode)
  else stopPolling()
  // 人脸识别任务独立轮询：它可能是在扫描跑完后被自动拉起来的，
  // 那时扫描任务已 DONE，不启动这个轮询就会一直停在「已创建」
  const faceRunning = faceJobs.value.find((one) => one.jobStatus === 'RUNNING')
  if (faceRunning) startFacePolling(faceRunning.jobCode)
  else stopFacePolling()
}

/** 开始人脸识别。
 *
 * ⚠️ 依赖缺失时**直接拒绝并把原因显示出来**，不发出请求：
 *    缺 onnxruntime/cv2 时子进程 import 就死，而主进程看到的是
 *    「结果队列没数据」—— 任务会报「完成、一张脸都没检出」，
 *    与「这批照片没人脸」在界面上完全一样。宁可点不动，也不要假装成功。
 */
async function startFaceJob() {
  const env = face.environment || {}
  if (env.ok === false) {
    ElMessage.error(env.hint || '缺少人脸识别依赖，无法开始')
    return
  }
  if (!face.pendingPhotos) {
    ElMessage.info('所有照片都已经识别人脸了，没有待处理项')
    return
  }
  const data = await face.start({ batchSize: scan.batchSize || DEFAULT_BATCH_SIZE })
  ElMessage.success(`人脸识别任务 ${data.jobCode} 已启动`)
  startFacePolling(data.jobCode)
  await load()
}

async function resumeFace(row) {
  busyFaceJob.value = row.jobCode
  try {
    // 后端没有 /face/resume：识别任务本来就是 startBackground 跑到 DONE 的
    // （maxBatches 不限），所以 PAUSED 只可能来自「停止」，续跑靠再点开始。
    const data = await face.start({
      jobCode: row.jobCode,
      batchSize: Number(row.batchSize) || scan.batchSize || DEFAULT_BATCH_SIZE,
    })
    ElMessage.info('已继续下一批')
    startFacePolling(data.jobCode || row.jobCode)
    await load()
  } finally {
    busyFaceJob.value = ''
  }
}

async function pauseFace(row) {
  busyFaceJob.value = row.jobCode
  try {
    const data = await face.stop(row.jobCode, true)
    // stillRunning=true 不是失败：停止在**批边界**生效，当前批还在跑完
    if (data?.stillRunning) ElMessage.warning(data.note || '正在跑完当前批，稍后再看')
    else ElMessage.info('已暂停')
    stopFacePolling()
    await load()
  } finally {
    busyFaceJob.value = ''
  }
}

function toggleFaceError(row) {
  faceErrorOf.value = faceErrorOf.value === row.jobCode ? '' : row.jobCode
}

function startFacePolling(jobCode) {
  if (!jobCode) return
  if (pollFaceJob.value === jobCode && facePollTimer) return
  stopFacePolling()
  pollFaceJob.value = jobCode
  facePollTimer = setInterval(async () => {
    try {
      await face.refreshStatus(jobCode)
    } catch (e) {
      /* 轮询失败静默：下一步自然会再试 */
    }
    if (face.progress?.jobStatus !== 'RUNNING') {
      stopFacePolling()
      await load()
    }
  }, 1500)
}

function stopFacePolling() {
  if (facePollTimer) {
    clearInterval(facePollTimer)
    facePollTimer = null
  }
  pollFaceJob.value = ''
}

/* ============================================================
 * 人脸匹配（第三步）—— 不是任务，所以没有 jobCode / 批次 / 续跑
 * ============================================================
 * 与认脸的两点本质差别，决定了这段代码为什么长这样：
 *   ① 认脸是**任务**（后台线程跑批，可能几十分钟），所以有 jobCode、
 *      进度条、「识别下一批」；匹配是一次几秒到几十秒的操作，跑完即止，
 *      没有「下一批」这回事 —— 别给它套任务的壳。
 *   ② 认脸的产物**只增不减**（多出待确认）；匹配会**同时改两个队列**：
 *      待确认减少、「我不同意」增加。所以跑完必须同时刷新 face
 *      （待确认条数）与 review（侧栏角标），只刷一个会显示成「数据错了」。
 */

/** 跑一次匹配：assign=false 只算不写（预览），用户看过统计再确认落库 */
async function startMatchJob(assign) {
  try {
    const data = await match.start({ assign })
    if (data?.started === false) {
      ElMessage.info(data?.note || '没有可判定的脸')
      await afterMatch()
      return
    }
    ElMessage.success(assign ? '匹配已开始，跑完会自动落库' : '匹配已开始（预览，结果不会写库）')
    startMatchPolling()
    await match.fetchStatus()
    if (!match.running) {
      // 小库上匹配可能一秒内就结束：不开轮询的那条路（走了也很可能已经跑完）
      stopMatchPolling()
      await afterMatch()
    }
  } catch (e) {
    // 前置校验失败（质心为空 / 已有任务在跑 / 队列为空）由 request.js 统一弹错误，
    // 这里不再叠一层 Toast —— 同一个原因弹两次比不弹更让人困惑
  }
}

/** 主按钮：按「先预览」勾选态决定落不落库 */
function runMatchNow() {
  return startMatchJob(!match.preview)
}

/** 预览后确认落库（同一份判定，幂等：重复跑不会重复归属） */
function confirmMatchAssign() {
  return startMatchJob(true)
}

function startMatchPolling() {
  if (matchTimer) return
  matchTimer = setInterval(async () => {
    try {
      await match.fetchStatus()
    } catch (e) {
      /* 轮询失败静默：下一步自然会再试 */
    }
    if (!match.running) {
      stopMatchPolling()
      await afterMatch()
    }
  }, 1000)
}

function stopMatchPolling() {
  if (matchTimer) {
    clearInterval(matchTimer)
    matchTimer = null
  }
}

/** 匹配跑完：两个队列都变了，必须一起刷新（只刷一个会像是数据错了） */
async function afterMatch() {
  await load()
  await review.fetchBadge()
}

onMounted(async () => {
  // 根目录来自设置页的只读展示，不在这里另解析一份（DR-14：只有一个真相）
  try {
    const data = await getSettings()
    rootPath.value = data?.paths?.photo || ''
  } catch (e) {
    rootPath.value = ''
  }
  await load()
})

onBeforeUnmount(() => {
  stopPolling()
  stopFacePolling()
  stopMatchPolling()
})

function startPolling(jobCode) {
  if (!jobCode) return
  if (pollJob.value === jobCode && pollTimer) return
  stopPolling()
  pollJob.value = jobCode
  pollTimer = setInterval(async () => {
    try {
      await scan.refreshStatus(jobCode)
    } catch (e) {
      /* 轮询失败静默：下一步自然会再试，不该每 1.5 秒弹一次 Toast */
    }
    if (scan.progress?.jobStatus !== 'RUNNING') {
      stopPolling()
      await load()
    }
  }, 1500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  pollJob.value = ''
}

function openCreate() {
  batchSizeInput.value = scan.batchSize || DEFAULT_BATCH_SIZE
  createVisible.value = true
}

/**
 * 新建扫描。
 *
 * ⚠️ rootPath **只传后端给的 photo_dir()**：后端会做精确相等校验
 * （库外目录与子目录都400），而「子目录也不行」是因为 relPathHash 会不同 ->
 * 同一张照片被判定成两张 -> 重复入库。
 */
async function confirmCreate() {
  starting.value = true
  try {
    scan.setBatchSize(batchSizeInput.value)
    const data = await scan.start(rootPath.value, autoFaceInput.value)
    createVisible.value = false
    ElMessage.success(`任务 ${data.jobCode} 已启动`)
    // 扫描跑完会自动接人脸识别（autoFace 缺省 true）：这一句必须提前说，
    // 否则用户看到「扫描 100% 完成」却发现待确认还是 0，会以为坏了
    if (data.autoFace === false) ElMessage.info('已关闭「扫完自动认脸」：待确认数不会变化')
    else ElMessage.info('扫描完成后会自动开始识别人脸')
    startPolling(data.jobCode)
    await load()
  } finally {
    starting.value = false
  }
}

async function resume(row) {
  busyJob.value = row.jobCode
  try {
    // wait=0：后台继续跑，界面靠轮询看 —— 同步等一批会让按钮卡住好几秒
    await scan.resume(row.jobCode, false)
    ElMessage.info('已继续下一批')
    startPolling(row.jobCode)
    await load()
  } finally {
    busyJob.value = ''
  }
}

async function pause(row) {
  busyJob.value = row.jobCode
  try {
    // wait=1：等后台线程真的退出再返回，否则计数还没落库就刷新了
    await scan.stop(row.jobCode, true)
    stopPolling()
    ElMessage.info('已暂停')
    await load()
  } finally {
    busyJob.value = ''
  }
}

function toggleError(row) {
  errorOf.value = errorOf.value === row.jobCode ? '' : row.jobCode
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">扫描任务</h2>
        <p class="pb-hint mt-1">
          扫描按批次限流：每处理 {{ scan.batchSize }} 张就停下来等你确认，不会一口气跑完整个照片库。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">
        <Plus class="mr-1 h-4 w-4" aria-hidden="true" />新建扫描
      </el-button>
    </div>

    <!-- 根目录（只读） -->
    <section class="pb-card flex flex-wrap items-center gap-3 p-4">
      <HardDrive class="h-4 w-4 shrink-0 text-ink-weak" aria-hidden="true" />
      <div class="min-w-0 flex-1">
        <p class="pb-hint">照片根目录（只读）</p>
        <p class="truncate font-mono text-body text-ink">{{ rootPath || '（后端未就绪）' }}</p>
      </div>
      <p class="pb-hint">
        目录由后端配置决定，界面上不提供修改入口 —— 原图只读，改目录等于换库。
      </p>
    </section>

    <!-- ============ 人脸识别（扫描之后的第二步） ============ -->
    <section class="pb-card p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="min-w-0">
          <h3 class="pb-section-title">人脸识别</h3>
          <p class="pb-hint mt-1">
            扫描只把照片写进图库，<strong class="text-ink">「待确认」来自这一步</strong> ——
            抽出的人脸进入待确认队列后，才会出现在侧栏的待确认数字里。
          </p>
        </div>
        <el-button
          type="primary"
          :loading="face.starting"
          :disabled="!face.canStart()"
          @click="startFaceJob"
        >
          <Play class="mr-1 h-4 w-4" aria-hidden="true" />
          {{ face.pendingPhotos ? `识别人脸（${formatCount(face.pendingPhotos)} 张待处理）` : '识别人脸' }}
        </el-button>
      </div>

      <!-- 两个计数含义不同，并列展示、绝不合并 -->
      <div class="mt-3 flex flex-wrap gap-4">
        <p class="text-body text-ink">
          待识别照片 <span class="font-semibold tabular-nums">{{ formatCount(face.pendingPhotos) }}</span> 张
        </p>
        <p class="text-body text-ink">
          待确认人脸 <span class="font-semibold tabular-nums">{{ formatCount(face.pendingFaces) }}</span> 条
        </p>
        <p v-if="!face.pendingPhotos" class="text-body text-success-ink">
          照片都已识别人脸，待确认数只增不减是正常现象 —— 它要等你去确认。
        </p>
      </div>

      <!-- 依赖缺失必须显式报警：缺依赖时任务会「成功」却检出 0 张脸 -->
      <p
        v-if="face.environment && face.environment.ok === false"
        class="mt-3 rounded-btn border border-line bg-danger-soft px-3 py-2 text-caption text-danger-ink"
      >
        <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
        {{ face.environment.hint || '缺少人脸识别依赖，无法识别人脸' }}
      </p>

      <!-- 人脸识别任务列表 -->
      <ul v-if="faceJobs.length" class="mt-3 space-y-2">
        <li v-for="row in faceJobs" :key="row.jobCode" class="pb-card p-3">
          <div class="flex flex-wrap items-center gap-3">
            <span class="w-40 shrink-0 font-mono text-caption text-ink">{{ row.jobCode }}</span>

            <span
              class="inline-flex items-center gap-1.5 rounded-btn px-2 py-0.5 text-caption"
              :class="[STATUS_SOFT[metaOf(row).tone], STATUS_TONE[metaOf(row).tone]]"
            >
              <component :is="STATUS_ICON[row.jobStatus] || Play" class="h-3 w-3" aria-hidden="true" />
              <span aria-hidden="true">{{ STATUS_GLYPH[row.jobStatus] || '○' }}</span>
              {{ metaOf(row).label }}
              <span class="sr-only">（{{ faceStatusHint(row) }}）</span>
            </span>

            <span class="w-36 shrink-0">
              <span class="text-caption tabular-nums text-ink">{{ batchText(row) }}</span>
              <span
                v-if="row.batchProcessed !== null && row.batchProcessed !== undefined"
                class="mt-1 block h-1.5 w-full overflow-hidden rounded-btn bg-surface"
                role="progressbar"
                :aria-valuenow="row.batchProcessed"
                :aria-valuemax="row.batchSize"
                :aria-label="`${row.jobCode} 本批进度`"
              >
                <span
                  class="block h-full"
                  :class="row.jobStatus === 'RUNNING' ? 'bg-info' : 'bg-[var(--pb-warning-line)]'"
                  :style="{ width: `${batchPercent(row)}%` }"
                />
              </span>
            </span>

            <!-- 人脸口径的三个计数：检出人脸的照片数 / 入库人脸条数 / 失败 -->
            <span class="w-56 shrink-0 text-caption tabular-nums text-ink-sub">
              有人脸 {{ formatCount(row.addedCount) }} 张
              · 入库 {{ formatCount(row.pendingCount) }} 条
              <span v-if="row.skippedCount" class="text-ink-weak">· 失败 {{ formatCount(row.skippedCount) }}</span>
            </span>

            <span class="ml-auto flex flex-wrap items-center gap-2">
              <el-button
                v-if="row.jobStatus === 'RUNNING'"
                size="small"
                :loading="busyFaceJob === row.jobCode"
                @click="pauseFace(row)"
              >
                <Pause class="mr-1 h-3.5 w-3.5" aria-hidden="true" />停止
              </el-button>
              <el-button
                v-else-if="canResume(row)"
                size="small"
                type="primary"
                :loading="busyFaceJob === row.jobCode"
                @click="resumeFace(row)"
              >
                <Play class="mr-1 h-3.5 w-3.5" aria-hidden="true" />识别下一批
              </el-button>
              <el-button
                v-if="row.jobStatus === 'FAILED'"
                size="small"
                type="danger"
                plain
                :aria-expanded="faceErrorOf === row.jobCode"
                @click="toggleFaceError(row)"
              >
                <TriangleAlert class="mr-1 h-3.5 w-3.5" aria-hidden="true" />查看错误
              </el-button>
            </span>
          </div>

          <p
            class="mt-2 text-caption"
            :class="row.jobStatus === 'PAUSED' ? 'text-warning-ink' : 'text-ink-weak'"
          >
            {{ faceStatusHint(row) }}
            <template v-if="row.batchIndex">· 已完成第 {{ row.batchIndex }} 批</template>
            <template v-if="row.finishedYMDHMS">· {{ formatDateTime(row.finishedYMDHMS) }} 结束</template>
            <template v-if="row.replaceFaces">· 重提取模式</template>
          </p>

          <pre
            v-if="faceErrorOf === row.jobCode"
            class="mt-2 overflow-x-auto rounded-btn bg-surface px-3 py-2 text-caption text-danger-ink"
          >{{ row.errMsg || '(没有记录错误文本，看后端日志)' }}</pre>
        </li>
      </ul>

      <p v-else class="pb-hint mt-3">
        还没有人脸识别任务。扫描完成后会自动发起一次；也可以现在点右上角手动开始。
      </p>
    </section>

    <!-- ============ 人脸匹配（第三步：把待确认的脸自动归属给某人） ============ -->
    <section class="pb-card p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="min-w-0">
          <h3 class="pb-section-title">人脸匹配</h3>
          <p class="pb-hint mt-1">
            把<strong class="text-ink">还没有归属</strong>的脸判给某个人。
            机器认下的结果<strong class="text-ink">不算你确认过</strong> ——
            它们会进「我不同意」，等你逐张否决。
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <el-button
            type="primary"
            :loading="match.starting"
            :disabled="match.running || !canRunMatch"
            @click="runMatchNow"
          >
            <Play class="mr-1 h-4 w-4" aria-hidden="true" />
            {{ match.running ? '匹配进行中…' : '跑一次匹配' }}
          </el-button>
          <el-button
            v-if="match.previewOnly"
            type="success"
            plain
            :loading="match.starting"
            @click="confirmMatchAssign"
          >
            <CircleCheck class="mr-1 h-4 w-4" aria-hidden="true" />
            确认落库（{{ formatCount(match.counts.auto) }} 张）
          </el-button>
        </div>
      </div>

      <!-- 四个数口径各不相同，并列展示、绝不合并 -->
      <div class="mt-3 flex flex-wrap gap-4">
        <p class="text-body text-ink">
          待确认
          <span class="font-semibold tabular-nums">{{ formatCount(review.pendingCount) }}</span> 条
        </p>
        <p class="text-body text-ink">
          我不同意
          <span class="font-semibold tabular-nums text-warning-ink">{{ formatCount(review.disputedCount) }}</span> 条
        </p>
        <p class="text-body text-ink">
          已人工确认
          <span class="font-semibold tabular-nums">{{ formatCount(matchStatus?.current?.confirmed) }}</span> 张
        </p>
        <p class="text-body text-ink">
          启用质心
          <span class="font-semibold tabular-nums">{{ formatCount(matchStatus?.enabledCentroids) }}</span> 个桶
        </p>
      </div>

      <!-- 状态取不到时必须说清：按钮会被禁用，不能让用户对着灰按钮猜原因 -->
      <p
        v-if="!matchStatus"
        class="mt-3 flex flex-wrap items-center gap-2 text-caption text-ink-weak"
      >
        还没取到匹配状态（后端未就绪或请求失败）。
        <el-button size="small" text @click="load">重试</el-button>
      </p>

      <!-- 空质心必须提前说清：这是「点了也没结果」的唯一原因 -->
      <p
        v-if="matchStatus && !canRunMatch"
        class="mt-3 rounded-btn border border-line bg-surface px-3 py-2 text-caption text-ink-sub"
      >
        还没有启用的质心 —— 质心只由<strong class="text-ink">人工确认</strong>的样本生成
        （每人至少 {{ matchStatus.minCentroidSamples }} 张）。先去
        <RouterLink class="text-brand-ink hover:underline" to="/review">待确认队列</RouterLink>
        确认几张脸再回来；这是防污染规则（一张误认的脸会把质心带偏），不是故障。
      </p>

      <!-- 先预览、不落库 -->
      <div class="mt-3 flex flex-wrap items-center gap-3">
        <el-checkbox v-model="match.preview" :disabled="match.running">先预览，不落库</el-checkbox>
        <span class="pb-hint">勾上则只算不写：结果先给你看，确认后再点「确认落库」。</span>
      </div>

      <!-- 阶段与结果：阶段是真的（载质心 / 读脸 / 比对 / 落库），不插值、不假进度 -->
      <div
        v-if="matchStatus && matchStatus.phase !== 'idle'"
        class="mt-3 rounded-btn bg-surface px-3 py-2"
      >
        <p class="text-caption text-ink-sub">
          {{ match.phaseText }}
          <template v-if="matchStatus.phase === 'loading_centroids' && matchStatus.centroidTotal">
            · 质心 {{ formatCount(matchStatus.centroidLoaded) }} /
            {{ formatCount(matchStatus.centroidTotal) }}
          </template>
          <template v-else-if="match.running && matchStatus.total">
            · 本次待判定 {{ formatCount(matchStatus.total) }} 张
          </template>
          <template v-else-if="matchStatus.elapsed">· 用时 {{ matchStatus.elapsed }}s</template>
          <template v-if="matchStatus.preset && matchStatus.tLow">
            · 阈值 {{ matchStatus.preset }}（{{ matchStatus.tLow }} / {{ matchStatus.tHigh }}）
          </template>
        </p>

        <p v-if="matchStatus.lastError" class="mt-1 text-caption text-danger-ink">
          {{ matchStatus.lastError }}
        </p>

        <template v-else-if="matchStatus.phase === 'done'">
          <dl class="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div>
              <dt class="pb-hint">自动归属</dt>
              <dd class="text-body tabular-nums text-ink">{{ formatCount(matchStatus.auto) }} 张</dd>
            </div>
            <div>
              <dt class="pb-hint">{{ matchStatus.assign ? '已落库' : '未落库（预览）' }}</dt>
              <dd
                class="text-body tabular-nums"
                :class="matchStatus.assign ? 'text-success-ink' : 'text-warning-ink'"
              >
                {{ formatCount(matchStatus.written) }} 条
              </dd>
            </div>
            <div>
              <dt class="pb-hint">仍需你确认</dt>
              <dd class="text-body tabular-nums text-ink">{{ formatCount(matchStatus.review) }} 张</dd>
            </div>
            <div>
              <dt class="pb-hint">进聚类</dt>
              <dd class="text-body tabular-nums text-ink">{{ formatCount(matchStatus.cluster) }} 张</dd>
            </div>
          </dl>
          <p
            class="mt-2 text-caption"
            :class="match.disputedDelta ? 'text-warning-ink' : 'text-ink-weak'"
          >
            「我不同意」{{ formatCount(matchStatus.disputedBefore) }} →
            {{ formatCount(matchStatus.disputedAfter) }}
            （本次 +{{ formatCount(match.disputedDelta) }}）—— 这些是机器认的，去
            <RouterLink class="underline" to="/review">我不同意</RouterLink>
            里否决认错的那些。
          </p>
          <p v-if="reasonText" class="mt-1 text-caption text-ink-weak">
            判定原因：{{ reasonText }}
          </p>
          <p v-if="matchStatus.message" class="mt-1 text-caption text-ink-sub">
            {{ matchStatus.message }}
          </p>
        </template>
      </div>

      <p v-else class="pb-hint mt-3">
        还没有跑过匹配。它只处理<strong class="text-ink">待确认队列</strong>里还没有归属的脸；
        已自动归属的（「我不同意」）不会被重跑 —— 那是改了阈值之后重判的语义。
      </p>
    </section>

    <!-- 任务列表 -->
    <section class="pb-card p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h3 class="pb-section-title">任务列表</h3>
        <el-button size="small" :loading="scan.loading" @click="load">
          <RefreshCw class="mr-1 h-3.5 w-3.5" aria-hidden="true" />刷新
        </el-button>
      </div>

      <div v-if="scan.loading && !jobs.length" class="mt-3 space-y-2" aria-hidden="true">
        <div v-for="n in 3" :key="n" class="h-12 animate-pulse rounded-btn bg-skeleton" />
      </div>

      <p v-else-if="!jobs.length" class="pb-card mt-3 px-4 py-10 text-center text-body text-ink-weak">
        还没有扫描任务。点右上角「新建扫描」开始 —— 扫描只读原图，不会改写任何文件。
      </p>

      <template v-else>
        <ul class="mt-3 space-y-2">
          <li v-for="row in jobs" :key="row.jobCode" class="pb-card p-3">
            <div class="flex flex-wrap items-center gap-3">
              <span class="w-40 shrink-0 font-mono text-caption text-ink">{{ row.jobCode }}</span>

              <!-- 状态：色 + 图标 + 文字三重编码 -->
              <span
                class="inline-flex items-center gap-1.5 rounded-btn px-2 py-0.5 text-caption"
                :class="[STATUS_SOFT[metaOf(row).tone], STATUS_TONE[metaOf(row).tone]]"
              >
                <component :is="STATUS_ICON[row.jobStatus] || Play" class="h-3 w-3" aria-hidden="true" />
                <span aria-hidden="true">{{ STATUS_GLYPH[row.jobStatus] || '○' }}</span>
                {{ metaOf(row).label }}
                <span class="sr-only">（{{ statusHint(row) }}）</span>
              </span>

              <!-- 本批进度：真实计数 -->
              <span class="w-36 shrink-0">
                <span class="text-caption tabular-nums text-ink">{{ batchText(row) }}</span>
                <span
                  v-if="row.batchProcessed !== null && row.batchProcessed !== undefined"
                  class="mt-1 block h-1.5 w-full overflow-hidden rounded-btn bg-surface"
                  role="progressbar"
                  :aria-valuenow="row.batchProcessed"
                  :aria-valuemax="row.batchSize"
                  :aria-label="`${row.jobCode} 本批进度`"
                >
                  <span
                    class="block h-full"
                    :class="row.jobStatus === 'RUNNING' ? 'bg-info' : 'bg-[var(--pb-warning-line)]'"
                    :style="{ width: `${batchPercent(row)}%` }"
                  />
                </span>
              </span>

              <!-- 累计 -->
              <span class="w-28 shrink-0 text-caption tabular-nums text-ink-sub">
                累计 {{ formatCount(row.processedCount) }}
                <span v-if="row.totalCount" class="text-ink-weak">/ {{ formatCount(row.totalCount) }}</span>
              </span>

              <!-- 操作 -->
              <span class="ml-auto flex flex-wrap items-center gap-2">
                <el-button
                  v-if="row.jobStatus === 'RUNNING'"
                  size="small"
                  :loading="busyJob === row.jobCode"
                  @click="pause(row)"
                >
                  <Pause class="mr-1 h-3.5 w-3.5" aria-hidden="true" />暂停
                </el-button>
                <el-button
                  v-else-if="canResume(row)"
                  size="small"
                  type="primary"
                  :loading="busyJob === row.jobCode"
                  @click="resume(row)"
                >
                  <Play class="mr-1 h-3.5 w-3.5" aria-hidden="true" />继续下一批
                </el-button>
                <el-button
                  v-if="row.jobStatus === 'FAILED'"
                  size="small"
                  type="danger"
                  plain
                  :aria-expanded="errorOf === row.jobCode"
                  @click="toggleError(row)"
                >
                  <TriangleAlert class="mr-1 h-3.5 w-3.5" aria-hidden="true" />查看错误
                </el-button>
              </span>
            </div>

            <!-- 状态说明常驻：不能只靠颜色块暗示「已暂停」 -->
            <p
              class="mt-2 text-caption"
              :class="row.jobStatus === 'PAUSED' ? 'text-warning-ink' : 'text-ink-weak'"
            >
              {{ statusHint(row) }}
              <template v-if="row.batchIndex">
                · 已完成第 {{ row.batchIndex }} 批
              </template>
              <template v-if="row.finishedYMDHMS">
                · {{ formatDateTime(row.finishedYMDHMS) }} 结束
              </template>
            </p>

            <!-- 断点续扫的位置：用户最怕的是「续扫会不会从头再来」 -->
            <p v-if="row.jobStatus === 'PAUSED' && row.lastCursor" class="pb-hint mt-1">
              断点游标：<code class="font-mono">{{ row.lastCursor }}</code>
              —— 点「继续下一批」从这里接着扫，不会从头再来。
            </p>

            <!-- 原始错误 -->
            <pre
              v-if="errorOf === row.jobCode"
              class="mt-2 overflow-x-auto rounded-btn bg-surface px-3 py-2 text-caption text-danger-ink"
            >{{ row.errMsg || '(没有记录错误文本 —— 可能是在写库阶段失败的，看后端日志)' }}</pre>
          </li>
        </ul>

        <footer
          v-if="scan.total > scan.size"
          class="mt-3 flex items-center justify-end gap-2"
        >
          <el-pagination
            layout="prev, pager, next"
            background
            :total="scan.total"
            :page-size="scan.size"
            :current-page="scan.page"
            @current-change="(value) => { scan.page = value; load(); }"
          />
        </footer>
      </template>

      <p class="pb-hint mt-3">
        <template v-if="jobs.some((one) => one.jobStatus === 'PAUSED')">
          有任务处于「已暂停」，点「继续下一批」从上次游标接着扫 —— 不会从头再来。
        </template>
        <template v-else-if="jobs.length">
          所有任务都已跑完。进度显示的是真实计数，不做假进度条。
        </template>
      </p>
    </section>

    <!-- 新建扫描对话框 -->
    <el-dialog v-model="createVisible" title="新建扫描任务" width="460px">
      <div class="space-y-4">
        <div>
          <p class="pb-hint">照片根目录</p>
          <p class="mt-1 rounded-btn border border-line bg-surface px-3 py-2 font-mono text-body text-ink">
            {{ rootPath || '（后端未就绪）' }}
          </p>
          <p class="pb-hint mt-1">
            只能扫描照片根目录本身：库外目录与子目录都会被后端拒绝（会导致重复入库或写脏 relPath）。
          </p>
        </div>

        <div>
          <label class="pb-hint block" for="sj-batch">每批张数</label>
          <el-input-number
            id="sj-batch"
            v-model="batchSizeInput"
            :min="1"
            :max="5000"
            :step="50"
            class="w-40"
          />
          <p class="pb-hint mt-1">
            默认 {{ DEFAULT_BATCH_SIZE }}。到这个数就自动暂停，等你点「继续下一批」——
            10 万张的库一口气跑完不合适。
          </p>
        </div>

        <div>
          <el-checkbox v-model="autoFaceInput">
            扫描完成后自动识别人脸
          </el-checkbox>
          <p class="pb-hint mt-1">
            扫描只把照片写进图库，待确认来自人脸识别这一步。
            保持勾选：扫完会自动接着认脸，待确认才会增加。
          </p>
        </div>
      </div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="starting" @click="confirmCreate">开始扫描</el-button>
      </template>
    </el-dialog>
  </div>
</template>