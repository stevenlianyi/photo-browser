/* ============================================================
 * 人脸匹配 store —— 跑一次匹配 / 轮询阶段 / 留住上次结果
 * ============================================================
 * 为什么与 scan / face store 长得不一样（它们满是 task 字段）
 * -------------------------------------------------------
 *   扫描与人脸识别是**可断点续跑的任务**：pb_scan_job 一行 + 批次 + 游标 +
 *   状态机，所以那两个 store 里全是 jobs / progress / pendingPhotos。
 *   匹配**不是任务**：没有 jobCode、没有批次、没有断点，跑一次就是一次
 *   「载质心 -> 比对 -> 落库」。硬套任务字段只会让界面显示一堆空值，
 *   还会让人以为它也能「继续下一批」。
 *   ⇒ 这里只维护**一份快照**（status），字段由后端 /api/match/status 给全；
 *     前端不自己拼进度、不算百分比（比对是一次矩阵乘，没有中间态，
 *     编一个百分比出来就是骗人）。
 *
 * 为什么跑完的结果要一直留在 status 里
 * -----------------------------------
 *   「给用户看」是这一步的核心诉求：点完按钮必须看得到自动归属几张、
 *   落库几条、「我不同意」涨了多少。轮询一停（running 变 false）就清空的话，
 *   界面上正好在用户需要读结果的那一刻变成空白。
 *   服务端也是这么想的（_TASK 是模块级快照，跑完不销毁），所以只要进程没重启，
 *   刷新页面也还能看到上次结果。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getMatchStatus, runMatch } from '@/api/match'

export const useMatchStore = defineStore('match', () => {
  /** 后端 /api/match/status 的完整快照；null = 还没取过 */
  const status = ref(null)
  /** 正在发「跑一次」这个请求（按钮 loading，防连点） */
  const starting = ref(false)
  /** 正在轮询状态 */
  const loading = ref(false)
  /** 「先预览，不落库」的勾选态（页面级草稿，不上服务端） */
  const preview = ref(false)

  /** 后台线程是否在跑 */
  const running = computed(() => Boolean(status.value?.running))
  const phase = computed(() => String(status.value?.phase || 'idle'))
  /** 阶段文案由后端给（唯一出处，前端不复述，避免两边漂移） */
  const phaseText = computed(() => String(status.value?.phaseText || ''))

  /** 判定分布（跑动中也有值 —— 是**已完成的部分**，不是估算） */
  const counts = computed(() => ({
    total: Number(status.value?.total) || 0,
    auto: Number(status.value?.auto) || 0,
    review: Number(status.value?.review) || 0,
    cluster: Number(status.value?.cluster) || 0,
  }))

  /** 「我不同意」的增量（跑完之后 disputedAfter - disputedBefore） */
  const disputedDelta = computed(() => {
    const one = status.value
    if (!one) return 0
    return (Number(one.disputedAfter) || 0) - (Number(one.disputedBefore) || 0)
  })

  /**
   * 预览态：算完了但**没有落库**，且确实有可落库的结果。
   * 界面据此给出「确认落库」按钮 —— 否则用户会以为机器白跑了一趟。
   * ⚠️ 必须同时要求 auto > 0：auto=0 时「确认落库」点了也不会写任何行。
   */
  const previewOnly = computed(
    () => phase.value === 'done'
      && status.value?.assign === false
      && (Number(status.value?.auto) || 0) > 0,
  )

  async function fetchStatus() {
    loading.value = true
    try {
      const data = await getMatchStatus()
      if (data) status.value = data
      return data
    } finally {
      loading.value = false
    }
  }

  /**
   * 跑一次匹配。
   * 响应本身就是一份快照（后端 dto.okBody + _snapshot），所以直接落进 status，
   * 少一次往返；随后由页面负责轮询到 running=false。
   */
  async function start({ assign = true } = {}) {
    starting.value = true
    try {
      const data = await runMatch({ assign })
      if (data) status.value = data
      return data
    } finally {
      starting.value = false
    }
  }

  return {
    status,
    starting,
    loading,
    preview,
    running,
    phase,
    phaseText,
    counts,
    disputedDelta,
    previewOnly,
    fetchStatus,
    start,
  }
})
