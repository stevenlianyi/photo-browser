<!--
  P-11 文件夹（`/folders`）—— R16 · DR-59~63
  ------------------------------------------------------------
  **单页双侧栏**：左侧目录树 + 右侧当前目录的缩略图网格。
  为什么不照抄「地点」的两页：本功能的主动作是**逐级下钻**，"切换目录"是最高频
  操作；两页模式每下钻一层都要跳一次页、返回一次栈，把最常见的动作做成最贵的动作。

  数据来自两个端点（都是只读）
  --------------------------
    · `GET /api/albums/path?path=` —— 目录树（整棵）+ 面包屑 + 三个计数
    · `GET /api/photos?dirPath=`   —— 当前目录**直属**的照片（复用照片列表）

  ⚠️ 三条必须在本页说清的事（否则用户会以为功能坏了）
  --------------------------------------------------
    ① 树上的张数是**直属**张数、**不含子目录** —— 所以「2013美国游 显示 3、
       它下面三个日期目录各几十张」不是少报，是两个不同的量；
    ② 磁盘上新放的目录**要扫描后**才会出现（树是从库里的 relPath 派生的，
       本页对 `photo/` 目录的访问次数是 0）；
    ③ 路径**不存在**时给完整面包屑 + 服务端那句说明，而不是一个只剩标题的空白。

  ⚠️ 根目录的表示：URL 上**不传 `path`**（干净的链接），但发给接口时换成
     保留 token（`dirPathParam`）—— 因为 `request.js` 会把空串参数整个丢掉，
     那会让"根目录"变成"全库"。范围（scope）同理，见 `dirScopeParam`。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ArrowUp,
  FolderTree,
  Images,
  PanelLeftOpen,
  RotateCw,
  ScanLine,
} from 'lucide-vue-next'
import { listAlbumPath, dirPathParam } from '@/api/album'
import { listPhotos } from '@/api/browse'
import FolderTreePanel from '@/components/common/FolderTree.vue'
import PhotoThumb from '@/components/photo/PhotoThumb.vue'
import {
  ALBUM_ROOT_TOKEN,
  currentSource,
  dirScopeParam,
  photoDetailLink,
} from '@/utils/photoReturn'
import { formatCount } from '@/utils/format'

const route = useRoute()
const router = useRouter()

/** 与 photos 流一致的页大小（后端 dto.DEFAULT_PAGE_SIZE = 60） */
const PAGE_SIZE = 60

const resp = ref(null)
const treeLoading = ref(false)
const treeError = ref('')

const photos = ref([])
const photoTotal = ref(0)
const page = ref(1)
const photosLoading = ref(false)
const photosError = ref('')

/** 窄屏（<lg）默认收起目录树；宽屏由 CSS 强制显示（见模板的 `lg:block`） */
const treeVisible = ref(false)

/** URL 上的目录键。⚠️ 保留 token 也当根目录收（书签里可能留着它） */
const current = computed(() => {
  const raw = route.query.path
  const text = String((Array.isArray(raw) ? raw[0] : raw) || '')
  return text === ALBUM_ROOT_TOKEN ? '' : text
})

const crumbs = computed(() => resp.value?.breadcrumb ?? [])
const currentNode = computed(() => ({
  name: resp.value?.pathName || '照片库',
  count: Number(resp.value?.directCount) || 0,
}))
/** 库里有照片的**目录总数**；为 0 = 整个库都还没扫描过 */
const dirTotal = computed(() => Number(resp.value?.dirTotal) || 0)
const libraryPhotoTotal = computed(() => Number(resp.value?.photoTotal) || 0)
const subDirCount = computed(() => Number(resp.value?.subDirCount) || 0)
/** 路径在库里已经不存在（照片被删 / 目录改名 / 还没扫描） */
const pathMissing = computed(() => Boolean(resp.value) && resp.value.pathExists === false)

/** 点图进照片详情时，把**当前目录**作为翻页范围传过去（R6/DR-31 的既有机制） */
const photoSource = computed(() => ({
  ...currentSource(route, currentNode.value.name || '文件夹'),
  scope: dirScopeParam(current.value),
}))

async function loadTree() {
  treeLoading.value = true
  treeError.value = ''
  try {
    resp.value = await listAlbumPath({ path: dirPathParam(current.value) })
  } catch (err) {
    // ⚠️ 失败**不能静默**：留一个空树 + 空网格，看起来就是"这个目录是空的"，
    //    而真相是接口挂了。如实说出来并给重试（与 PhotoDetailView.scopeFailed 同款）。
    resp.value = null
    treeError.value = err?.message || '目录读取失败'
  } finally {
    treeLoading.value = false
  }
}

async function loadPhotos(pageNo = 1) {
  photosLoading.value = true
  photosError.value = ''
  try {
    const body = await listPhotos({
      dirPath: dirPathParam(current.value),
      page: pageNo,
      size: PAGE_SIZE,
      orderBy: 'takenAt',
      desc: 1,
    })
    photos.value = body?.items ?? []
    photoTotal.value = Number(body?.total) || 0
    page.value = Number(body?.page) || pageNo
  } catch (err) {
    photos.value = []
    photoTotal.value = 0
    photosError.value = err?.message || '照片读取失败'
  } finally {
    photosLoading.value = false
  }
}

/** 换目录 = 两个请求一起重取（**不整页刷新**；树的展开状态由 FolderTree 自己保留） */
function loadAll(pageNo = 1) {
  return Promise.all([loadTree(), loadPhotos(pageNo)])
}

watch(current, () => { loadAll(1) }, { immediate: true })

/** 切目录：只改 URL query（根目录不写参数，保持链接干净） */
function goTo(dirKey) {
  const next = String(dirKey || '')
  if (next === current.value) return
  router.push({ path: '/folders', query: next ? { path: next } : {} })
}

function goParent() {
  const list = crumbs.value
  goTo(list.length > 1 ? list[list.length - 2].path : '')
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- ============ 页头：当前位置 + 动作 ============ -->
    <div class="space-y-2">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="min-w-0">
          <h2 class="text-title text-ink">文件夹</h2>
          <p class="pb-hint mt-1">
            按磁盘目录逐级浏览 ·
            <template v-if="dirTotal">
              共 {{ formatCount(dirTotal) }} 个目录、{{ formatCount(libraryPhotoTotal) }} 张照片
            </template>
            <template v-else>目录树来自已扫描入库的照片</template>
          </p>
        </div>
        <div class="flex items-center gap-2">
          <el-button
            class="lg:hidden"
            :aria-expanded="treeVisible ? 'true' : 'false'"
            @click="treeVisible = !treeVisible"
          >
            <FolderTree class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
            {{ treeVisible ? '收起目录树' : '目录树' }}
          </el-button>
          <el-button
            :disabled="!crumbs.length || crumbs.length < 2"
            aria-label="返回上一级目录"
            @click="goParent"
          >
            <ArrowUp class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />返回上级
          </el-button>
          <el-button :loading="treeLoading || photosLoading" aria-label="重新加载" @click="loadAll(page)">
            <RotateCw class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />刷新
          </el-button>
        </div>
      </div>

      <!-- 当前位置：每一段可点击跳回（末段是当前目录，加粗不可点） -->
      <nav v-if="crumbs.length" aria-label="当前位置" class="flex flex-wrap items-center gap-1">
        <template v-for="(crumb, index) in crumbs" :key="crumb.path || '__root__'">
          <span v-if="index > 0" class="text-ink-weak" aria-hidden="true">/</span>
          <button
            v-if="index < crumbs.length - 1"
            type="button"
            class="cursor-pointer rounded-btn px-1.5 py-0.5 text-body text-ink-sub transition-colors duration-150 hover:bg-surface hover:text-ink"
            @click="goTo(crumb.path)"
          >
            {{ crumb.name }}
            <span class="ml-1 text-caption tabular-nums text-ink-weak">
              {{ formatCount(crumb.count) }}
            </span>
          </button>
          <span v-else class="px-1.5 py-0.5 text-body font-semibold text-ink">
            {{ crumb.name }}
            <span class="ml-1 text-caption font-normal tabular-nums text-ink-weak">
              {{ formatCount(crumb.count) }} 张直属
            </span>
          </span>
        </template>
      </nav>
    </div>

    <!-- 树端点失败：如实说 + 可重试（不允许静默变成"空目录"） -->
    <section
      v-if="treeError"
      class="pb-card flex flex-wrap items-center gap-3 px-4 py-6 text-body text-danger-ink"
    >
      <span>目录读取失败：{{ treeError }}</span>
      <el-button size="small" @click="loadAll(1)">重试</el-button>
    </section>

    <div v-else class="flex items-start gap-4">
      <!-- ============ 左：目录树 ============ -->
      <aside
        class="w-full shrink-0 lg:block lg:w-[260px]"
        :class="treeVisible ? 'block' : 'hidden'"
      >
        <div class="mb-2 flex items-center justify-between lg:hidden">
          <span class="pb-hint">目录树</span>
          <button
            type="button"
            class="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-btn text-ink-sub hover:bg-surface hover:text-ink"
            aria-label="收起目录树"
            @click="treeVisible = false"
          >
            <PanelLeftOpen class="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <FolderTreePanel
          :root="resp?.root || null"
          :current="current"
          @select="goTo"
        />
      </aside>

      <!-- ============ 右：当前目录的图片 ============ -->
      <main class="min-w-0 flex-1 space-y-3">
        <!-- 空状态 1：整个库还没有照片（与"这个目录空"是两件事） -->
        <section
          v-if="!treeLoading && !dirTotal"
          class="pb-card flex flex-col items-center gap-3 px-4 py-12 text-center"
        >
          <ScanLine class="h-8 w-8 text-ink-weak" aria-hidden="true" />
          <p class="text-body text-ink">库里还没有照片，所以还没有目录。</p>
          <p class="pb-hint max-w-md">
            把照片放进照片库的 <code>photo\</code> 目录，然后到「扫描任务」里跑一次扫描 ——
            扫描之后这里才会出现目录树（本页只展示已入库的照片）。
          </p>
          <RouterLink to="/scan-jobs">
            <el-button type="primary">去扫描任务</el-button>
          </RouterLink>
        </section>

        <template v-else>
          <!-- 空状态 2：当前目录在库里已经没有了 -->
          <section
            v-if="pathMissing"
            class="pb-card space-y-2 px-4 py-6 text-body"
          >
            <p class="text-ink">这个目录在库里没有照片。</p>
            <p class="pb-hint">{{ resp?.note }}</p>
            <p class="pb-hint">
              可能的原因：照片被软删、目录被改名（旧链接就失效了），
              或者这批照片还没扫描入库。
            </p>
          </section>

          <!-- 空状态 3：目录存在、但没有**直属**图片（子目录在左侧） -->
          <section
            v-else-if="!photosLoading && !photos.length"
            class="pb-card flex flex-col items-center gap-2 px-4 py-12 text-center"
          >
            <Images class="h-8 w-8 text-ink-weak" aria-hidden="true" />
            <p class="text-body text-ink">
              这个目录没有直属图片<template v-if="subDirCount">，左侧有 {{ subDirCount }} 个子目录</template>。
            </p>
            <p class="pb-hint max-w-md">
              这里的张数只算直接放在这个目录下的照片，不含子目录 ——
              子目录在左侧单独一层，点进去就能看到。
            </p>
          </section>

          <!-- 加载失败：如实说 + 可重试 -->
          <section
            v-if="photosError"
            class="pb-card flex flex-wrap items-center gap-3 px-4 py-6 text-body text-danger-ink"
          >
            <span>照片读取失败：{{ photosError }}</span>
            <el-button size="small" @click="loadPhotos(page)">重试</el-button>
          </section>

          <!-- 网格 -->
          <template v-if="photos.length">
            <ul
              class="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
              aria-label="当前目录的图片"
            >
              <li v-for="photo in photos" :key="photo.photoCode">
                <RouterLink
                  :to="photoDetailLink(photo.photoCode, photoSource)"
                  class="block"
                  :aria-label="`打开照片详情：${photo.relPath || photo.photoCode}`"
                >
                  <PhotoThumb :photo="photo" thumb-size="200" />
                </RouterLink>
              </li>
            </ul>

            <div class="flex flex-wrap items-center justify-between gap-2 pt-1">
              <p class="pb-hint">
                共 {{ formatCount(photoTotal) }} 张（直属）·
                第 {{ formatCount((page - 1) * PAGE_SIZE + 1) }}–{{
                  formatCount(Math.min(page * PAGE_SIZE, photoTotal))
                }} 张 · 点开某张后可在该目录内用左右箭头连续翻看
              </p>
              <el-pagination
                v-if="photoTotal > PAGE_SIZE"
                layout="prev, pager, next"
                :total="photoTotal"
                :page-size="PAGE_SIZE"
                :current-page="page"
                :disabled="photosLoading"
                @current-change="loadPhotos"
              />
            </div>
          </template>
        </template>
      </main>
    </div>
  </div>
</template>
