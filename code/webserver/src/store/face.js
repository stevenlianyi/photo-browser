/* ============================================================
 * 人脸识别任务 store —— 进度轮询 / 待识别张数 / 环境自检
 * ============================================================
 * 为什么单独一个 store 而不是并进 scan.js
 * ---------------------------------------
 *   两者共用 pb_scan_job 表与状态机，但**计数口径不同**：
 *     · 扫描的 addedCount  = 新增入库的照片张数，pendingCount = 疑似移动张数
 *     · 人脸的 addedCount  = **检出人脸的照片张数**（合影 3 张脸算 1），
 *                          pendingCount = **新入库的人脸条数**
 *   混进同一个 store 的同一个字段里，就会出现「列表里两个任务
 *   顶着同名标签却含义不同」—— 界面上完全解释不清。
 *
 * 与 scan store 共用 JOB_STATUS_META：状态机的五种状态是同一套，
 * 不该在两处各写一份映射表（那迟早会漂移）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getFacePendingCount,
  getFaceStatus,
  listFaceJobs,
  startFace,
  stopFace,
} from '@/api/face'

export const useFaceStore = defineStore('face', () => {
  /** 人脸识别任务列表 */
  const jobs = ref([])
  const total = ref(0)
  const page = ref(1)
  const size = ref(20)
  const loading = ref(false)

  /** 当前任务进度（与扫描同构的字段名，可复用进度条组件） */
  const progress = ref(null)
  const starting = ref(false)

  /** 还没提取过特征的照片张数 + 环境自检结果 */
  const pendingPhotos = ref(0)
  /** 已提取但未归属的人脸条数 = 待确认队列（侧栏角标同一个数） */
  const pendingFaces = ref(0)
  const environment = ref({ ok: true, missing: [], optional: [], hint: '' })
  const overview = ref(null)

  /**
   * 是否可以安全地开始。
   * 缺依赖时点开始 → 子进程 import 失败 → 任务却报「完成、检出 0 张脸」，
   * 那是比"点不动"糟糕得多的结果，所以这里必须拦住并把原因显示出来。
   */
  const canStart = () => Number(pendingPhotos.value) > 0 && environment.value.ok !== false

  async function fetchOverview() {
    const data = await getFacePendingCount()
    overview.value = data || null
    pendingPhotos.value = Number(data?.pendingPhotos ?? 0)
    pendingFaces.value = Number(data?.pendingFaces ?? 0)
    environment.value = data?.environment || { ok: true, missing: [], optional: [], hint: '' }
    return data
  }

  async function fetchJobs() {
    loading.value = true
    try {
      const data = await listFaceJobs({ page: page.value, size: size.value })
      jobs.value = data?.items ?? []
      total.value = data?.total ?? 0
      return data
    } finally {
      loading.value = false
    }
  }

  async function start(options = {}) {
    starting.value = true
    try {
      const data = await startFace(options)
      await fetchOverview()
      return data
    } finally {
      starting.value = false
    }
  }

  async function stop(jobCode, wait = true) {
    return stopFace(jobCode, { wait: wait ? 1 : 0 })
  }

  async function refreshStatus(jobCode) {
    progress.value = await getFaceStatus(jobCode)
    return progress.value
  }

  return {
    jobs,
    total,
    page,
    size,
    loading,
    progress,
    starting,
    pendingPhotos,
    pendingFaces,
    environment,
    overview,
    canStart,
    fetchOverview,
    fetchJobs,
    start,
    stop,
    refreshStatus,
  }
})
