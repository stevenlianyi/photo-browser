/* ============================================================
 * axios 封装 —— 全站唯一的 HTTP 出口
 * ============================================================
 * 后端（步骤 9）给了两条统一约定，前端因此只需要这一个拦截器：
 *   · 分页：一律 { page, size, total, items }
 *   · 错误：一律非 2xx + { code, message }（错误码是字符串常量）
 *
 * 三件事在这里做掉，页面里就不用各写一遍：
 *   1. baseURL：开发环境读 VITE_API_BASE（.env.development），
 *      生产环境为空 → 走同源 /api（构建产物由后端 StaticFiles 挂在 / 上）。
 *   2. 响应拆包：直接返回 response.data，页面拿到的就是业务体。
 *   3. 统一错误提示：把后端 message 弹成 Toast，同时把 code/status/payload
 *      挂到 Error 对象上（409 DUPLICATE_DISPLAY_NAME 等分支页面要自己处理）。
 *
 * 「我不想弹 Toast」的调用方式：传 { silent: true }
 *   request.get('/api/places', { silent: true })
 *   （例如轮询失败不该每 2 秒弹一次，由页面自己决定怎么呈现）
 * ============================================================ */
import axios from 'axios'
// 深路径引入而非`from 'element-plus'`：barrel 入口会把整个 EP 拖进来
//（按需引入的意义就没了），这里只取Message 一个组件
import { ElMessage } from 'element-plus/es/components/message/index'
// ElMessage 是**命令式**调用（$message / ElMessage），unplugin-vue-components
// 只处理模板里的 <el-*>，不会给它带样式 —— 少这行则 Toast 变成无样式的裸文字
import 'element-plus/theme-chalk/el-message.css'

/** 开发环境有值；生产环境未配置 → 空串 → axios 按相对路径走（同源） */
export const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const request = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: { Accept: 'application/json' },
  /**
   * 空值不进 query string（步骤 11 修掉的第一个 bug 的**根治**处）
   * -------------------------------------------------------------
   * 症状：`hasFace=''`（store 复位成 `''` 后没走 `|| undefined`）→ FastAPI
   *      `hasFace: int` 收到空串 → 整个照片流首屏 422 白屏。
   * 以前每个 store 都要自己记得 `|| undefined`；只要有一处忘了，就是一次白屏。
   *
   * 规则**只丢** `undefined` / `null` / `''` 三种；
   * `0` 与 `false` 必须原样发出 —— `desc=0`（升序）与「没传 desc」（后端默认
   * 降序）是两个意思，把它当空值丢掉会**静默改变排序方向**且不报错。
   * （`hasFace=0`、`isDuplicate=0` 也一并保留：显式的「不过滤」与「不关心」
   *   万一哪天不同，这个转换器不会替我们做主。）
   */
  paramsSerializer: {
    serialize(params) {
      const usp = new URLSearchParams()
      for (const [key, value] of Object.entries(params || {})) {
        if (value === undefined || value === null || value === '') continue
        usp.append(key, String(value))
      }
      return usp.toString()
    },
  },
})

request.interceptors.request.use(
  (config) => {
    // 便于后端日志区分请求来源（本机单用户，纯粹为排障）
    config.headers['X-PB-Client'] = 'photo-browser-web'
    return config
  },
  (error) => Promise.reject(error),
)

/** 没连上后端时给出能直接照做的提示，而不是 "Network Error" */
function buildMessage(error) {
  const response = error.response
  if (response) {
    const payload = response.data
    if (payload && typeof payload.message === 'string' && payload.message) {
      return payload.message
    }
    return `请求失败（HTTP ${response.status}）`
  }
  if (error.code === 'ECONNABORTED') return '请求超时，后端可能正忙，请稍后再试'
  return '无法连接后端服务，请确认后端已启动（默认 127.0.0.1:8765）'
}

request.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const response = error.response
    const payload = response?.data
    const message = buildMessage(error)

    // silent 的请求不弹 Toast（轮询 / 后台刷新场景），由调用方自己处理
    if (!error.config?.silent) {
      ElMessage.error(message)
    }

    const wrapped = new Error(message)
    // 后端错误码优先（PARAM_INVALID / NOT_FOUND / DUPLICATE_DISPLAY_NAME …），
    // 网络层错误码（ERR_NETWORK）作为兜底
    wrapped.code = payload?.code || error.code || 'UNKNOWN'
    wrapped.status = response?.status ?? 0
    wrapped.payload = payload ?? null
    wrapped.silent = Boolean(error.config?.silent)
    return Promise.reject(wrapped)
  },
)

export default request