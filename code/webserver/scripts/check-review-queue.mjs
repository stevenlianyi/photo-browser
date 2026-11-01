/* ============================================================
 * 待确认队列「点一下只发一次写请求」的可执行校验（`npm run check:review`）
 * ============================================================
 * 为什么必须是「真的执行」而不是看代码
 * ----------------------------------
 *   这次要修的问题**不会报错**：链路仍然是"点了确认 -> 界面前进"，
 *   只是中间偷偷发了 11 次串行 GET（实测 3 秒多）。代码怎么看都正常，
 *   只有把「用户点一下之后到底发了哪些请求」数出来才算证据。
 *
 *   要守住的性质：
 *     ① 确认/忽略一次 = 1 次写请求，**0 次读**（本地前进）；
 *     ② 跳过 = 0 次请求（只记进跳过集），且**跨会话保留**（localStorage）；
 *     ③ 缓冲见底时才补 1 页，补页把「跳过的 + 手上已有的」都作为排除集发过去；
 *     ④ 「剩余」与「总数」是两个数：跳过之后剩余要跟着减少，总数不动。
 *
 * 怎么跑：`npm run check:review`
 *   store 里用了 `@/api/*` 别名，Node 直接 import 不了，所以先用 vite 以
 *   **ssr** 模式打成 Node 可执行模块（产物在 dist-ssr/，已 gitignore），再 node 跑。
 *
 * 打桩方式：`@/api/request` 是 axios 实例的**单例模块对象**，直接把 get/put/post
 *   替换成记录函数即可 —— 不用引入测试框架，也不会碰真实网络。
 *   localStorage 在 Node 里也不存在，这里用内存实现顶上（store 内部有 try/catch）。
 */
import { createPinia, setActivePinia } from 'pinia'
import request from '../src/api/request.js'
import { useReviewStore } from '../src/store/review.js'

const DEFERRED_KEY = 'pb.review.deferred.v1'

let failed = 0
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) failed += 1
  console.log('%s %s -> %s%s', ok ? '[ok]  ' : '[FAIL]', name, JSON.stringify(got),
    ok ? '' : '（期望 ' + JSON.stringify(want) + '）')
}

// ---- localStorage 打桩（Node 没有；store 内部对缺失/异常都有兜底）----
const memory = new Map()
globalThis.localStorage = {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
}

// ---- axios 打桩：把每一次请求记下来 ----
const calls = []
/** 服务端造脸的计数器 */
let seq = 0
/** 服务端待确认总数（写操作会把它减 1，模拟"处理掉一条"） */
let serverPending = 1000

request.get = async (url, config = {}) => {
  const params = config.params || {}
  calls.push({ method: 'GET', url, params })
  if (url === '/review/pending') {
    const size = Number(params.size) || 20
    const items = Array.from({ length: size }, () => {
      seq += 1
      return { faceCode: 'FC_' + seq, photoCode: 'PH_' + seq, topCandidates: [] }
    })
    // 服务端口径：remaining = 总数 - 跳过里仍待确认的（桩里"全部仍待确认"）
    const skipped = String(params.skippedFaceCodes || '').split(',').filter(Boolean)
    return {
      ok: true,
      items,
      total: serverPending,
      skippedCount: skipped.length,
      excludedPending: skipped.length,
      remaining: Math.max(0, serverPending - skipped.length),
      exhausted: false,
      scanCapped: false,
    }
  }
  if (url === '/review/pending/count') {
    return { ok: true, pendingCount: serverPending, disputedCount: 0 }
  }
  return { ok: true }
}

request.put = async (url, body) => {
  calls.push({ method: 'PUT', url, body })
  serverPending = Math.max(0, serverPending - 1)
  return { ok: true, pendingCount: serverPending, disputedCount: 0,
    person: { photoCount: 3, faceCount: 5 } }
}

request.post = async (url, body, config = {}) => {
  calls.push({ method: 'POST', url, body, params: config.params || {} })
  if (url === '/review/fix') {
    serverPending = Math.max(0, serverPending - (body.faceCodes || []).length)
    return { ok: true, pendingCount: serverPending, disputedCount: 0 }
  }
  return { ok: true, assigned: (body?.faceCodes || []).length }
}

const countOf = (method, url) =>
  calls.filter((c) => c.method === method && c.url === url).length

async function main() {
  // ---- ① 干净启动（本机没有旧跳过记录）：首屏 1 次读 ----
  setActivePinia(createPinia())
  const review = useReviewStore()
  await review.fetchPending({ reset: true })
  check('首屏取页只发 1 次 GET', countOf('GET', '/review/pending'), 1)
  check('首屏 20 条', review.pendingItems.length, 20)
  check('首屏 page 固定为 1', calls[0].params.page, 1)
  check('没有跳过集时不带 excludeFaceCodes',
    Object.prototype.hasOwnProperty.call(calls[0].params, 'excludeFaceCodes'), false)
  check('没有跳过时剩余 == 总数', review.remaining, review.pendingTotal)

  // ---- ② 连点 10 次确认：10 次 PUT，**0 次 GET** ----
  const before = calls.length
  for (let i = 0; i < 10; i += 1) {
    await review.confirmCandidate({ personCode: 'P_x', displayName: 'X' })
  }
  const afterTen = calls.slice(before)
  check('10 次确认 = 10 次 PUT', afterTen.filter((c) => c.method === 'PUT').length, 10)
  check('10 次确认 = 0 次 GET（缓冲还够，不补页）',
    afterTen.filter((c) => c.method === 'GET').length, 0)
  check('本地缓冲已摘掉 10 条', review.pendingItems.length, 10)
  check('进度数取服务端回带的 pendingCount（不是本地减）', review.pendingTotal, 990)

  // ---- ③ 跳过 3 次：0 次请求，但"剩余"要减少、"总数"不动 ----
  const beforeSkip = calls.length
  for (let i = 0; i < 3; i += 1) await review.skip()
  check('跳过 3 次 = 0 次请求', calls.length - beforeSkip, 0)
  check('跳过集记住 3 条', review.deferredCodes.length, 3)
  check('总数不动（跳过的脸仍在待确认里）', review.pendingTotal, 990)
  check('剩余跟着减（跳过的不用再处理）', review.remaining, 987)
  check('已跳过里仍待确认 3 条', review.deferredPending, 3)
  check('跳过已写入本机存储',
    JSON.parse(memory.get(DEFERRED_KEY) || '[]').length, 3)

  // ---- ④ 缓冲见底时补 1 页：排除集 + 跳过集分开报 ----
  const skipped = [...review.deferredCodes]
  await review.confirmCandidate({ personCode: 'P_x', displayName: 'X' })
  await review.confirmCandidate({ personCode: 'P_x', displayName: 'X' })
  const prefetch = calls.filter((c) => c.method === 'GET' && c.url === '/review/pending').pop()
  const excludeList = String(prefetch?.params?.excludeFaceCodes || '').split(',')
  const skippedList = String(prefetch?.params?.skippedFaceCodes || '').split(',')
  check('补页把「跳过的」作为排除集发过去',
    skipped.every((code) => excludeList.includes(code)), true)
  check('补页同时排除「手上已有的」',
    excludeList.length >= skipped.length + 1, true)
  check('skippedFaceCodes 只报"跳过的"（不混入手里的缓冲）',
    skippedList.filter(Boolean).length, 3)
  check('补页最多 1 次（不是串行翻 11 页）', countOf('GET', '/review/pending'), 2)

  // ---- ⑤ 忽略 = 1 次写 + 0 次读，且带 withCentroid=0 ----
  const beforeIgnore = calls.length
  await review.ignoreCurrent()
  const ignoreCalls = calls.slice(beforeIgnore)
  check('忽略 = 1 次 POST', ignoreCalls.filter((c) => c.method === 'POST').length, 1)
  check('忽略 = 0 次 GET', ignoreCalls.filter((c) => c.method === 'GET').length, 0)
  check('忽略不带质心明细（withCentroid=0）', ignoreCalls[0].params.withCentroid, 0)

  // ---- ⑥ 跨会话：新开一个 store（= 刷新页面）跳过记录要还在 ----
  setActivePinia(createPinia())
  const afterReload = useReviewStore()
  check('刷新后跳过记录仍在', afterReload.deferredCodes.length, 3)
  const beforeReloadFetch = calls.length
  await afterReload.fetchPending({ reset: true })
  const reloadCall = calls[beforeReloadFetch]
  check('刷新后首屏取页就把跳过集带上',
    skipped.every((code) => String(reloadCall.params.excludeFaceCodes || '').split(',').includes(code)),
    true)
  check('刷新后剩余已扣掉跳过的',
    afterReload.remaining, Math.max(0, afterReload.pendingTotal - 3))

  // ---- ⑦ 「重新显示已跳过的 N 条」要清内存 + 清本机存储 ----
  await afterReload.showDeferred()
  check('重看之后跳过集清空', afterReload.deferredCodes.length, 0)
  check('本机存储也清空', JSON.parse(memory.get(DEFERRED_KEY) || '[]').length, 0)
  check('重看时不再带排除集',
    Object.prototype.hasOwnProperty.call(
      calls[calls.length - 1].params, 'excludeFaceCodes'), false)

  console.log(failed ? `\n${failed} 项不通过` : '\n全部通过')
  if (failed) process.exitCode = 1
}

main()
