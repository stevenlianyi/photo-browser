/* ============================================================
 * 待确认队列「点一下只发一次写请求 + 跳过不会绕回同一批」的可执行校验
 * （`npm run check:review`）
 * ============================================================
 * 为什么必须是「真的执行」而不是看代码
 * ----------------------------------
 *   这类问题**不会报错**：链路仍然是"点跳过 -> 下一张"，只是
 *     ① 中间偷偷发了 11 次串行 GET（实测 3 秒多）；
 *     ② 跳过累积到几百条后，服务端又把同一批 20 张发回来，
 *        用户就一直在那 20 张里循环（实测复现，见 §「长跑」那段）。
 *   代码怎么看都正常，只有把「请求了什么、拿回来的是不是同一批」数出来才算证据。
 *
 * 要守住的性质：
 *     ① 确认/忽略一次 = 1 次写请求，**0 次读**（本地前进）；
 *     ② 跳过 = 0 次请求，且**跨会话保留**（localStorage）；
 *     ③ 补页用**游标**（afterQuality/afterRecID），不再靠"排除手里已有的"——
 *        后者有 500 条上限，跳过多了就开始重复；
 *     ④ 「剩余」与「总数」是两个数：跳过之后剩余要减少，总数不动。
 *
 * 怎么跑：`npm run check:review`
 *   store 里用了 `@/api/*` 别名，Node 直接 import 不了，所以先用 vite 以
 *   **ssr** 模式打成 Node 可执行模块（产物在 dist-ssr/，已 gitignore），再 node 跑。
 *
 * 打桩方式：`@/api/request` 是 axios 实例的**单例模块对象**，直接把 get/put/post
 *   替换成记录函数即可 —— 不用引入测试框架，也不会碰真实网络。
 *   服务端用"质量分递减、recID 递增"的假队列，足以复现游标与排除集的行为。
 */
import { createPinia, setActivePinia } from 'pinia'
import request from '../src/api/request.js'
import { useReviewStore } from '../src/store/review.js'

const DEFERRED_KEY = 'pb.review.deferred.v1'
const DEFERRED_MAX = 300

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

// ---- 假服务端：质量分递减、recID 递增的一条队列 ----
const SERVER_ROWS = 5000
const serverRows = Array.from({ length: SERVER_ROWS }, (_, i) => ({
  faceCode: 'fc' + String(i).padStart(32, '0'),
  recID: i + 1,
  quality: Number((0.99 - i * 0.0001).toFixed(4)),
  photoCode: 'PH_' + i,
}))
/** 已处理（离队）的 faceCode —— assign/stranger 会加进来 */
const removed = new Set()
const pendingRows = () => serverRows.filter((row) => !removed.has(row.faceCode))

const calls = []
request.get = async (url, config = {}) => {
  const params = config.params || {}
  calls.push({ method: 'GET', url, params })
  if (url === '/review/pending/count') {
    return { ok: true, pendingCount: pendingRows().length, disputedCount: 0 }
  }
  if (url !== '/review/pending') return { ok: true }

  const size = Number(params.size) || 20
  const exclude = new Set(String(params.excludeFaceCodes || '').split(',').filter(Boolean))
  // 游标语义：严格排在 (quality DESC, recID ASC) 之后
  const afterRecID = params.afterRecID === undefined ? null : Number(params.afterRecID)
  const afterQuality = params.afterQuality === undefined ? null : Number(params.afterQuality)
  const rows = pendingRows().filter((row) => {
    if (exclude.has(row.faceCode)) return false
    if (afterRecID === null) return true
    if (row.quality < afterQuality) return true
    return row.quality === afterQuality && row.recID > afterRecID
  })
  const page = rows.slice(0, size)
  const skipped = String(params.skippedFaceCodes || '').split(',').filter(Boolean)
  const stillPending = new Set(pendingRows().map((row) => row.faceCode))
  const excludedPending = skipped.filter((code) => stillPending.has(code)).length
  const tail = page[page.length - 1]
  return {
    ok: true,
    items: page.map((row) => ({ faceCode: row.faceCode, photoCode: row.photoCode, topCandidates: [] })),
    total: pendingRows().length,
    skippedCount: skipped.length,
    excludedPending,
    remaining: Math.max(0, pendingRows().length - excludedPending),
    exhausted: rows.length <= size,
    scanCapped: false,
    nextQuality: tail ? tail.quality : null,
    nextRecID: tail ? tail.recID : null,
  }
}

request.put = async (url, body) => {
  calls.push({ method: 'PUT', url, body })
  const matched = String(url).match(/^\/review\/([^/]+)\/assign$/)
  if (matched) removed.add(decodeURIComponent(matched[1]))
  return { ok: true, pendingCount: pendingRows().length, disputedCount: 0,
    person: { photoCount: 3, faceCount: 5 } }
}

request.post = async (url, body, config = {}) => {
  calls.push({ method: 'POST', url, body, params: config.params || {} })
  for (const code of body?.faceCodes || []) removed.add(String(code))
  return { ok: true, pendingCount: pendingRows().length, disputedCount: 0,
    assigned: (body?.faceCodes || []).length }
}

const countOf = (method, url) =>
  calls.filter((c) => c.method === method && c.url === url).length
const lastPendingCall = () => calls.filter((c) => c.url === '/review/pending').pop()

async function main() {
  // ---- ① 干净启动：首屏 1 次读，从队首取 ----
  setActivePinia(createPinia())
  const review = useReviewStore()
  await review.fetchPending({ reset: true })
  check('首屏取页只发 1 次 GET', countOf('GET', '/review/pending'), 1)
  check('首屏 20 条', review.pendingItems.length, 20)
  check('首屏从队首取（不带游标）',
    Object.prototype.hasOwnProperty.call(calls[0].params, 'afterRecID'), false)
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

  // ---- ③ 跳过 3 次：0 次请求，但"剩余"要减少、"总数"不动 ----
  const totalBeforeSkip = review.pendingTotal
  const remainingBeforeSkip = review.remaining
  const beforeSkip = calls.length
  for (let i = 0; i < 3; i += 1) await review.skip()
  check('跳过 3 次 = 0 次请求', calls.length - beforeSkip, 0)
  check('跳过集记住 3 条', review.deferredCodes.length, 3)
  check('总数不动（跳过的脸仍在待确认里）', review.pendingTotal, totalBeforeSkip)
  check('剩余跟着减（跳过的不用再处理）', review.remaining, remainingBeforeSkip - 3)
  check('跳过已写入本机存储',
    JSON.parse(memory.get(DEFERRED_KEY) || '[]').length, 3)

  // ---- ④ 缓冲见底补页：带游标，**不带** excludeFaceCodes ----
  //      缓冲 10 条 -> 再跳 5 条到低水位 -> 触发 1 次补页
  for (let i = 0; i < 5; i += 1) await review.skip()
  const prefetch = lastPendingCall()
  check('补页带游标 afterRecID', typeof prefetch?.params?.afterRecID, 'number')
  check('补页带游标 afterQuality', typeof prefetch?.params?.afterQuality, 'number')
  check('补页不再靠"排除手里已有的"',
    Object.prototype.hasOwnProperty.call(prefetch?.params || {}, 'excludeFaceCodes'), false)
  check('补页仍上报跳过集（用于算剩余）',
    String(prefetch?.params?.skippedFaceCodes || '').split(',').filter(Boolean).length >= 3,
    true)

  // ---- ⑤ 忽略 = 1 次写 + 0 次读，且带 withCentroid=0 ----
  const beforeIgnore = calls.length
  await review.ignoreCurrent()
  const ignoreCalls = calls.slice(beforeIgnore)
  check('忽略 = 1 次 POST', ignoreCalls.filter((c) => c.method === 'POST').length, 1)
  check('忽略 = 0 次 GET', ignoreCalls.filter((c) => c.method === 'GET').length, 0)
  check('忽略不带质心明细（withCentroid=0）', ignoreCalls[0].params.withCentroid, 0)

  // ---- ⑥ 跨会话：新开 store（= 刷新）跳过记录还在，且队首取页会排除它们 ----
  const skipped = [...review.deferredCodes]
  setActivePinia(createPinia())
  const afterReload = useReviewStore()
  check('刷新后跳过记录仍在', afterReload.deferredCodes.length, skipped.length)
  const beforeReload = calls.length
  await afterReload.fetchPending({ reset: true })
  const reloadCall = calls[beforeReload]
  check('刷新后队首取页带上了跳过集',
    String(reloadCall.params.excludeFaceCodes || '').split(',').length, skipped.length)

  // ---- ⑦ 「重新显示已跳过的 N 条」清内存 + 清本机存储 ----
  await afterReload.showDeferred()
  check('重看之后跳过集清空', afterReload.deferredCodes.length, 0)
  check('本机存储也清空', JSON.parse(memory.get(DEFERRED_KEY) || '[]').length, 0)

  // ---- ⑧ 长跑（这是本次真正修的 bug）：连跳 400 次，缓冲里不许出现重复 ----
  //      旧实现到 ~500 条跳过时会开始重复，用户表现为"一直在那 20 张里循环"。
  const longRun = useReviewStore()
  await longRun.fetchPending({ reset: true })
  let duplicated = 0
  let emptyAt = -1
  for (let i = 0; i < 400; i += 1) {
    if (!longRun.current) {
      emptyAt = i
      break
    }
    await longRun.skip()
    const codes = longRun.pendingItems.map((one) => one.faceCode)
    if (new Set(codes).size !== codes.length) duplicated += 1
  }
  check('连跳 400 次：缓冲里没有重复的脸', duplicated, 0)
  check('连跳 400 次：缓冲没被跑空（队列还有的是）', emptyAt, -1)
  check('跳过集按上限截断（不无限增长）', longRun.deferredCodes.length, DEFERRED_MAX)
  check('补页请求数量与缓冲/低水位匹配（不是一次点击一页）',
    countOf('GET', '/review/pending') < 60, true)

  console.log(failed ? `\n${failed} 项不通过` : '\n全部通过')
  if (failed) process.exitCode = 1
}

main()
