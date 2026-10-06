/* ============================================================
 * 人脸四态语义（DR-16①）—— **全站唯一的口径出口**
 * ============================================================
 * 四态由 personCode / isConfirmed / isStranger **三个字段推导**，不新增 matchType：
 *   待确认   = personCode 为空 且 isStranger=0     -> 进「待确认」Tab
 *   我不同意 = personCode 非空 且 isConfirmed=0 且 isStranger=0 -> 进「我不同意」Tab
 *   人工确认 = isConfirmed=1
 *   陌生人   = isStranger=1（永久排除，不进队列也不聚类）
 *
 * ⚠️ 老口径「待确认 = personCode IS NULL **OR** isConfirmed=0」已废弃：
 *    那个条件会把**全部自动归属**卷入（10 万张 ≈ 4~8 万条，队列爆炸）。
 *
 * 为什么这张表放在 utils 而不是各组件里各写一份
 * ---------------------------------------------
 *   FaceBox / PhotoDetailView / ReviewView / DisputedList 四处都要渲染
 *   「这个人脸处于什么状态」。抄成四份的后果不是「代码冗余」这种可以接受的小事，
 *   而是**两处漂移**：详情页说「待确认」、队列页说「未知」，
 *   用户会以为是两张不同的脸。所以这里是**一处定义 + 四处 import**。
 *
 * 四重编码（色 + 描边 + 图标 + 文字）
 * ---------------------------------
 *   灰度打印、色盲、单色屏下都必须能分辨 —— 颜色只能是**第四条**信息，
 *   不能是唯一一条。描边样式额外承担「归属来源」这一维：
 *     实线 = 人工确认 ｜ **虚线 = 机器自动认的（可否决）** ｜ 点线 = 待确认
 */

/** 描边样式：CSS border-style 的取值 */
export const STROKE_SOLID = 'solid'
export const STROKE_DASHED = 'dashed'
export const STROKE_DOTTED = 'dotted'

/**
 * 四态元数据。
 * tone 只影响**颜色**那一维；label/icon/stroke 三维各自独立取值，
 * 所以任何一维单独失效（比如截图被转成灰度）都仍能分辨。
 */
export const FACE_STATE_META = {
  /** 人工确认：isConfirmed=1 */
  confirmed: {
    key: 'confirmed',
    label: '已确认',
    /** 悬停/常显的完整说明 */
    hint: '已确认（你亲自确认过）',
    icon: '✓',
    stroke: STROKE_SOLID,
    tone: 'success',
    /** Tailwind 类：文字色 + 底色 + 边框色 */
    textClass: 'text-success-ink',
    softClass: 'bg-success-soft',
    borderClass: 'border-success',
    /** 描边宽度：2px 是设计稿定的，人工确认最实 */
    borderWidth: '2px',
  },
  /** 机器自动归属、未确认 —— 「我不同意」列表的来源，**可否决** */
  disputed: {
    key: 'disputed',
    label: '机器认的，可否决',
    hint: '机器认的，还没你确认过 —— 可以否决',
    icon: '◐',
    stroke: STROKE_DASHED,
    tone: 'success',
    textClass: 'text-success-ink',
    softClass: 'bg-success-soft',
    borderClass: 'border-success',
    borderWidth: '2px',
  },
  /** 未归属：进待确认队列 */
  pending: {
    key: 'pending',
    label: '待确认',
    hint: '还没有归属 —— 进待确认队列',
    icon: '⚠',
    stroke: STROKE_DOTTED,
    tone: 'warning',
    textClass: 'text-warning-ink',
    softClass: 'bg-warning-soft',
    borderClass: 'border-[var(--pb-warning-line)]',
    borderWidth: '2px',
  },
  /** 陌生人：永久排除 */
  stranger: {
    key: 'stranger',
    label: '陌生人',
    hint: '已标记为陌生人，不再出现在任何队列里',
    icon: '⊘',
    stroke: STROKE_SOLID,
    tone: 'neutral',
    textClass: 'text-ink-weak',
    softClass: 'bg-surface',
    borderClass: 'border-[var(--pb-border-strong)]',
    // ⚠️ 设计稿状态表里陌生人写的是「描边：无」。但 P0-1「人脸框永远可见」
    //    是更高优先级的原则 —— 没有描边的框等于框不存在，用户会以为检测漏了脸。
    //    这里折中：**中性灰 + 1px 实线**，四维（灰/实/⊘/陌生人）都与另三态
    //    可区分，同时不与「可操作」的正色状态抢注意力。
    borderWidth: '1px',
  },
}

/** 状态未知时的兜底（不该出现，出现也要看得见而不是白框） */
const UNKNOWN_STATE = {
  key: 'unknown',
  label: '状态未知',
  hint: '状态未知（数据异常）',
  icon: '?',
  stroke: STROKE_DOTTED,
  tone: 'neutral',
  textClass: 'text-ink-weak',
  softClass: 'bg-surface',
  borderClass: 'border-[var(--pb-border-strong)]',
  borderWidth: '2px',
}

/**
 * 由一张脸推出它处于哪一态。
 *
 * 优先用服务端给的 `state`（api/browse.py 的 getPhoto 已经算好）；
 * 没有就本地按三字段推 —— 两处口径必须一致，所以**推的顺序与
 * 后端 getPhoto 里的 if/elif 完全一样**（stranger 优先于一切）。
 *
 * @param {object} face pb_face 派生的一行（至少含 personCode/isConfirmed/isStranger）
 */
export function faceStateOf(face) {
  if (!face) return UNKNOWN_STATE
  const state = face.state
  if (state && FACE_STATE_META[state]) return FACE_STATE_META[state]
  if (Number(face.isStranger) === 1) return FACE_STATE_META.stranger
  const person = String(face.personCode || '')
  if (person && Number(face.isConfirmed) !== 1) return FACE_STATE_META.disputed
  if (person) return FACE_STATE_META.confirmed
  return FACE_STATE_META.pending
}

/**
 * 描边样式（inline style 用）。描边样式是「归属来源」这一维的载体，
 * 不能省 —— 虚线与实线是用户判断「这张脸能不能否决」的唯一**非颜色**线索。
 */
export function faceStrokeStyle(meta) {
  return {
    borderStyle: meta.stroke,
    borderWidth: meta.borderWidth,
  }
}

/** 相似度文案：null 表示「没有可比质心」，必须说人话而不是显示 0.00 */
export function similarityText(value) {
  if (value === null || value === undefined || value === '') return '—'
  const num = Number(value)
  if (Number.isNaN(num)) return '—'
  return num.toFixed(2)
}

/** 人脸框几何：把 pb_face.bbox（"x,y,w,h" 归一化）转成百分比定位 */
export function faceBoxStyle(bbox) {
  if (!bbox) return null
  const parts = String(bbox)
    .split(/[,;|]/)
    .map((v) => Number(v))
  if (parts.length < 4 || parts.some((v) => Number.isNaN(v))) return null
  const [x, y, w, h] = parts
  // 库里存的是检测模型的原始框（步骤 4 明确不夹紧），这里也不夹紧 ——
  // 夹紧会让「框超出画面」这类数据问题被悄悄藏起来。越界时浏览器自己裁掉。
  return {
    left: `${x * 100}%`,
    top: `${y * 100}%`,
    width: `${w * 100}%`,
    height: `${h * 100}%`,
  }
}
