<!--
  P-03 照片详情（设计稿 §4.4）
  ------------------------------------------------------------
  左侧大图 + 人脸框，右侧拍摄信息与「出现的人」。

  四条硬约束
  ----------
  ① **原图只在这里出现**：网格只给缩略图，点击进入本页才发 /api/original
     （验收第 2/3 条）。本页**不提供任何编辑 / 覆盖 / 删除原图的入口** ——
     软删除只改库里的记录，磁盘上的文件一个字节都不动。
  ② **人脸框永远可见**（P0-1），四重编码（色 + 描边 + 图标 + 文字），
     描边再表达归属来源：实线=人工确认 / **虚线=机器自动（可否决）** / 点线=待确认。
  ③ **「✗ 不是他」一次点击可达**（P0-6）：人脸框悬停即出，不绕到人物详情。
  ④ **不可逆操作复述 + 二次确认**（P0-3）：软删除、标记陌生人。

  左右翻页（DR-31）
  ----------------------------------------
  ⑤ `←` `→` 与两侧浮动箭头，**默认**翻的是 `store.photos.items` 里相邻的那一张 ——
     入口仍然只有 `/photos/:photoCode`（可分享、可刷新）。
     分母用**已加载条数**而不是后端 `total`；到头**不循环**。
     ⚠️ 从人物 / 地点详情进来时，入口会带 `?scope=`，翻的是**那一批照片**
     （按需分页，往下翻自动续下一页），而不是全库 —— 否则在「白瑞琴的照片」里
     翻两张就翻到陌生人的照片上，界面上没有任何东西提示这件事发生了。
     约定见 `utils/photoReturn.js` 顶部；没有 `scope` 的入口（待确认 / 概览 /
     重复对比）行为一字不变，那几种情况仍走「不在列表里就禁用 + 给说明」。
     ⚠️ **首屏带锚点**（服务端唯一为此新增的入参 `anchorPhotoCode`）：这一批按
     `takenAt DESC` 分页，而点进来的常常是一张**老照片**（某个人的 2000 多张里
     排在第 40 页往后）。只拉第 1 页的话它不在列表里 ⇒ 两个箭头全禁用、键盘也
     不响应 —— 症状是「从人物库进来之后左右翻页坏了」而且不报错。
     服务端算出它所在的那一页，窗口从那一页向两边长（`append` / `prepend`）。

  侧栏「出现的人」（§4.4）
  ----------------------------------------
  ⑥ 每行三个出口：**详情**（去看 TA 的档案）/ **确认**（人工确认）/ **改判**
     （认错了）。「确认」把这个人在这张照片里**机器认的**（`state=disputed`）
     脸一次确认为人工归属 —— 这是浏览时最常发生的一步，原来在这张照片上
     没有入口（pending 队列里没有它们，人物详情要一页页翻才找得到）。
     ⚠️ 只在还有机器认的脸时出现：全都确认过时它点了没有任何变化，那是假入口。
     ⚠️ 走 `/review/fix` 的 `assign`（与改判同一端点、同一套重算），并且**二次确认**：
     确认会立即参与质心重算，认错了就是把质心往偏里带（见 P-05 横幅那句话）。
     ⚠️ **改判进行中禁翻**：改判会改掉这张脸的人脸框三态与侧栏「出现的人」，
       请求没回来就跳走 ⇒ 回来时状态陈旧，**而且不报错**。所以 busy 覆盖
       改判 / 确认 / 忽略 / 标记重复 / 软删除 / 旋转 / 翻页，成功后先 `reloadPhoto()` 再解禁。

     侧栏「未归属的人脸」（§4.4）
     ----------------------------------------
     ⑦ 每行两个出口：**改判**（认给某人 —— 与「出现的人」那个改判是同一个浮层，
        候选 / 全库搜索 / 新建人物都在里面）/ **忽略**（= 标记陌生人，永久排除）。
        原先并列的「确认归属」已删除：它与改判是同一件事的两套实现，能力却更少
        （不能新建人物），详见脚本里那段说明。
        每行还带 **#编号**，与图上人脸框标签上的编号**同源**（`pendingOrdinal`）——
        否则「第 2 行」在图上找不到是哪一张，清单只剩「知道有几张」的信息量。
        ⚠️ 忽略是四态里唯一**不可逆**的一档（陌生人不再进任何队列），
        所以必须二次确认（P0-3），且只做**单张**：不提供「这张照片全忽略」。

  关于 Range
  ---------
  /api/original 支持 Range（206 + Content-Range）。本页会先发一个
  `Range: bytes=0-65535` 的探测请求：① 把「分段可用」变成 Network 面板里
  **看得见**的事实（右侧「原图分段」行显示状态码与 Content-Range）；
  ② 提前把连接与首字节热起来。
  ⚠️ 缩放平移**不靠 Range 拼 JPEG 分片** —— JPEG 不整体解码就没有局部，
  那样做只会得到半张花屏。放大后拖动是 CSS 缩放 + 原生滚动，已解码的位图
  不再发任何请求。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft,
  Check,
  CircleSlash,
  Copy,
  Download,
  Maximize2,
  Pencil,
  RotateCcw,
  RotateCw,
  Search,
  Trash2,
  Undo2,
  X,
} from 'lucide-vue-next'
import FaceBox from '@/components/photo/FaceBox.vue'
import PhotoPager from '@/components/photo/PhotoPager.vue'
import ShotYearFixDialog from '@/components/photo/ShotYearFixDialog.vue'
import FixFaceDialog from '@/components/common/FixFaceDialog.vue'
import { usePhotosStore } from '@/store/photos'
import { useReviewStore } from '@/store/review'
import { useSettingsStore } from '@/store/settings'
import { listPhotos } from '@/api/browse'
// ⚠️ 地点显示名的**唯一入口**（契约：nameZh ?? placeName）。各页自己写一遍
//    `placeZh || placeName` 的后果是「GPS 地点在某页是中文、在某页是英文」,
//    而两处都"看起来正常"，谁也不会发现。
import { listPlacePhotos, placeDisplayName, placeRawName } from '@/api/place'
import {
  getDeleteImpact,
  markDuplicate,
  resetRotate,
  rotatePhoto,
  softDelete,
  unmarkDuplicate,
} from '@/api/photoAction'
import { originalUrl, thumbUrl } from '@/api/static'
// 「确认」= 人工确认（与改判同一端点，action=assign）—— 见 confirmPersonFaces
import { fixFace } from '@/api/review'
// 「返回」与「翻页范围」的约定（入口声明来源 / 详情页回退）——见文件顶部说明
import {
  goBackOr,
  inheritSource,
  parseScope,
  photoDetailLink,
  photoReturnTarget,
} from '@/utils/photoReturn'
import {
  EMPTY,
  baseName,
  displaySize,
  formatBucketKey,
  formatDate,
  formatDateTime,
  formatFileSize,
  shortHash,
} from '@/utils/format'

const route = useRoute()
const router = useRouter()
const photos = usePhotosStore()
const review = useReviewStore()
const settings = useSettingsStore()

const photo = computed(() => photos.current)
const photoCode = computed(() => String(route.params.photoCode || ''))

/**
 * 「地点」栏的显示名（R5）：**中文优先**。
 *
 * `placeDisplayName()` 同时认 `nameZh`（地点接口）与 `placeZh`（照片接口），
 * 所以行首直接传 `photo` 就够了 —— 不必在这里再抄一遍字段映射。
 *
 * ⚠️ 英文原值**照旧带出来**（tooltip）：排障时要回答「这个中文名是从哪个
 *    英文键算出来的」。丢掉它，中文名算错了就永远查不出来。
 */
const placeText = computed(() => placeDisplayName(photo.value))
const placeTooltip = computed(() => {
  const raw = placeRawName(photo.value)
  return raw ? `英文聚合键：${raw}` : undefined
})

/** 姓名解析：getPhoto 的 faces[] 与 persons[] 是分开的两份数据，要在这里对上 */
const personMap = computed(() => {
  const map = {}
  for (const person of photo.value?.persons || []) map[person.personCode] = person
  return map
})

function nameOf(face) {
  if (!face?.personCode) return ''
  return personMap.value[face?.personCode]?.displayName || face.personCode
}

const faces = computed(() => photo.value?.faces || [])
const pendingFaces = computed(() => faces.value.filter((f) => f.state === 'pending'))

/**
 * 未归属人脸的**编号**（1 起）：`faceCode -> 序号`。
 *
 * 为什么必须有一份编号
 * -------------------
 *   侧栏「还有 N 张未归属的人脸」逐行列出这些脸，图上也有 N 个点线框，
 *   但两边**没有任何共同的标识** —— 缩略图只有 24px，合影里几张侧脸几乎
 *   长得一样，用户没法回答「我说的第 2 行是图上哪一张」。于是这个清单
 *   只剩「知道有 2 张」的信息量，无法**就地下手**（要改判它得先在图上找到它）。
 *
 * 为什么编号只给 pending
 * --------------------
 *   disputed / confirmed 的脸在图上已经带着**人名**，侧栏「出现的人」也按人名
 *   成组，靠名字就能对照；给它们再编一套号只是往框上堆字（框本来就紧贴脸）。
 *
 * ⚠️ 序号**只依赖 `faces[]` 的数组顺序**（服务端给的顺序，同一张照片每次一样），
 *    图上标签与侧栏列表取的是同一个 Map ⇒ 两边不可能错位。
 *    这也是为什么不在模板里用 `index`：`FaceBox` 渲染的是**全部**脸（含已归属的），
 *    v-for 的 index 是「第几张脸」而不是「第几张待确认」，直接拿来当编号会跳号。
 */
const pendingOrdinal = computed(() => {
  const map = new Map()
  pendingFaces.value.forEach((face, i) => map.set(face.faceCode, i + 1))
  return map
})

/**
 * 这张脸的候选。
 * 能在已加载的待确认队列里对上就**用真候选**（带相似度）；对不上就退回
 * 「全部人物按姓名列出来」—— 因为任意一张脸都没有现成的相似度可给，
 * 硬编一个数字比不给更坏。
 */
function candidatesOf(face) {
  const entry = review.pendingItems.find((item) => item.faceCode === face?.faceCode)
  if (entry) return review.decorateCandidates(entry.topCandidates)
  return review.decorateCandidates(
    Object.values(review.personDirectory).map((person) => ({
      personCode: person.personCode,
      displayName: person.displayName,
      avatarFaceCode: person.avatarFaceCode,
      similarity: null,
    })),
  )
}

/** 当前照片的人工旋转角度（DR-43）：0/90/180/270，0 = 未修正 */
const rotateDeg = computed(() => Number(photo.value?.rotateDeg) || 0)
/** 旋转请求在飞（成功前禁第二下 —— 连点两次会发两个请求、后到的赢，角度就错了） */
const rotateSaving = ref(false)

/**
 * 主图**外层**比例必须等于**显示**方向的比例（= 旋转后），否则人脸框会整体偏移。
 * `displaySize(photo, rotateDeg)` 是唯一的几何入口（DR-43）：
 * 它内部按 ①EXIF orientation ②人工旋转 依次转置宽高。
 */
const frameStyle = computed(() => {
  const size = displaySize(photo.value, rotateDeg.value)
  if (!size.width || !size.height) return { width: '100%' }
  return {
    aspectRatio: `${size.width} / ${size.height}`,
    width: `min(100%, calc(70vh * ${size.width} / ${size.height}))`,
  }
})

/**
 * 主图**内层**（真正被旋转的那一层）样式 —— DR-43 最关键的一处结构。
 *
 * ⚠️ 为什么必须两层（只给 <img> 加 transform 是错的）
 * ---------------------------------------------------
 *   人脸框的 `pb_face.bbox` 是**归一化 x,y,w,h**（`faceState.js` 的 parseBbox ->
 *   faceBoxStyle 转成百分比），`FaceBox` **绝对定位**叠在图上，百分比基座是
 *   它的 offsetParent。如果只转 <img>、FaceBox 留在外面 ⇒ 百分比基座没变
 *   ⇒ **框全部错位**（在 90/270 上错得最狠）。
 *   所以这里把 **img 与全部 FaceBox 一起**放进 inner，只转 inner：
 *     outer（吃旋转后比例，撑开布局）
 *       └ inner（吃未旋转比例，居中 + rotate）
 *            ├ <img>          ← 自己不单独加 transform
 *            └ <FaceBox> × N  ← 跟着一起转，百分比基座恒为未旋转的画布
 *   ⇒ bbox 语义**一个字都不用改**，一行坐标数学都不写。
 *
 * ⚠️ 90/270 时 inner 要比 outer **宽**（用未旋转的横边去覆盖旋转后的竖边）：
 *   inner.width = outer 宽度 × (未旋转宽/高)，配合 aspect-ratio 得到
 *   inner 高 = outer 宽度，转 90° 后包围盒正好 = outer 的（宽, 高）。
 *   只写 `width: 100%` 的话旋转后会**两边留空**（露出卡片底色）。
 */
const innerStyle = computed(() => {
  const deg = rotateDeg.value
  const base = displaySize(photo.value)          // 未旋转（含 EXIF 折算）
  if (!base.width || !base.height) return {}
  const style = {
    aspectRatio: `${base.width} / ${base.height}`,
    transform: `translate(-50%, -50%) rotate(${deg}deg)`,
  }
  style.width = deg === 90 || deg === 270
    ? `calc(100% * ${base.width / base.height})`
    : '100%'
  return style
})

/** 旋转后的宽高（「尺寸」那一行必须显示**旋转后**的值，与容器同源） */
const shownSize = computed(() => displaySize(photo.value, rotateDeg.value))

/**
 * 左转 / 右转 90°：角度累加 mod 360，**乐观更新 + 失败回滚**。
 *
 * ⚠️ 为什么不做单独的 180° 按钮：连点两次左转就是 180°，多一个按钮只是
 *    让「到底转了哪边」多一种可能。
 * ⚠️ 成功后写回 store 而**不整页 reload**：详情页的图/尺寸/人脸框都读
 *    `photos.current`，`setRotateDeg` 按 photoCode 命中才改 —— 这同时保证了
 *    **翻页不串角度**（翻到下一张时读的是那一张自己的值）。
 */
async function doRotate(delta) {
  if (rotateSaving.value || busy.value) return
  const previous = rotateDeg.value
  const next = ((previous + delta) % 360 + 360) % 360
  if (next === previous) return
  rotateSaving.value = true
  // 乐观更新：先让画面转起来（失败再回滚），旋转是纯显示、没有副作用可担心
  photos.setRotateDeg(photoCode.value, next)
  try {
    const data = await rotatePhoto(photoCode.value, next)
    photos.setRotateDeg(photoCode.value, Number(data?.rotateDeg ?? next))
  } catch (error) {
    photos.setRotateDeg(photoCode.value, previous)
    ElMessage.error(`旋转失败：${error?.message || '未知错误'}`)
  } finally {
    rotateSaving.value = false
  }
}

/** 重置方向（只在 rotateDeg !== 0 时出现）：写回 0，恢复扫描件自己的方向 */
async function doResetRotate() {
  if (rotateSaving.value || busy.value || rotateDeg.value === 0) return
  const previous = rotateDeg.value
  rotateSaving.value = true
  photos.setRotateDeg(photoCode.value, 0)
  try {
    await resetRotate(photoCode.value)
    photos.setRotateDeg(photoCode.value, 0)
  } catch (error) {
    photos.setRotateDeg(photoCode.value, previous)
    ElMessage.error(`重置方向失败：${error?.message || '未知错误'}`)
  } finally {
    rotateSaving.value = false
  }
}

const fileLabel = computed(() => baseName(photo.value?.relPath) || photoCode.value)

// ---- Range 探测（把「分段可用」变成看得见的事实）----
const rangeProbe = ref({ status: '', contentRange: '' })

async function probeRange() {
  if (!photoCode.value) return
  try {
    const response = await fetch(originalUrl(photoCode.value), {
      headers: { Range: 'bytes=0-65535' },
    })
    await response.arrayBuffer()
    rangeProbe.value = {
      status: String(response.status),
      contentRange: response.headers.get('Content-Range') || '',
    }
  } catch {
    rangeProbe.value = { status: '—', contentRange: '' }
  }
}

async function reloadPhoto() {
  photos.current = await photos.fetchPhoto(photoCode.value)
}

// ---- 改判浮层（三种 action 共用）----
const fixVisible = ref(false)
const fixFaces = ref([])
const fixMode = ref('assign')
const fixCandidates = ref([])
const fixPhotoLabel = ref('')
const fixLoading = ref(false)

const fixSiblings = computed(() =>
  faces.value.filter((face) => !fixFaces.value.some((f) => f.faceCode === face.faceCode)),
)

function openFix(face, mode = 'assign') {
  // ⚠️ 必须把 displayName 一起带过去：FixFaceDialog 的复述要写「当前被认成 <谁>」，
  //    而 getPhoto 的 faces[] 里**只有 personCode 没有姓名**（姓名在 persons[] 里）。
  //    漏了的话浮层会显示成「当前被认成 UI_xxxx」这种编码 —— 用户完全看不懂。
  fixFaces.value = [{ ...face, displayName: nameOf(face) }]
  fixMode.value = mode
  fixCandidates.value = candidatesOf(face)
  fixPhotoLabel.value = photo.value?.relPath || photoCode.value
  fixVisible.value = true
}

/** 侧栏「出现的人」每行的「✗ 改判」：把这个人在这张照片里的脸全带出来 */
function openFixForPerson(person) {
  const owned = faces.value.filter(
    (face) => face.personCode === person.personCode && face.state !== 'stranger',
  )
  if (!owned.length) return
  // 同上：带上姓名，否则浮层的复述里只有 personCode
  fixFaces.value = owned.map((face) => ({ ...face, displayName: person.displayName }))
  fixMode.value = 'assign'
  fixCandidates.value = candidatesOf(owned[0])
  fixPhotoLabel.value = photo.value?.relPath || photoCode.value
  fixVisible.value = true
}

/**
 * 本照片里这个人的**机器自动归属、还没人工确认**的脸（四态里的 `disputed`）。
 *
 * ⚠️ 只有这些脸才让「确认」出现：全都确认过时那个按钮点了也不会有任何变化，
 *    那是一个**假入口**（用户点两次才发现它什么都没干）。
 */
function autoFacesOf(person) {
  return faces.value.filter(
    (face) => face.personCode === person.personCode && face.state === 'disputed',
  )
}

/** 正在确认的那个人（按钮 loading + 挡翻页，见 busy） */
const confirmingPerson = ref('')

/**
 * 「确认」= **人工确认**：把这个人在这张照片里机器认的脸一次性确认为人工归属。
 *
 * 为什么这一格必须有（设计稿 §4.4「出现的人」）
 * ----------------------------------------
 *   这一栏原来只有「详情」（去看 TA 的档案）与「改判」（认错了）。而**最常发生的
 *   那一步 —— 「机器认得对，我确认一下」 —— 在这张照片上根本没有入口**：
 *     · 待确认队列里没有它（pending 是**没有归属**的脸，disputed 已经挂在人上了）；
 *     · 人物详情页的人脸样本要一页页翻才能对上这一张。
 *   ⇒ 结果只能「放着不管」，而「机器认的」与「人工确认的」差别是
 *     **质心要不要算它**（确认完立即重算）：放着越久，识别越按没核实的样本漂。
 *
 * ⚠️ 走 `/review/fix` 的 `assign`（isConfirmed=1 + 立即重算质心 + 落日志），
 *    与「改判」同一个端点、同一套重算 —— 另写一套是漂移的来源。
 * ⚠️ 二次确认（popconfirm）：它**立刻参与质心重算**，认错了就是把质心往偏里带
 *    （P-05 横幅那句话），所以先复述「确认谁、几张」，再动手。
 */
async function confirmPersonFaces(person) {
  const targets = autoFacesOf(person)
  if (!targets.length) return
  confirmingPerson.value = person.personCode
  try {
    const data = await fixFace({
      faceCodes: targets.map((face) => face.faceCode),
      action: 'assign',
      personCode: person.personCode,
    })
    review.applyCounts(data)
    // 与改判同序：**先刷当前图，再解禁** —— 否则人脸框仍是旧的三态（实线/虚线）
    await reloadPhoto()
    ElMessage.success(
      `已人工确认：${person.displayName} 在这张照片里的 ${targets.length} 张脸`,
    )
  } finally {
    confirmingPerson.value = ''
  }
}

async function submitFix({ action, faceCodes, personCode }) {
  fixLoading.value = true
  try {
    const data = await review.fix(action, faceCodes, personCode)
    review.applyCounts(data)
    // 详情页要立刻反映改判结果（框的颜色 / 文字 /「出现的人」列表）
    await reloadPhoto()
    fixVisible.value = false
    ElMessage.success(
      action === 'unknown'
        ? '已置为未知：这些脸回到「待确认」队列'
        : action === 'stranger'
          ? '已标记为陌生人：这些脸不再出现在任何队列里'
          : '改判完成：该人照片数与两个角标已同步更新',
    )
  } finally {
    fixLoading.value = false
  }
}

async function createPersonAndAssign({ form, faceCodes }, onDone) {
  fixLoading.value = true
  try {
    await review.createPersonAndAssign(form, faceCodes)
    await reloadPhoto()
    fixVisible.value = false
    onDone?.()
    ElMessage.success('已新建人物并把该脸归属给他')
  } finally {
    fixLoading.value = false
  }
}

// ---- 忽略 = 标记陌生人（未归属的脸）----
/** 正在忽略的那一张（按钮级 loading，避免一排一起转圈） */
const strangerFaceCode = ref('')

/**
 * 「忽略」= 标记陌生人：这张脸**永久排除**，不再出现在任何队列里。
 *
 * 为什么未归属那行必须有这个出口
 * ----------------------------
 *   合影里的路人、背景里被误检的一张脸，既不属于库里任何一个人，也不该
 *   一直占着待确认队列 —— 原来这两条出路（改判 / 忽略）一条都没有：
 *   只能去「待确认」页逐条否决，或者干脆放着不管，而队列是**每天要过的**。
 *
 * ⚠️ 走 `/review/fix` 的 `stranger`（与改判同一个端点、同一条日志），
 *    它是四态里唯一**不可逆**的一档（`faceStateOf` 里陌生人不进任何队列、
 *    也不参与聚类），所以按钮上必须挂二次确认 —— 见模板里的 el-popconfirm。
 * ⚠️ **只做单张**：不做「这张照片全忽略」。这一栏里几张脸往往是不同的陌生人，
 *    一次点掉整张照片会把「其实是他本人、只是还没认出来」的那张也排除掉。
 * ⚠️ 与改判同序：**先 reloadPhoto() 刷当前图，再解禁**（否则人脸框还是旧态）。
 */
async function ignoreFace(face) {
  if (!face?.faceCode) return
  strangerFaceCode.value = face.faceCode
  try {
    const data = await review.fix('stranger', [face.faceCode])
    review.applyCounts(data)
    await reloadPhoto()
    ElMessage.success('已忽略：这张脸标记为陌生人，不再出现在任何队列里')
  } finally {
    strangerFaceCode.value = ''
  }
}

// ============================================================
// 为什么这里**没有**「确认归属」浮层（原先有，已整段移除）
// ============================================================
// 它和 `FixFaceDialog` 的 `assign` 模式是同一件事的两套实现：同一个端点
// （`/review/fix` action=assign，都会写 isConfirmed=1 + 立即重算质心 + 落日志），
// 同样要用户「搜人名字 → 选一个人」。
//
// 两套实现的直接后果不是代码冗余，而是**能力漂移**：改判浮层里能「新建人物并归属」
// （库里还没有这个人时的唯一出路），而确认归属浮层里没有 —— 于是「未归属那行点哪个
// 按钮」竟然决定了能不能建人；反过来，确认归属的搜索框/空态文案又是第三份。
//
// ⇒ 未归属的脸（pending）与机器认的脸（disputed）现在走**同一个**浮层：
//   侧栏那行的「改判」→ FixFaceDialog(assign)，候选 + 全库搜索 + 新建人物都在里面。

// ---- 标记重复（选一张作为主照片）----
const dupVisible = ref(false)
const dupKeyword = ref('')
const dupCandidates = ref([])
const dupLoading = ref(false)
const dupSaving = ref(false)

async function searchDupCandidates() {
  dupLoading.value = true
  try {
    const data = await listPhotos({ page: 1, size: 20, keyword: dupKeyword.value || undefined })
    dupCandidates.value = (data?.items || []).filter((one) => one.photoCode !== photoCode.value)
  } finally {
    dupLoading.value = false
  }
}

function openDup() {
  dupKeyword.value = ''
  dupCandidates.value = []
  dupVisible.value = true
  searchDupCandidates()
}

async function saveDup(target) {
  dupSaving.value = true
  try {
    await markDuplicate(photoCode.value, target.photoCode)
    await reloadPhoto()
    dupVisible.value = false
    ElMessage.success('已标记为重复：不删除任何文件，随时可取消')
  } finally {
    dupSaving.value = false
  }
}

async function doUnmarkDup() {
  dupSaving.value = true
  try {
    await unmarkDuplicate(photoCode.value)
    await reloadPhoto()
    ElMessage.success('已取消重复标记')
  } finally {
    dupSaving.value = false
  }
}

// ---- 软删除（两段式：先看影响面，再 confirm）----
const delVisible = ref(false)
const delImpact = ref(null)
const delLoading = ref(false)

async function openDelete() {
  delLoading.value = true
  try {
    delImpact.value = await getDeleteImpact(photoCode.value)
    delVisible.value = true
  } finally {
    delLoading.value = false
  }
}

async function doDelete() {
  delLoading.value = true
  try {
    const data = await softDelete(photoCode.value, true)
    review.applyCounts(data)
    delVisible.value = false
    ElMessage.success(
      `已软删除：${data?.facesDeleted ?? 0} 张人脸一并从队列移出。磁盘上的原图没有被改动。`,
    )
  } finally {
    delLoading.value = false
  }
}

// ---- 年代修正（DR-42）：老相册翻拍件 / 扫描件的「拍摄年代」----
/**
 * 为什么这个入口必须留在照片详情页
 * ------------------------------
 *   `pb_photo.shotYear` 是**扫描时**从 EXIF / 文件名 / mtime 读出来的，而老相册的
 *   翻拍件三条链子给的都只是"翻拍那一刻"。用户看到的现象是「这个人的时间轴里
 *   混进了一张 2019」—— 而他要修的是**这张照片是哪一年拍的**，
 *   所以修的地方就该是这张照片的详情页（而不是某个人物页的拖拽）。
 */
/** 年代是不是被人工改过（决定"人工修正"角标显不显示） */
const shotYearOverridden = computed(() => {
  const value = photo.value?.shotYearOverride
  return value !== null && value !== undefined && value !== ''
})

const shotYearVisible = ref(false)

/**
 * 修正成功后：刷新本页 + 让队列角标失效。
 * ⚠️ 修正会**重算质心**（这张照片里每个人在当前年代档的向量），
 *    所以待确认队列的候选顺序也可能变了 —— 不能只刷新本页。
 * ⚠️ `warnings` 非空表示「修正本身成功了，但那个人的**其它**照片还有旧口径年代档键」
 *    （DR-22 前置检查挡下的）。这时必须说出来：静默吞掉它，
 *    用户会在几天后发现"某个人怎么认不准了"而毫无线索。
 */
async function onShotYearFixed(data) {
  await reloadPhoto()
  review.fetchBadge()
  const warnings = data?.warnings || []
  if (warnings.length) {
    ElMessage.warning(
      `年代已修正；但有 ${warnings.length} 个人的历史年代档口径待重算，`
        + '请按其提示跑一次刷新年代档（否则那个人的匹配会静默失准）。',
    )
  }
}

// ---- 缩放查看（CSS 缩放 + 原生滚动，不再发请求）----
const zoomVisible = ref(false)
const zoomPercent = ref(100)
// DR-43：放大弹窗里**没有**人脸框（裸 <img>），所以这里可以只转 img 自己 ——
// 与主图区那套「外层吃旋转后比例 + 内层包住 img 与脸框」不同，两者不能互相抄：
// 主图区若照这里只转 img，人脸框的百分比基座就错了（框全错位）。
// ⚠️ 这里转的是**同一张原图**（同一份字节），所以下载原图仍是原始方向 ——
//    弹窗看到的方向 = 用户在界面上选的方向，与原图字节无关。
const zoomStyle = computed(() => ({
  width: `${zoomPercent.value}%`,
  transform: `rotate(${rotateDeg.value}deg)`,
  transformOrigin: 'center center',
}))

// ============================================================
// 左右翻页（DR-31）
// ============================================================
// 数据源：默认是全库照片流（`store.photos.items`）；入口声明了 `?scope=` 时
// 改沿**那一批照片**翻（某人的 / 某地点的），约定见 utils/photoReturn.js 顶部。

/** 作用域列表的页大小：与照片流默认页一致（翻页是一次一张，不需要更大） */
const SCOPE_PAGE_SIZE = 60
/** 距已加载末尾还剩这么多张就补下一页，避免「箭头忽然禁用一下再恢复」 */
const SCOPE_PREFETCH_MARGIN = 12

const scopeSpec = computed(() => parseScope(route.query.scope))
const scopeItems = ref([])
const scopeTotal = ref(0)
/**
 * 已加载窗口的**第一页 / 最后一页**（页码，1 基）。
 *
 * ⚠️ 为什么是「窗口」而不是「从第 1 页开始的累计」（DR-31 补，本页最容易再漏的一条）
 *   这一批照片由服务端按 `takenAt DESC` 分页，而入口点进来的是**某一张具体的照片**：
 *   从人物详情的时间轴点开的很可能是一张**老照片**（2005 年），它在这个人的
 *   2686 张里排在第 40 页往后。旧实现只拉「第 1 页起」，于是当前照片
 *   **不在已加载列表里** ⇒ `currentIndex = -1` ⇒ 两个箭头全禁用、键盘也不响应，
 *   而界面上只有一行小字 —— 用户看到的就是「从人物库进来之后左右翻页坏了」。
 *   ⇒ 首屏用 `anchorPhotoCode` 让服务端**直接给「它所在的那一页」**（'anchor'），
 *     窗口从那一页向两边按需长出去（'append' / 'prepend'）。
 */
const scopeStartPage = ref(1)
const scopeEndPage = ref(0)
const scopeLoading = ref(false)
/** 这一批没取到（接口失败）：翻页退回全库列表，而不是留一个全禁用的空列表 */
const scopeFailed = ref(false)

/** 这一批的总页数（锚点定位拿到 total 之后才知道） */
const scopePageCount = computed(() =>
  Math.max(1, Math.ceil(scopeTotal.value / SCOPE_PAGE_SIZE)))

/** 翻页用的列表：有作用域就用那一批，否则用全库照片流 */
const pagerItems = computed(() =>
  (scopeSpec.value && !scopeFailed.value ? scopeItems.value : photos.items),
)

/**
 * 作用域列表按需分页：一个人可能有几千张照片，一次拉完既慢，也把「翻页」
 * 变成一次全表扫描。翻到接近末尾再补一页（见 ensureScopeWindow）。
 *
 * @param page 页码（1 基）
 * @param mode `anchor` 首屏（带锚点，服务端定位到当前照片所在页）
 *             `append` 往后补一页 / `prepend` 往前补一页
 */
async function loadScopePage(page, mode = 'append') {
  const spec = scopeSpec.value
  if (!spec || scopeLoading.value) return
  scopeLoading.value = true
  try {
    const params = { page, size: SCOPE_PAGE_SIZE, orderBy: 'takenAt', desc: 1 }
    // ⚠️ 锚点只用于**首屏**：翻页时列表里已经有它了，再传是白算一次
    if (mode === 'anchor') params.anchorPhotoCode = photoCode.value
    const data = spec.type === 'person'
      ? await listPhotos({ ...params, personCode: spec.code })
      : await listPlacePhotos(spec.code, { ...params, placeCodes: spec.extra || undefined })
    const list = data?.items ?? []
    // 服务端算出来的页（锚点定位时由它给出；不适用时就是入参 page）
    const got = Number(data?.page) || page
    if (mode === 'append' && scopeItems.value.length) {
      scopeItems.value = [...scopeItems.value, ...list]
      scopeEndPage.value = Math.max(scopeEndPage.value, got)
    } else if (mode === 'prepend' && scopeItems.value.length) {
      scopeItems.value = [...list, ...scopeItems.value]
      scopeStartPage.value = Math.min(scopeStartPage.value, got)
    } else {
      scopeItems.value = list
      scopeStartPage.value = got
      scopeEndPage.value = got
    }
    scopeTotal.value = Number(data?.total) || scopeItems.value.length
  } catch (e) {
    // ⚠️ 失败**不能静默**：列表空着时两个箭头全禁用，看起来就是「坏了」。
    //    标记失败 → 翻页退回全库列表，并如实说明。
    scopeFailed.value = true
    scopeItems.value = []
    scopeStartPage.value = 1
    scopeEndPage.value = 0
    ElMessage.warning('这一批照片没取到，左右翻页已退回全库列表')
  } finally {
    scopeLoading.value = false
  }
}

/** 换了一个人 / 换了一个地点 → 重新拉第一批（`key` 是字符串，避免 watch 对象引用） */
const scopeKey = computed(() => String(route.query.scope || ''))

watch(
  scopeKey,
  async (key) => {
    scopeItems.value = []
    scopeTotal.value = 0
    scopeStartPage.value = 1
    scopeEndPage.value = 0
    scopeFailed.value = false
    if (!key) return
    // ⚠️ 首屏必须带锚点：不带的话「当前照片不在第 1 页」时这里是死路 ——
    //    后面所有补页逻辑都要求列表非空（见 ensureScopeWindow）。
    await loadScopePage(1, 'anchor')
    // 锚点页到手就顺势补下一页，避免第一下按 → 时才等请求
    ensureScopeWindow()
  },
  { immediate: true },
)

/**
 * 翻到接近已加载窗口末尾时补下一页。
 * ⚠️ 只在**已经进入这一批**（列表非空 + 当前照片在列表里）时补：
 *    否则一个不在列表里的 photoCode 会触发一次没人要的请求
 *    （锚点定位失败时就是这种情形 —— 那一页确实没有它，补页也找不到）。
 */
async function ensureScopeWindow() {
  if (!scopeSpec.value || scopeFailed.value || scopeLoading.value) return
  if (!scopeItems.value.length || currentIndex.value < 0) return
  if (scopeEndPage.value >= scopePageCount.value) return
  if (scopeItems.value.length - 1 - currentIndex.value > SCOPE_PREFETCH_MARGIN) return
  await loadScopePage(scopeEndPage.value + 1, 'append')
}

/**
 * 当前照片在**已加载列表**里的下标；-1 = 不在列表里。
 *
 * ⚠️ 用 `route.params.photoCode` 而不是 `photo.value?.photoCode`：
 *   路由参数在 push 的那一刻就同步变了，而 `photos.current` 要等 fetchPhoto
 *   回来才变。用后者的话，连按 → 时第二次算出来的 target 还是**同一张**
 *   （上一页还没回来），按了没反应且不报错。
 */
const currentIndex = computed(() =>
  pagerItems.value.findIndex((one) => one.photoCode === photoCode.value),
)

/**
 * 有上一张吗。
 * ⚠️ 作用域模式下标在窗口首条（index=0）但**窗口左边界之前还有页**时，
 *    仍算「有上一张」—— 由 gotoOffset() 去 prepend 一页。
 */
const canPrev = computed(() => {
  if (currentIndex.value > 0) return true
  return currentIndex.value === 0
    && Boolean(scopeSpec.value)
    && !scopeFailed.value
    && scopeStartPage.value > 1
})

/**
 * 有下一张吗。
 * ⚠️ 作用域模式下列表是**按需分页**的：下标到了窗口末尾不代表真到头，可能只是
 *    下一页还没取 —— 此时仍算「有下一张」，由 gotoOffset() 去补一页。
 */
const canNext = computed(() => {
  if (currentIndex.value < 0) return false
  if (currentIndex.value < pagerItems.value.length - 1) return true
  return Boolean(scopeSpec.value)
    && !scopeFailed.value
    && scopeEndPage.value < scopePageCount.value
})

/**
 * 分子：**在这一批里的绝对序号**（scope 模式）/ 已加载列表里的序号（全库模式）。
 *
 * ⚠️ 为什么 scope 模式要换算成绝对序号（而不是 `currentIndex + 1`）
 *   锚点定位之后窗口可能从第 40 页开始，此时「1 / 60」会被读成「这是这批的
 *   第 1 张」—— 那是一句假话，用户对「我在哪」的判断会整体错位。
 */
const pagerIndex = computed(() => {
  if (currentIndex.value < 0) return 0
  if (scopeSpec.value && !scopeFailed.value) {
    return (scopeStartPage.value - 1) * SCOPE_PAGE_SIZE + currentIndex.value + 1
  }
  return currentIndex.value + 1
})

/**
 * 分母（DR-31 纪律③）。
 * · 全库模式（照片流 / 待确认…）：**已加载条数**。只加载 60 张而 total=2137 时
 *   写「12 / 2137」，用户会以为后面还能翻 2125 张，翻两下就到头 ——
 *   那不叫「到头了」，那叫「被骗了」。
 * · scope 模式：**这一批的真实总数**（可以按需加载到任意位置，所以它不骗人），
 *   与上面换算过的绝对序号配成「2341 / 2686」。
 */
const pagerTotal = computed(() =>
  (scopeSpec.value && !scopeFailed.value ? scopeTotal.value : pagerItems.value.length))

/** 补充提示：不在列表里 / 只加载了一部分（到上限时说实话，不写「可继续加载」） */
const pagerHint = computed(() => {
  if (currentIndex.value < 0) {
    if (scopeSpec.value) {
      return scopeLoading.value
        ? '正在读取这一批照片…'
        : '这一批照片里没有这张（可能已被筛掉），可用上面的返回键离开'
    }
    return '这张照片不在当前已加载的列表里，无法连续翻页（可回照片流或待确认队列重新进入）'
  }
  const loaded = pagerItems.value.length
  // 作用域模式：说**已载入哪一段**，而不是「已加载 N / total」——
  // 窗口可能从锚点那一页开始（比如第 40 页），此时「60 / 2686」会被读成
  // 「后面还有 2626 张没载」，其实前面的 2340 张也没载。往下翻会自动续下一页。
  if (scopeSpec.value && !scopeFailed.value) {
    const from = (scopeStartPage.value - 1) * SCOPE_PAGE_SIZE + 1
    const to = Math.min(scopeTotal.value, scopeEndPage.value * SCOPE_PAGE_SIZE)
    if (from <= 1 && to >= scopeTotal.value) return ''
    return `这批共 ${scopeTotal.value} 张，已载入第 ${from}–${to} 张（左右翻页会自动续）`
  }
  if (loaded >= photos.total) return ''
  return photos.appendCapped
    ? `已加载 ${loaded} / ${photos.total}，继续加载已达上限，请回照片流改用年份筛选或分页器`
    : `已加载 ${loaded} / ${photos.total}，滚动可继续加载`
})

/** 正在翻页：路由 push 期间挡住第二次按键，否则一次按键能连跳两格 */
const navigating = ref(false)

/**
 * 本页「有写操作在飞」。
 *
 * ⚠️ 为什么改判进行中必须禁翻（DR-31 × DR-16，本步最容易漏的一条）
 *   改判会改掉这张脸的 personCode / isStranger 与 pb_photo_person
 *   ⇒ 人脸框描边（实线/虚线/点线三态）与侧栏「出现的人」列表**都必须重画**。
 *   改判请求还没回来就按 → 跳走，回来时看到的是**陈旧状态，而且不报错** ——
 *   症状是「我明明改判了，人脸框还是绿的」。
 *   ⇒ 改判 / 忽略（陌生人）/ 标记重复 / 软删除 / 旋转 / 翻页期间一律 busy：
 *     两个箭头禁用 + 键盘不响应；改判成功后**先 reloadPhoto() 刷当前图，再解禁**
 *     （submitFix 里已经是这个顺序，不要把它挪到 reloadPhoto 之前）。
 */
const busy = computed(
  () =>
    navigating.value ||
    fixLoading.value ||
    // 「忽略」与改判同一端点，同样会改掉人脸框的三态与侧栏那两个清单
    Boolean(strangerFaceCode.value) ||
    // 「确认」（人工确认）与改判一样会改掉人脸框的三态与侧栏「出现的人」：
    // 请求没回来就翻走，回来看到的是陈旧状态而且不报错
    Boolean(confirmingPerson.value) ||
    dupSaving.value ||
    delLoading.value ||
    // 旋转请求在飞时也禁翻：角度要落到**这张**照片上，
    // 没回来就跳走会让乐观更新与后到的响应错位（症状是「转完一张按 →，下一张也躺着」）
    rotateSaving.value,
)

/** 延后到浏览器空闲时再发预取；没有 requestIdleCallback 就退化成 setTimeout */
function idle(fn) {
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(fn, { timeout: 1500 })
    return
  }
  window.setTimeout(fn, 200)
}

/**
 * 预取**下一张**的首块（Range），不整张拉 —— /api/original 已支持 Range（步骤 4）。
 * ⚠️ 只预取下一张：用户多为往下看；每按一次 → 最多一个请求，不会堆成一片。
 * ⚠️ 失败**静默忽略**：用户并没有要求看下一张，为一个他没要的结果弹错误提示是噪声。
 * ⚠️ 用裸 fetch 而不是 axios，与本文件既有的 probeRange() 一致（图片类请求不走 axios）。
 */
function prefetchNext() {
  const next = pagerItems.value[currentIndex.value + 1]
  if (!next) return
  idle(() => {
    fetch(originalUrl(next.photoCode), { headers: { Range: 'bytes=0-65535' } }).catch(() => {})
  })
}

/**
 * 返回目标：入口带了 `?from=` 就按来源回，没有就回照片流。
 * 主按钮走**浏览器回退语义**（能 back 就 back，没历史才 replace 兜底），
 * 完整约定见 `utils/photoReturn.js` 顶部。
 */
const backTarget = computed(() => photoReturnTarget(route))

function goBack() {
  goBackOr(router, backTarget.value.path)
}

async function gotoOffset(delta) {
  if (busy.value) return
  navigating.value = true
  try {
    let target = pagerItems.value[currentIndex.value + delta]
    // 作用域列表按需分页：翻到窗口的两端时先把**邻页**取回来再判断
    // （往回补是 prepend：锚点定位后窗口可能从第 40 页开始，往左同样要能续）
    if (!target && scopeSpec.value && !scopeFailed.value) {
      if (delta === 1) {
        await loadScopePage(scopeEndPage.value + 1, 'append')
      } else if (scopeStartPage.value > 1) {
        await loadScopePage(scopeStartPage.value - 1, 'prepend')
      }
      // ⚠️ 补页后 currentIndex 会跟着列表重算（prepend 时整体后移），
      //    所以这里必须**重新取**一次，不能用补页前的下标
      target = pagerItems.value[currentIndex.value + delta]
    }
    if (!target) return
    // 路由参数用 photoCode（/photos/:photoCode）：URL 可分享、可刷新
    // ⚠️ replace 而不是 push：翻 N 张会在历史里叠 N 条，
    //    页面上的「返回」和浏览器后退键都得按 N 次才回得去来源页。
    // ⚠️ query 必须一起带过去：from / fromName / scope 是「返回」与「翻哪一批」
    //    的全部依据，丢了下一次刷新就只能退回照片流。
    await router.replace({
      name: 'photoDetail',
      params: { photoCode: target.photoCode },
      query: route.query,
    })
    prefetchNext()
  } finally {
    navigating.value = false
  }
}

/**
 * ⚠️ 下面两条判据**照抄 `ReviewView.vue`**（同一套纪律在两处各写一遍，
 *    别的地方要复用就照它抄，不要另写一套 —— 判据不一致会出现「在输入框里按 ← 翻页了」）。
 */
function isTypingTarget(target) {
  if (!target) return false
  const tag = String(target.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable
}

/** 任何 EP 浮层打开时都不响应（放大查看 / 改判浮层打开时按 ← 不该翻页） */
function overlayOpen() {
  return Boolean(document.querySelector('.el-overlay:not([style*="display: none"])'))
}

function onKeydown(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return
  if (isTypingTarget(event.target)) return
  if (overlayOpen()) return
  // ⚠️ preventDefault 必须调：不调 ← / → 会触发横向滚动（验收第 10 条）
  // ⚠️ 只占用这两个键：Esc 关闭、F 全屏留给将来；↑↓ 不占用（会与缩放/滚动打架）
  if (event.key === 'ArrowLeft' && canPrev.value) {
    event.preventDefault()
    gotoOffset(-1)
    return
  }
  if (event.key === 'ArrowRight' && canNext.value) {
    event.preventDefault()
    gotoOffset(1)
    return
  }
  // DR-43：`[` 左转 90° / `]` 右转 90°。
  // ⚠️ 纪律照抄上面两条（与 ReviewView 同款）：组合键让位、输入框内不响应、
  //    浮层打开不响应 —— 少一条就会出现「在搜索框里打 [ 把照片转了」。
  // ⚠️ 与 ← / → 不同，这里没有 canPrev 那种前置条件，但 busy 必须在：
  //    旋转请求在飞时连按两次会发两个请求，后到的赢，角度就错了。
  if (event.key === '[' || event.key === ']') {
    event.preventDefault()
    doRotate(event.key === '[' ? -90 : 90)
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  review.ensurePersonDirectory()
  reloadPhoto()
  probeRange()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})

watch(photoCode, (code) => {
  if (!code) return
  reloadPhoto()
  probeRange()
  ensureScopeWindow()
  zoomVisible.value = false
  zoomPercent.value = 100
})
</script>

<template>
  <div class="pb-page space-y-4">
    <div
      v-if="photos.currentLoading && !photo"
      class="pb-card p-8 text-center text-body text-ink-weak"
    >
      正在加载照片…
    </div>

    <p
      v-else-if="!photo"
      class="pb-card px-4 py-12 text-center text-body text-ink-weak"
    >
      没有找到这张照片（可能已被软删除，或链接里的编码不对）。
    </p>

    <template v-else>
      <!-- 页头 -->
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="min-w-0">
          <!-- 两个返回，各管一件事：
               ① 主按钮 = 回退到**来的那一页**（人物详情 / 地点详情 / 待确认…），
                  和浏览器后退键同语义；没有站内上一页（刷新/分享链接）才回照片流。
               ② 次级 = 固定入口，不关心来源、就是要看全库。 -->
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-btn text-caption text-ink-sub transition-colors duration-150 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
              @click="goBack"
            >
              <ArrowLeft class="h-3.5 w-3.5" aria-hidden="true" />{{ backTarget.label }}
            </button>
            <span class="text-caption text-ink-weak" aria-hidden="true">|</span>
            <RouterLink
              to="/photos"
              class="text-caption text-ink-sub transition-colors duration-150 hover:text-ink"
              >照片流</RouterLink
            >
          </div>
          <h2 class="mt-1 flex flex-wrap items-baseline gap-x-2 text-title text-ink">
            <span>{{ formatDate(photo.takenAt) }}</span>
            <span class="font-mono text-body text-ink-sub">{{ fileLabel }}</span>
            <span
              v-if="Number(photo.isMissing) === 1"
              class="rounded-btn bg-danger-soft px-2 py-0.5 text-caption text-danger-ink"
              >原图不在磁盘上</span
            >
          </h2>
        </div>
        <div class="flex items-center gap-2">
          <el-button @click="zoomVisible = true">
            <Maximize2 class="mr-1 h-4 w-4" aria-hidden="true" />放大查看
          </el-button>
          <!-- 下载也只是「读」原图：拿一份副本，不动磁盘上那份 -->
          <a
            :href="originalUrl(photo.photoCode)"
            :download="fileLabel"
            class="inline-flex h-8 items-center rounded-btn border border-line bg-card px-3 text-body text-ink transition-colors duration-150 hover:bg-surface"
          >
            <Download class="mr-1 h-4 w-4" aria-hidden="true" />下载原图
          </a>
        </div>
      </div>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <!-- ============ 主图区 ============ -->
        <section class="pb-card p-4" aria-label="照片">
          <!-- ⚠️ 这里**不能**套 .pb-photo-frame（它 overflow:hidden）：
               人脸框的标签与「不是他」按钮要溢出框外，否则会被裁掉 -->
          <!-- group/photo：PhotoPager 的箭头靠它 hover 显形（⚠️ 用具名 group，
               不能用 group —— FaceBox 已经占了 group/face，具名 group 互不干扰） -->
          <!-- DR-43 旋转容器（结构见脚本里 innerStyle 的注释）：
               outer 吃**旋转后**的比例撑开布局；inner 吃**未旋转**的比例、
               居中并整体 rotate —— img 与全部 FaceBox 都在 inner 里，
               所以 bbox 的百分比基座恒为未旋转的画布，框永远贴脸。
               ⚠️ inner **绝不能加 overflow-hidden**：人脸框的标签与「不是他」
                  要溢出框外（与这层不套 .pb-photo-frame 同一个理由）。 -->
          <div class="group/photo relative mx-auto" :style="frameStyle">
            <div class="absolute left-1/2 top-1/2" :style="innerStyle">
              <img
                :src="originalUrl(photo.photoCode)"
                :alt="`照片 ${fileLabel} 的原图`"
                class="block h-full w-full rounded-thumb border border-line object-cover"
                draggable="false"
              />
              <FaceBox
                v-for="face in faces"
                :key="face.faceCode"
                :face="face"
                :display-name="nameOf(face)"
                :ordinal="pendingOrdinal.get(face.faceCode)"
                @fix="openFix($event, 'assign')"
              />
            </div>
            <!-- 左右翻页：DR-31。不在列表里时两个箭头自动禁用并给说明 -->
            <PhotoPager
              :index="pagerIndex"
              :total="pagerTotal"
              :can-prev="canPrev"
              :can-next="canNext"
              :disabled="busy"
              :loaded-hint="pagerHint"
              @nav="gotoOffset"
            />
          </div>

          <div class="mt-3 flex flex-wrap items-center gap-3">
            <p class="pb-hint">
              <span class="mr-2 inline-flex items-center gap-1">
                <span
                  class="inline-block h-0 w-4 border-t-2 border-solid border-success align-middle"
                  aria-hidden="true"
                />
                实线 = 人工确认
              </span>
              <span class="mr-2 inline-flex items-center gap-1">
                <span
                  class="inline-block h-0 w-4 border-t-2 border-dashed border-success align-middle"
                  aria-hidden="true"
                />
                虚线 = 机器自动认的（可否决）
              </span>
              <span class="inline-flex items-center gap-1">
                <span
                  class="inline-block h-0 w-4 border-t-2 border-dotted border-[var(--pb-warning-line)] align-middle"
                  aria-hidden="true"
                />
                点线 = 待确认
              </span>
            </p>
            <el-button size="small" @click="zoomVisible = true">
              <Maximize2 class="mr-1 h-3.5 w-3.5" aria-hidden="true" />放大
            </el-button>
            <!-- DR-43 人工旋转：只改显示，原图不动。
                 ⚠️ 不做单独的 180° 按钮 —— 连点两次左转就是 180°。 -->
            <div class="ml-auto flex items-center gap-1" role="group" aria-label="旋转方向">
              <el-button
                size="small"
                :loading="rotateSaving"
                aria-label="向左旋转 90 度"
                title="向左旋转 90 度（快捷键 [）"
                @click="doRotate(-90)"
              >
                <RotateCcw class="h-3.5 w-3.5" aria-hidden="true" />
              </el-button>
              <el-button
                size="small"
                :loading="rotateSaving"
                aria-label="向右旋转 90 度"
                title="向右旋转 90 度（快捷键 ]）"
                @click="doRotate(90)"
              >
                <RotateCw class="h-3.5 w-3.5" aria-hidden="true" />
              </el-button>
              <el-button
                v-if="rotateDeg !== 0"
                size="small"
                text
                :loading="rotateSaving"
                aria-label="重置方向（恢复原方向）"
                title="重置方向：恢复照片自己的方向"
                @click="doResetRotate"
              >
                重置方向
              </el-button>
              <!-- 角度**可见**（不只靠图标）：色盲/灰度下也能回答"现在转了多少" -->
              <span
                class="text-caption tabular-nums text-ink-weak"
                :aria-label="`当前显示角度 ${rotateDeg} 度`"
              >
                {{ rotateDeg === 0 ? '未旋转' : `已旋转 ${rotateDeg}°` }}
              </span>
            </div>
          </div>
          <p class="pb-hint mt-1">
            悬停人脸框即出「✗ 不是他」，<b>一次点击</b>就能改判。照片按真实色彩呈现，
            界面不叠加任何滤镜。
          </p>

          <!-- 四态计数：图例之外再给一次数字，色弱/灰度下也能核对 -->
          <dl class="mt-3 flex flex-wrap gap-4 border-t border-line pt-3 text-caption">
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">✓ 已确认</dt>
              <dd class="tabular-nums text-success-ink">{{ photo.stateCounts?.confirmed ?? 0 }}</dd>
            </div>
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">◐ 机器认的</dt>
              <dd class="tabular-nums text-success-ink">{{ photo.stateCounts?.disputed ?? 0 }}</dd>
            </div>
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">⚠ 待确认</dt>
              <dd class="tabular-nums text-warning-ink">{{ photo.stateCounts?.pending ?? 0 }}</dd>
            </div>
            <div class="flex items-center gap-1">
              <dt class="text-ink-weak">⊘ 陌生人</dt>
              <dd class="tabular-nums text-ink-weak">{{ photo.stateCounts?.stranger ?? 0 }}</dd>
            </div>
          </dl>
        </section>

        <!-- ============ 侧栏 ============ -->
        <aside class="space-y-4">
          <section class="pb-card p-4" aria-labelledby="pd-exif-title">
            <h3 id="pd-exif-title" class="pb-section-title">拍摄信息</h3>
            <dl class="mt-3 space-y-2 text-body">
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">时间</dt>
                <dd class="text-right tabular-nums text-ink">{{ formatDateTime(photo.takenAt) }}</dd>
              </div>
              <!-- R8 / DR-42：年代 + 人工修正入口。
                   老相册翻拍件的 shotYear 是"翻拍那一刻"，所以这里必须能改。 -->
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">年代</dt>
                <dd class="text-right text-ink">
                  <span class="tabular-nums">{{ photo.shotYear ?? EMPTY }}</span>
                  <span
                    v-if="shotYearOverridden"
                    class="ml-1 text-caption text-brand-ink"
                    :title="`扫描时读到的年份：${photo.shotYearExif ?? '未读到'}（已人工修正）`"
                    >人工修正</span
                  >
                  <el-button size="small" text @click="shotYearVisible = true">
                    <Pencil class="h-3.5 w-3.5" aria-hidden="true" />修正
                  </el-button>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">相机</dt>
                <dd class="truncate text-right text-ink">{{ photo.cameraModel || EMPTY }}</dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">尺寸</dt>
                <dd class="text-right tabular-nums text-ink">
                  <!-- DR-43：这里显示的是**最终显示方向**的宽高（含 EXIF 与人工旋转
                       两次折算）—— 与主图容器同源（shownSize），不能各算一份。 -->
                  {{ shownSize.width || EMPTY }} × {{ shownSize.height || EMPTY }}
                  <span class="text-ink-weak">（{{ formatFileSize(photo.fileSize) }}）</span>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">地点</dt>
                <!-- R5：显示**中文**（nameZh ?? placeName）；英文原值进 tooltip -->
                <dd class="text-right text-ink" :title="placeTooltip">
                  {{ placeText }}
                  <span v-if="photo.gps?.lat" class="block text-caption tabular-nums text-ink-weak">
                    {{ photo.gps.lat }}, {{ photo.gps.lon }}
                  </span>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">路径</dt>
                <dd
                  class="truncate text-right font-mono text-caption text-ink-sub"
                  :title="photo.relPath"
                >
                  {{ photo.relPath }}
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">文件 hash</dt>
                <dd
                  class="truncate text-right font-mono text-caption text-ink-sub"
                  :title="photo.fileHash"
                >
                  {{ shortHash(photo.fileHash, 10, 8) }}
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">原图分段</dt>
                <dd class="text-right text-caption text-ink-sub">
                  <template v-if="rangeProbe.status === '206'">
                    <span class="text-success-ink">Range 生效</span>
                    <span class="tabular-nums"> · {{ rangeProbe.contentRange }}</span>
                  </template>
                  <template v-else-if="rangeProbe.status">状态 {{ rangeProbe.status }}</template>
                  <template v-else>探测中…</template>
                </dd>
              </div>
            </dl>
          </section>

          <!-- 出现的人 -->
          <section class="pb-card p-4" aria-labelledby="pd-persons-title">
            <h3 id="pd-persons-title" class="pb-section-title">出现的人</h3>

            <ul v-if="(photo.persons || []).length" class="mt-3 space-y-2">
              <li
                v-for="person in photo.persons"
                :key="person.personCode"
                class="flex items-center gap-2"
              >
                <img
                  v-if="person.thumbUrl"
                  :src="person.thumbUrl"
                  class="h-8 w-8 shrink-0 rounded-full border border-line object-cover"
                  alt=""
                />
                <span
                  v-else
                  class="h-8 w-8 shrink-0 rounded-full border border-line bg-surface"
                  aria-hidden="true"
                />
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-body text-ink">{{ person.displayName }}</span>
                  <span class="block text-caption text-ink-weak">
                    <span v-if="person.confirmedFaceCount" class="text-success-ink"
                      >✓ 已确认 {{ person.confirmedFaceCount }}</span
                    >
                    <span v-if="person.autoFaceCount" class="ml-1 text-success-ink"
                      >◐ 机器认的 {{ person.autoFaceCount }}</span
                    >
                    <span v-if="!person.confirmedFaceCount && !person.autoFaceCount"
                      >这张照片里没检测到脸</span
                    >
                  </span>
                </span>
                <RouterLink
                  :to="`/people/${person.personCode}`"
                  class="shrink-0 text-caption text-brand-ink hover:underline"
                  >详情</RouterLink
                >
                <!--
                  「确认」= 人工确认（机器认的那些脸）——详情 / 改判之间缺的那一步。
                  ⚠️ 只在**还有机器认的脸**时出现：全都确认过时它点了也没有任何变化。
                -->
                <el-popconfirm
                  v-if="autoFacesOf(person).length"
                  :title="`把 ${person.displayName} 在这张照片里机器认的 ${autoFacesOf(person).length} 张脸确认为人工确认？确认后立即参与质心重算`"
                  confirm-button-text="确认"
                  cancel-button-text="取消"
                  :width="300"
                  @confirm="confirmPersonFaces(person)"
                >
                  <template #reference>
                    <el-button
                      size="small"
                      :loading="confirmingPerson === person.personCode"
                      :aria-label="`人工确认 ${person.displayName} 在这张照片里机器认的 ${autoFacesOf(person).length} 张脸`"
                    >
                      <Check class="h-3.5 w-3.5" aria-hidden="true" />确认
                    </el-button>
                  </template>
                </el-popconfirm>
                <el-button
                  size="small"
                  text
                  type="danger"
                  :aria-label="`改判 ${person.displayName} 在这张照片里的脸`"
                  @click="openFixForPerson(person)"
                >
                  <X class="h-3.5 w-3.5" aria-hidden="true" />改判
                </el-button>
              </li>
            </ul>
            <p v-else class="pb-hint mt-2">这张照片里还没有已归属的人。</p>

            <!--
              未归属的脸：单独列出。
              **改判** = 认给某人（与「出现的人」那个改判是同一个浮层：候选 /
              全库搜索 / 新建人物都在里面）；**忽略** = 标记陌生人（永久排除，
              见脚本里的 ignoreFace）。原先并列的「确认归属」已删除 ——
              它与改判是同一件事的两套实现，能力却更少（不能新建人物）。
            -->
            <div v-if="pendingFaces.length" class="mt-3 border-t border-line pt-3">
              <p class="pb-hint">
                还有 {{ pendingFaces.length }} 张未归属的人脸 ——
                <b>#编号与图上人脸框的编号一致</b>，照着编号就能找到是哪一张。
                认不出是谁（路人 / 误检）就点<b>忽略</b>。
              </p>
              <ul class="mt-2 space-y-1.5">
                <li
                  v-for="face in pendingFaces"
                  :key="face.faceCode"
                  class="flex items-center gap-2 rounded-btn bg-warning-soft px-2 py-1.5"
                >
                  <img
                    v-if="face.thumbUrl"
                    :src="face.thumbUrl"
                    class="h-6 w-6 shrink-0 rounded-full border border-line object-cover"
                    alt=""
                  />
                  <!-- 编号与图上标签同源（pendingOrdinal），不是另算一遍 -->
                  <span class="shrink-0 text-caption font-medium tabular-nums text-warning-ink"
                    >#{{ pendingOrdinal.get(face.faceCode) }}</span
                  >
                  <!--
                    行内只留 ⚠ + 年代档：这行现在要同时容下两个按钮，再把
                    「待确认」三个字写进来的话，整个年代档会被挤成省略号
                    （「（2000–…」比不显示更难看）。而状态在这一栏里已经说了两遍 ——
                    小节标题「还有 N 张未归属的人脸」+ 整行 warning 底色，逐行重复一遍
                    换来的只是把**年代档**挤掉。⚠ 保留：它与图例「⚠ 待确认」、
                    图上标签「⚠ 未归属」是同一个字形。
                  -->
                  <span class="min-w-0 flex-1 truncate text-caption text-warning-ink">
                    <span aria-hidden="true">⚠</span>
                    <span v-if="face.shotBucket" class="ml-1 tabular-nums"
                      >（{{ formatBucketKey(face.shotBucket) }}）</span
                    >
                  </span>
                  <el-button
                    size="small"
                    :loading="fixLoading"
                    :aria-label="`改判第 ${pendingOrdinal.get(face.faceCode)} 张未归属的人脸`"
                    @click="openFix(face, 'assign')"
                  >
                    改判
                  </el-button>
                  <!--
                    忽略 = 标记陌生人（不可逆语义，P0-3 要求先复述再动手）。
                    用 popconfirm 而不是 ReviewView 那种整块弹窗：这里一次只处理
                    一张脸、而且常常要连着点好几张，弹窗会变成一路「确认」的噪声。
                    标题里**必须**写清「永久排除」——它是四态里唯一回不去的一档。
                  -->
                  <el-popconfirm
                    :title="`把第 ${pendingOrdinal.get(face.faceCode)} 张脸标为陌生人？它会永久排除：不再出现在任何队列、也不参与聚类。认错人请用「改判」`"
                    confirm-button-text="确认忽略"
                    cancel-button-text="取消"
                    :width="300"
                    @confirm="ignoreFace(face)"
                  >
                    <template #reference>
                      <el-button
                        size="small"
                        text
                        type="danger"
                        :loading="strangerFaceCode === face.faceCode"
                        :aria-label="`忽略第 ${pendingOrdinal.get(face.faceCode)} 张未归属的人脸（标记为陌生人，永久排除）`"
                      >
                        <CircleSlash class="mr-1 h-3.5 w-3.5" aria-hidden="true" />忽略
                      </el-button>
                    </template>
                  </el-popconfirm>
                </li>
              </ul>
            </div>
          </section>

          <!-- 操作 -->
          <section class="pb-card p-4" aria-labelledby="pd-actions-title">
            <h3 id="pd-actions-title" class="pb-section-title">操作</h3>
            <div class="mt-3 flex flex-wrap gap-2">
              <el-button
                v-if="Number(photo.duplicates?.isDuplicate) === 1"
                :loading="dupSaving"
                @click="doUnmarkDup"
              >
                <Undo2 class="mr-1 h-4 w-4" aria-hidden="true" />取消重复标记
              </el-button>
              <el-button v-else @click="openDup">
                <Copy class="mr-1 h-4 w-4" aria-hidden="true" />标记重复
              </el-button>
              <el-button type="danger" plain :loading="delLoading" @click="openDelete">
                <Trash2 class="mr-1 h-4 w-4" aria-hidden="true" />软删除
              </el-button>
            </div>
            <p v-if="photo.duplicates?.dupOfPhotoCode" class="pb-hint mt-2">
              已标记为
              <!-- 同页跳转：replace + 沿用当前来源，返回时仍回「来的那一页」 -->
              <RouterLink
                :to="photoDetailLink(photo.duplicates.dupOfPhotoCode, inheritSource(route))"
                replace
                class="text-brand-ink hover:underline"
                >另一张照片</RouterLink
              >
              的副本。
            </p>
            <p class="pb-hint mt-3">
              本工具不提供编辑或覆盖原图的入口：所有操作只改数据库与缓存，磁盘上的原图始终只读。
              软删除也只标记库里的记录，重新扫描即可恢复。
            </p>
          </section>
        </aside>
      </div>
    </template>

    <!-- ============ 改判浮层（三种 action 共用）============ -->
    <FixFaceDialog
      v-model="fixVisible"
      :faces="fixFaces"
      :siblings="fixSiblings"
      :candidates="fixCandidates"
      :search-fn="review.searchPersons"
      :mode="fixMode"
      :loading="fixLoading"
      :photo-label="fixPhotoLabel"
      :threshold-low="settings.matchThresholdLow"
      :threshold-high="settings.matchThresholdHigh"
      @submit="submitFix"
      @create-person="createPersonAndAssign"
    />

    <!-- ============ 年代修正（DR-42）============ -->
    <ShotYearFixDialog
      v-model="shotYearVisible"
      :photo="photo"
      @done="onShotYearFixed"
    />

    <!-- ============ 标记重复（选一张作为主照片）============ -->
    <el-dialog v-model="dupVisible" :title="`把「${fileLabel}」标成谁的副本？`" width="560px">
      <el-input
        v-model="dupKeyword"
        placeholder="按路径 / 机型 / 地点搜主照片"
        clearable
        size="small"
        aria-label="搜索主照片"
        @keyup.enter="searchDupCandidates"
      >
        <template #prefix>
          <Search class="h-3.5 w-3.5 text-ink-weak" aria-hidden="true" />
        </template>
      </el-input>
      <ul class="mt-3 max-h-72 divide-y divide-line overflow-y-auto rounded-btn border border-line">
        <li v-for="candidate in dupCandidates" :key="candidate.photoCode">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface"
            :disabled="dupSaving"
            @click="saveDup(candidate)"
          >
            <img
              :src="thumbUrl(candidate.photoCode, 200)"
              class="h-10 w-10 shrink-0 rounded-thumb border border-line object-cover"
              alt=""
              loading="lazy"
            />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-body text-ink">{{ baseName(candidate.relPath) }}</span>
              <span class="block truncate text-caption text-ink-weak">
                {{ formatDate(candidate.takenAt) }} · {{ placeDisplayName(candidate) }}
              </span>
            </span>
            <span class="shrink-0 text-caption text-brand-ink">选它</span>
          </button>
        </li>
      </ul>
      <p v-if="!dupLoading && !dupCandidates.length" class="pb-hint mt-2">没有搜到候选照片。</p>
      <p class="pb-hint mt-2">
        重复标记只是打一个指针（<code>dupOfPhotoCode</code>），
        <b>不会删除任何文件</b>，随时可以取消。
      </p>
      <template #footer>
        <el-button @click="dupVisible = false">取消</el-button>
        <el-button :loading="dupLoading" @click="searchDupCandidates">重新搜索</el-button>
      </template>
    </el-dialog>

    <!-- ============ 软删除：两段式（先看影响面，再确认）============ -->
    <el-dialog
      v-model="delVisible"
      :title="`确认软删除「${fileLabel}」？`"
      width="480px"
      :close-on-click-modal="false"
    >
      <p class="text-body text-ink-sub">软删除后会发生：</p>
      <ul class="mt-2 list-disc space-y-1 pl-5 text-body text-ink-sub">
        <li>这张照片不再出现在照片流与人物时间轴里，库中的记录被标记为已删除</li>
        <li>
          <b>{{ delImpact?.faceCount ?? 0 }}</b> 张人脸一并从队列移出（其中待确认
          <b>{{ delImpact?.pendingFaces ?? 0 }}</b> 张、机器认的
          <b>{{ delImpact?.disputedFaces ?? 0 }}</b> 张、已确认
          <b>{{ delImpact?.confirmedFaces ?? 0 }}</b> 张）
        </li>
        <li v-if="delImpact?.personCodes?.length">
          出现过的 <b>{{ delImpact.personCodes.length }}</b> 个人物的照片数会各减 1
        </li>
        <li>
          磁盘上的原图<b>不会被删除或改动</b> —— 重新扫描一次就能完整恢复（人脸也一起回来）
        </li>
      </ul>
      <template #footer>
        <el-button :disabled="delLoading" @click="delVisible = false">取消</el-button>
        <el-button type="danger" :loading="delLoading" @click="doDelete">
          确认软删除（不动原图）
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 放大查看：CSS 缩放 + 原生滚动 ============ -->
    <el-dialog v-model="zoomVisible" title="放大查看原图" width="88%" top="4vh">
      <div class="mb-2 flex items-center gap-2">
        <span class="pb-hint">缩放</span>
        <el-radio-group v-model="zoomPercent" size="small" aria-label="缩放比例">
          <el-radio-button :value="50">50%</el-radio-button>
          <el-radio-button :value="100">适应</el-radio-button>
          <el-radio-button :value="200">200%</el-radio-button>
          <el-radio-button :value="400">400%</el-radio-button>
        </el-radio-group>
        <span class="pb-hint ml-2">
          拖动滚动查看局部（放大后不再发请求，用的是已解码的这张图）
        </span>
      </div>
      <div class="max-h-[76vh] overflow-auto rounded-thumb border border-line bg-photo">
        <img
          :src="originalUrl(photo?.photoCode || '')"
          :alt="`照片 ${fileLabel} 的原图（放大）`"
          class="block"
          :style="zoomStyle"
          draggable="false"
        />
      </div>
    </el-dialog>
  </div>
</template>
