/* ============================================================
 * 扫描任务 store —— 任务列表 / 进度轮询
 * ============================================================
 * 两条语义必须保持（设计稿 §4.7 / §6.1）：
 *   1. **批次限流**：每处理 batchSize（默认 100）张就置 PAUSED，等人工点
 *      「继续下一批」。所以 UI 必须显式展示「本批进度 + 已暂停等待指示」。
 *   2. **进度诚实**：展示的是 pb_scan_job 行里的真实计数（已处理 / 本批 / 累计），
 *      不做假进度条。
 *
 * 轮询用 silent:true —— 轮询失败不该每 2 秒弹一次 Toast（见 api/request.js）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getScanStatus, listJobs, resumeScan, startScan, stopScan } from '@/api/scan'

/** 每批默认 100 张（硬约束） */
export const DEFAULT_BATCH_SIZE = 100

/** 任务状态 → 文案 / 语义色（色 + 图标 + 文字三重编码，不单靠颜色） */
export const JOB_STATUS_META = {
  IDLE: { label: '待开始', tone: 'neutral' },
  RUNNING: { label: '运行中', tone: 'info' },
  PAUSED: { label: '已暂停', tone: 'warning' },
  DONE: { label: '完成', tone: 'success' },
  FAILED: { label: '失败', tone: 'danger' },
}

export const useScanStore = defineStore('scan', () => {
  const jobs = ref([])
  const total = ref(0)
  const page = ref(1)
  const size = ref(20)
  const loading = ref(false)

  /** 当前任务进度（六个计数全部来自服务端，不是本地估算） */
  const progress = ref(null)
  const polling = ref(false)

  const batchSize = ref(DEFAULT_BATCH_SIZE)
  /** 根目录只读展示：后端 photo_dir()，界面上不可改（改目录=换库） */
  const rootPath = ref('')

  /** 本批进度百分比（真实计数算出来的，不是动画值） */
  const batchPercent = computed(() => {
    const processed = Number(progress.value?.batchProcessed ?? 0)
    const sizeOfBatch = Number(progress.value?.batchSize ?? batchSize.value)
    if (!sizeOfBatch) return 0
    return Math.min(100, Math.round((processed / sizeOfBatch) * 100))
  })

  async function fetchJobs() {
    loading.value = true
    try {
      const data = await listJobs({ page: page.value, size: size.value })
      jobs.value = data?.items ?? []
      total.value = data?.total ?? 0
      return data
    } finally {
      loading.value = false
    }
  }

  function start(rootPathValue = rootPath.value, autoFace = true) {
    return startScan(rootPathValue, batchSize.value, autoFace)
  }

  /** 继续下一批：wait=1 同步跑完一批，返回后计数已是最终值 */
  function resume(jobCode, wait = true) {
    return resumeScan(jobCode, { wait: wait ? 1 : 0 })
  }

  function stop(jobCode, wait = true) {
    return stopScan(jobCode, { wait: wait ? 1 : 0 })
  }

  async function refreshStatus(jobCode) {
    progress.value = await getScanStatus(jobCode)
    return progress.value
  }

  /** 开/关轮询（仅在任务处于 RUNNING 时需要） */
  function setPolling(value) {
    polling.value = Boolean(value)
  }

  function setBatchSize(value) {
    const parsed = Number(value)
    batchSize.value = Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : DEFAULT_BATCH_SIZE
  }

  return {
    jobs,
    total,
    page,
    size,
    loading,
    progress,
    polling,
    batchSize,
    rootPath,
    batchPercent,
    fetchJobs,
    start,
    resume,
    stop,
    refreshStatus,
    setPolling,
    setBatchSize,
  }
})