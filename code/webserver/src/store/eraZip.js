/* ============================================================
 * 年代档打包 store（R15 · DR-56 ~ DR-58）
 * ============================================================
 * 为什么单开一个 store 而不是并进 persons.js
 * ----------------------------------------
 *   persons.js 管的是「这个人是谁、有多少照片」这类**档案事实**；
 *   本模块管的是「哪一档正在打成 zip、打到第几张」这类**瞬时任务态**。
 *   混在一起会让「换人时该清哪些」变成一道需要逐字段判断的题 ——
 *   而漏清一个字段的症状是「A 的打包进度条画在 B 的页面上」，
 *   它看起来完全合理，只是错的。
 *
 * 轮询放在 store 而不是组件（与 store/settings.js 的重算任务同一手法）：
 *   换 Tab、翻页都不会中断进度；组件只负责「什么时候开始 / 结束看」。
 *
 * ⚠️ 后端**全局同时只允许一个打包任务**（用户 Q-1 选定），所以这里也是
 *    单任务状态（task 只有一份），不是「每档一个进度」。
 * ============================================================ */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getEraZipStatus, listEraZips, startEraZip, stopEraZip } from '@/api/eraZip'

/** 轮询间隔（毫秒）。后端进度按张数上报并做了节流，1.2 秒足够实时。 */
const POLL_INTERVAL = 1200

export const useEraZipStore = defineStore('eraZip', () => {
  /** 当前列表对应的 personCode（换人时清空，防止串档） */
  const personCode = ref('')
  /** 各年代档的打包现状（后端原样，含 timelineKey / photoTotal / zipSize / stale） */
  const buckets = ref([])
  /** `exports\` 目录占用（字节数 + 文件数）—— 只报数，不清理（DR-24） */
  const exportsUsage = ref(null)
  /** 列表加载中 */
  const loading = ref(false)
  /** 最近一次任务快照（含终态，供界面显示结果）；null = 本次会话还没打过 */
  const task = ref(null)

  /** 轮询定时器与它对应的 jobCode（模块内，不进响应式） */
  let timer = null

  /** 是否正在打包 */
  const running = computed(() => String(task.value?.state || '') === 'BUILDING')
  /** 是否已有产物（本次会话打过并成功） */
  const ready = computed(() => String(task.value?.state || '') === 'READY')

  /**
   * 取某个**时间轴档键**对应的打包项。
   *
   * ⚠️ 必须用后端给的 `timelineKey` 来匹配，不能拿 `bucketKey` 直接比：
   *    时间轴把「无拍摄年份」写作 `(无拍摄年份)`，而打包侧要的是 `NOYEAR`
   *    （它得进 URL 与文件名）。两边都用后端给的键，前端不自己翻译。
   */
  function bucketOf(timelineKey) {
    const key = String(timelineKey || '')
    return buckets.value.find((one) => String(one.timelineKey || '') === key) || null
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  /** 拉取档位列表（人物详情进入 Tab1 / 打包结束后刷新都走它） */
  async function fetchBuckets(code) {
    const target = String(code || '')
    if (!target) return
    personCode.value = target
    loading.value = true
    try {
      const data = await listEraZips(target)
      buckets.value = data?.buckets || []
      exportsUsage.value = data?.exports || null
      const serverTask = data?.running || null
      if (serverTask) {
        task.value = serverTask
        // 页面刷新 / 换 Tab 回来时，服务端可能还在打 —— 把轮询接上，
        // 否则用户会看到一个永远停在「打包中」的界面（只能靠刷新救）。
        if (String(serverTask.state || '') === 'BUILDING' && serverTask.jobCode) {
          startPolling(serverTask.jobCode)
        }
      }
    } finally {
      loading.value = false
    }
  }

  /**
   * 起一次打包。返回后端响应（调用方据此决定「直接下载」还是「等轮询」）。
   *
   * ⚠️ 不要在这里自动下载：下载是 DOM 动作（建 `<a>` 再 click），
   *    属于视图层的活。store 只负责状态。
   */
  async function start(code, bucketKey, force = false) {
    const info = await startEraZip(code, bucketKey, force)
    if (info?.started && info?.jobCode) {
      task.value = {
        jobCode: info.jobCode,
        state: 'BUILDING',
        personCode: code,
        bucketKey: info.bucketKey,
        bucketText: info.bucketText,
        total: Number(info.total) || 0,
        processed: 0,
        percent: 0,
        missing: 0,
        zipSize: 0,
        errMsg: '',
      }
      startPolling(info.jobCode)
    }
    return info
  }

  function startPolling(jobCode) {
    const code = String(jobCode || '')
    if (!code) return
    if (timer && String(task.value?.jobCode || '') === code) return
    stopPolling()
    timer = setInterval(async () => {
      try {
        const snap = await getEraZipStatus(code)
        task.value = snap
        if (String(snap?.state || '') !== 'BUILDING') {
          stopPolling()
          // 打完立刻刷新档位列表：按钮要从「打包中」变成「下载 512 MB」
          await fetchBuckets(personCode.value)
        }
      } catch (e) {
        // 轮询失败**静默**（下一步自然会再试，不该每 1.2 秒弹一次 Toast）；
        // 404 = 这个 jobCode 已被新的任务取代 → 停，别一直问一个不存在的任务。
        if (e?.response?.status === 404 || e?.status === 404) stopPolling()
      }
    }, POLL_INTERVAL)
  }

  /** 请求停止（在张数边界生效） */
  async function stop() {
    try {
      await stopEraZip()
    } catch (e) {
      /* request.js 已弹过 Toast；这里只保证不产生未处理的 rejection */
    }
  }

  /** 换人 / 离开页面时清空（**不清会把 A 的进度画在 B 的页面上**） */
  function clear() {
    stopPolling()
    personCode.value = ''
    buckets.value = []
    task.value = null
    exportsUsage.value = null
  }

  return {
    personCode, buckets, exportsUsage, loading, task,
    running, ready,
    bucketOf, fetchBuckets, start, stop, clear, stopPolling,
  }
})
