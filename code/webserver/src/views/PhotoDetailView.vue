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
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft,
  Copy,
  Download,
  Maximize2,
  Search,
  Trash2,
  Undo2,
  X,
} from 'lucide-vue-next'
import FaceBox from '@/components/photo/FaceBox.vue'
import FixFaceDialog from '@/components/common/FixFaceDialog.vue'
import { usePhotosStore } from '@/store/photos'
import { useReviewStore } from '@/store/review'
import { useSettingsStore } from '@/store/settings'
import { listPhotos } from '@/api/browse'
import {
  getDeleteImpact,
  markDuplicate,
  softDelete,
  unmarkDuplicate,
} from '@/api/photoAction'
import { faceUrl, originalUrl, thumbUrl } from '@/api/static'
import { similarityText } from '@/utils/faceState'
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
const photos = usePhotosStore()
const review = useReviewStore()
const settings = useSettingsStore()

const photo = computed(() => photos.current)
const photoCode = computed(() => String(route.params.photoCode || ''))

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

/** 主图容器比例必须等于**显示**方向的比例，否则人脸框的百分比会整体偏移 */
const frameStyle = computed(() => {
  const size = displaySize(photo.value)
  if (!size.width || !size.height) return { width: '100%' }
  return {
    aspectRatio: `${size.width} / ${size.height}`,
    width: `min(100%, calc(70vh * ${size.width} / ${size.height}))`,
  }
})

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

// ---- 确认归属（未归属的脸）----
const confirmVisible = ref(false)
const confirmFace = ref(null)
const confirmCandidates = ref([])
const confirmKeyword = ref('')
const confirmLoading = ref(false)

const confirmList = computed(() => {
  const text = String(confirmKeyword.value || '').trim().toLowerCase()
  if (!text) return confirmCandidates.value
  return confirmCandidates.value.filter((c) =>
    String(c.displayName || '').toLowerCase().includes(text),
  )
})

function openConfirm(face) {
  confirmFace.value = face
  confirmCandidates.value = candidatesOf(face)
  confirmKeyword.value = ''
  confirmVisible.value = true
}

async function doConfirm(candidate) {
  if (!confirmFace.value || !candidate?.personCode) return
  confirmLoading.value = true
  try {
    const data = await review.fix('assign', [confirmFace.value.faceCode], candidate.personCode)
    review.applyCounts(data)
    await reloadPhoto()
    confirmVisible.value = false
    ElMessage.success(`已确认为「${candidate.displayName}」，照片数与角标已更新`)
  } finally {
    confirmLoading.value = false
  }
}

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

// ---- 缩放查看（CSS 缩放 + 原生滚动，不再发请求）----
const zoomVisible = ref(false)
const zoomPercent = ref(100)
const zoomStyle = computed(() => ({ width: `${zoomPercent.value}%` }))

onMounted(() => {
  review.ensurePersonDirectory()
  reloadPhoto()
  probeRange()
})

watch(photoCode, (code) => {
  if (!code) return
  reloadPhoto()
  probeRange()
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
          <RouterLink
            to="/photos"
            class="inline-flex items-center gap-1 text-caption text-ink-sub hover:text-ink"
          >
            <ArrowLeft class="h-3.5 w-3.5" aria-hidden="true" />返回照片流
          </RouterLink>
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
          <div class="relative mx-auto" :style="frameStyle">
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
              @fix="openFix($event, 'assign')"
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
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">相机</dt>
                <dd class="truncate text-right text-ink">{{ photo.cameraModel || EMPTY }}</dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">尺寸</dt>
                <dd class="text-right tabular-nums text-ink">
                  {{ displaySize(photo).width || EMPTY }} × {{ displaySize(photo).height || EMPTY }}
                  <span class="text-ink-weak">（{{ formatFileSize(photo.fileSize) }}）</span>
                </dd>
              </div>
              <div class="flex justify-between gap-3">
                <dt class="shrink-0 text-ink-weak">地点</dt>
                <dd class="text-right text-ink">
                  {{ photo.gps?.placeName || EMPTY }}
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

            <!-- 未归属的脸：单独列出并给「确认归属」入口 -->
            <div v-if="pendingFaces.length" class="mt-3 border-t border-line pt-3">
              <p class="pb-hint">还有 {{ pendingFaces.length }} 张未归属的人脸</p>
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
                  <span class="min-w-0 flex-1 truncate text-caption text-warning-ink">
                    <span aria-hidden="true">⚠</span> 待确认
                    <span v-if="face.shotBucket" class="ml-1 tabular-nums"
                      >（{{ formatBucketKey(face.shotBucket) }}）</span
                    >
                  </span>
                  <el-button size="small" :loading="confirmLoading" @click="openConfirm(face)">
                    确认归属
                  </el-button>
                  <el-button size="small" text @click="openFix(face, 'assign')">改判</el-button>
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
              <RouterLink
                :to="`/photos/${photo.duplicates.dupOfPhotoCode}`"
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
      :mode="fixMode"
      :loading="fixLoading"
      :photo-label="fixPhotoLabel"
      :threshold-low="settings.matchThresholdLow"
      :threshold-high="settings.matchThresholdHigh"
      @submit="submitFix"
      @create-person="createPersonAndAssign"
    />

    <!-- ============ 确认归属（未归属的脸）============ -->
    <el-dialog v-model="confirmVisible" title="确认这张脸属于谁" width="480px">
      <el-input
        v-model="confirmKeyword"
        placeholder="搜索人物"
        clearable
        size="small"
        aria-label="搜索人物"
      >
        <template #prefix>
          <Search class="h-3.5 w-3.5 text-ink-weak" aria-hidden="true" />
        </template>
      </el-input>
      <p
        v-if="!confirmCandidates.length"
        class="mt-3 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        库里还没有任何人物档案可对照。先用「改判 → 新建人物」建一个并归属，
        之后同类照片就能自动比对了。
      </p>
      <ul
        v-else
        class="mt-3 max-h-72 divide-y divide-line overflow-y-auto rounded-btn border border-line"
      >
        <li v-for="candidate in confirmList" :key="candidate.personCode">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface"
            @click="doConfirm(candidate)"
          >
            <img
              v-if="candidate.avatarFaceCode"
              :src="faceUrl(candidate.avatarFaceCode)"
              class="h-7 w-7 shrink-0 rounded-full border border-line object-cover"
              alt=""
            />
            <span class="min-w-0 flex-1 truncate text-body text-ink">{{ candidate.displayName }}</span>
            <span class="shrink-0 text-caption tabular-nums text-ink-sub">{{
              similarityText(candidate.similarity)
            }}</span>
            <span class="shrink-0 text-caption text-brand-ink">确认</span>
          </button>
        </li>
      </ul>
      <p class="pb-hint mt-2">
        确认后会立即重算这个人的年代桶质心，并<b>自动前进到队列的下一条</b>。
      </p>
    </el-dialog>

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
                {{ formatDate(candidate.takenAt) }} · {{ candidate.placeName || EMPTY }}
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
