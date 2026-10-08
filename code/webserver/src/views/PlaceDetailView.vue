<!--
  P-07 地点详情（`/places/:placeCode`）—— R5 新建
  ------------------------------------------------------------
  头部：‹ 地点 + **中文名** + 归并提示（「归并 N 处」）+ 年份 + 张数
        + 同组分段下钻（全部 15 张 / 大屯 8 张 / 望京 7 张）
  两个 Tab：照片（按年分组 + 分页）/ 在场的人（实时 join）
  底部：**在场的人为空时给引导文案**（指向纠错队列），不是一片空白。

  三条不能省的语义
  --------------
  ① **归并是展示分组，不是数据合并**（DR-36）：分段下钻让用户看得出
     「这 15 张里哪 8 张来自大屯」。丢掉下钻就是「把同组的照片混成一堆而丢失来源」。
     ⚠️ 下钻时**只改子请求的 `placeCodes`**，头部（总张数、年份）不动 ——
     否则用户点一下「大屯」，页面上的「15 张」也跟着变成 8 张，
     他会以为另外 7 张丢了。
  ② **没有坐标就不显示坐标**：11/28 个目录名地点 `centerLat/Lon` 全 NULL
     （DR-34/DR-37②）。本页**不画地图**，也不显示 "0, 0"
     （那正好是几内亚湾 —— DR-25 踩过的坑）。
  ③ **照片流按年分组**，点开照片仍走 P-03（那里已有左右翻页，R6）。
     本页只做「找到那张」，不做第二套翻页 —— 两套翻页的边界条件
     （列表到末尾、筛选变了索引要重置）会各错各的。
-->
<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ArrowLeft, MapPin, RotateCw, TriangleAlert } from 'lucide-vue-next'
import PhotoThumb from '@/components/photo/PhotoThumb.vue'
import PlaceMemberPicker from '@/components/common/PlaceMemberPicker.vue'
import PlacePersonList from '@/components/common/PlacePersonList.vue'
import { getPlace, listPlacePersons, listPlacePhotos, placeDisplayName, placeRawName }
  from '@/api/place'
import { EMPTY, formatCount, formatYear } from '@/utils/format'
// 进照片详情要**声明来源与翻页范围**：详情页的「返回」与左右箭头都靠它
// （见 utils/photoReturn.js 顶部）
import { currentSource, goBackOr, photoDetailLink, scopeParam } from '@/utils/photoReturn'

const route = useRoute()
const router = useRouter()

const placeCode = computed(() => String(route.params.placeCode || ''))
/** URL 上带的「已归并同组」；详情页自己也会按 nameZh 补齐，这里只做回显 */
const queryCodes = computed(() => String(route.query.placeCodes || ''))

const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')

const activeTab = ref('photos')
/** '' = 全部（同组一起看）；否则是组内某一个 placeCode（下钻） */
const activeCode = ref('')

const photos = ref([])
const photoTotal = ref(0)
const photoYears = ref([])
const photosLoading = ref(false)
const page = ref(1)
const SIZE = 60

const persons = ref([])
const photoTotalAtPlace = ref(0)
const personsLoading = ref(false)

const members = computed(() => detail.value?.members || [])
const hasCoord = computed(() => Boolean(detail.value?.hasCoord))

/** 子请求要用的 placeCodes：下钻时只发选中的那一个 */
const activeCodes = computed(() => {
  if (activeCode.value) return activeCode.value
  return (detail.value?.placeCodes || []).filter(Boolean).join(',')
})

/**
 * 进照片详情的来源声明：
 *   `path` = 本页 fullPath（带上同组 / 下钻参数），返回时回到同一个地点；
 *   `name` 用**地点显示名**，按钮会显示「← 返回 呼伦贝尔草原」；
 *   `scope` 让左右箭头沿**这个地点当前正在看的照片**翻 —— 用的是与
 *   `loadPhotos()` 同一份 `activeCodes`（同组归并 / 下钻都跟着走），
 *   在这里另写一套过滤条件，就会出现「页面上有 15 张、翻页只有 8 张」。
 */
const photoSource = computed(() => ({
  ...currentSource(route, detail.value ? placeDisplayName(detail.value) : '地点'),
  scope: scopeParam('place', placeCode.value, activeCodes.value),
}))

/**
 * 页头返回：与照片详情页**同一语义**（见 utils/photoReturn.js 顶部）——
 * 能回退就回退（从地点列表点进来的就回地点列表），没有站内上一页才 replace 兜底。
 */
function goBack() {
  goBackOr(router, '/places')
}

async function loadDetail() {
  if (!placeCode.value) return
  detailLoading.value = true
  detailError.value = ''
  try {
    detail.value = await getPlace(placeCode.value, {
      placeCodes: queryCodes.value || undefined,
    })
    activeCode.value = ''
  } catch (e) {
    detail.value = null
    detailError.value = e?.message || '读取地点失败'
  } finally {
    detailLoading.value = false
  }
}

async function loadPhotos({ replace = true } = {}) {
  if (!placeCode.value) return
  photosLoading.value = true
  if (replace) page.value = 1
  try {
    const body = await listPlacePhotos(placeCode.value, {
      placeCodes: activeCodes.value || undefined,
      page: page.value,
      size: SIZE,
    })
    const list = body?.items ?? []
    photos.value = replace ? list : [...photos.value, ...list]
    photoTotal.value = Number(body?.total) || 0
    photoYears.value = body?.years ?? []
  } finally {
    photosLoading.value = false
  }
}

async function loadPersons() {
  if (!placeCode.value) return
  personsLoading.value = true
  try {
    const body = await listPlacePersons(placeCode.value, {
      placeCodes: activeCodes.value || undefined,
      size: 200,
    })
    persons.value = body?.items ?? []
    photoTotalAtPlace.value = Number(body?.photoTotal) || 0
  } finally {
    personsLoading.value = false
  }
}

/** 再取一页（`page` 由调用方先 +1）——页码分页而不是无限滚，理由同照片流 */
async function loadMore() {
  page.value += 1
  await loadPhotos({ replace: false })
}

onMounted(async () => {
  await loadDetail()
  await Promise.all([loadPhotos(), loadPersons()])
})

// 下钻切换：只重取子资源（头部不动，见文件头 ①）
watch(activeCode, async () => {
  // 把选择写进 URL，刷新/分享之后还是这一组
  router.replace({
    query: {
      ...route.query,
      placeCodes: activeCodes.value || undefined,
    },
  }).catch(() => {})
  await Promise.all([loadPhotos(), loadPersons()])
})

/**
 * 字典里的张数（**缓存值**）与实际按地点匹配到的张数之差。
 *
 * ⚠️ 为什么要把这个差显示出来：`pb_place.photoCount` 是 `rebuildPlaces()` 的
 *    派生缓存，扫描新照片/改目录名之后**不会自动更新**。于是同一页上会出现
 *    「头部说 15 张、照片页签说 40 张」——两个数字都"对"，但用户只会觉得
 *    这个页面坏了。差异本身说明「该刷新地点字典了」，是**可执行的信息**，
 *    所以显示它，而不是把其中一个数字改成另一个（那会把缓存过期这件事藏起来）。
 * ⚠️ 只在下钻到「全部」时比较：选中组内某一点时，照片数是子集，比不得。
 */
const countDrift = computed(() => {
  if (!detail.value || activeCode.value) return 0
  const cached = Number(detail.value.photoCount) || 0
  return Math.max(0, Number(photoTotal.value) - cached)
})

/** 照片按**年**分组（`shotYear` 缺失的单独一组，不丢） */
const photoGroups = computed(() => {
  const buckets = new Map()
  for (const photo of photos.value || []) {
    const year = formatYear(photo)
    if (!buckets.has(year)) buckets.set(year, [])
    buckets.get(year).push(photo)
  }
  return [...buckets.entries()].map(([year, list]) => ({ year, photos: list }))
})

const hasMorePhotos = computed(() => photos.value.length < photoTotal.value)

function yearSpanOf(place) {
  const low = place?.firstShotYear
  const high = place?.lastShotYear
  if (!low && !high) return EMPTY
  if (!low || !high || String(low) === String(high)) return String(low || high)
  return `${low}–${high}`
}

// ⚠️ 「同组分段怎么标」这件事在 `PlaceMemberPicker` 里（那里写了为什么
//    不能用显示名当标签），本文件只负责把选择写进响应式状态并重取子资源。
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 页头 -->
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <!-- 两个返回，与照片详情页同一套（主按钮 = 回退语义，次级 = 固定列表入口） -->
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-btn text-caption text-ink-sub transition-colors duration-150 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
            @click="goBack"
          >
            <ArrowLeft class="h-3.5 w-3.5" aria-hidden="true" />返回上一页
          </button>
          <span class="text-caption text-ink-weak" aria-hidden="true">|</span>
          <RouterLink
            to="/places"
            class="text-caption text-ink-sub transition-colors duration-150 hover:text-ink"
            >地点</RouterLink
          >
        </div>

        <h2 class="mt-1 truncate text-title text-ink" :title="placeRawName(detail) || undefined">
          {{ detail ? placeDisplayName(detail) : (detailLoading ? '正在读取…' : '地点详情') }}
        </h2>

        <p v-if="detail" class="pb-hint mt-1 flex flex-wrap items-center gap-x-2">
          <span class="tabular-nums">{{ yearSpanOf(detail) }}</span>
          <span aria-hidden="true">·</span>
          <span class="tabular-nums">{{ formatCount(detail.photoCount) }} 张</span>
          <template v-if="detail.merged">
            <span aria-hidden="true">·</span>
            <span>归并 {{ detail.placeCodes.length }} 处（同名地点，placeCode 未改）</span>
          </template>
          <template v-if="hasCoord">
            <span aria-hidden="true">·</span>
            <span class="inline-flex items-center gap-1 tabular-nums">
              <MapPin class="h-3 w-3" aria-hidden="true" />
              {{ detail.centerLat }}, {{ detail.centerLon }}
            </span>
          </template>
          <span v-else aria-hidden="true">·</span>
          <span v-if="!hasCoord">没有 GPS 坐标（来自目录名线索）—— 本页不画地图</span>
        </p>

        <!-- 英文原值：只在**排障**时有意义，但它是「中文名算得对不对」的唯一依据 -->
        <p v-if="placeRawName(detail)" class="pb-hint mt-1">
          英文聚合键：<code>{{ placeRawName(detail) }}</code>
        </p>
      </div>

      <el-button :loading="photosLoading" aria-label="重新加载" @click="loadPhotos()">
        <RotateCw class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />刷新
      </el-button>
    </div>

    <p v-if="detailError" class="pb-card px-4 py-6 text-body text-danger-ink">
      {{ detailError }}
      <el-button class="ml-2" size="small" @click="loadDetail">重试</el-button>
    </p>

    <!-- 同组分段下钻（DR-36：归并项点进来要能看出各来源各有多少） -->
    <PlaceMemberPicker
      v-model="activeCode"
      :members="members"
      :total="Number(detail?.photoCount) || 0"
    />

    <el-tabs v-model="activeTab" class="pb-card px-4 pb-4">
      <!-- ============ Tab1 照片 ============ -->
      <el-tab-pane :label="`照片（${formatCount(photoTotal)}）`" name="photos">
        <div class="space-y-4 pt-2">
          <p v-if="photosLoading && !photos.length" class="pb-hint">正在读取照片…</p>
          <p v-else-if="!photos.length" class="pb-hint">
            这里还没有照片。可能是地点字典里的张数是缓存值 ——
            到「设置」里点一次<b>重建地点字典</b>再回来看。
          </p>

          <template v-else>
            <section v-for="group in photoGroups" :key="group.year" class="space-y-2">
              <h3 class="flex items-baseline gap-2 text-body text-ink">
                <span class="tabular-nums">{{ group.year }}</span>
                <span class="text-caption tabular-nums text-ink-weak">
                  {{ group.photos.length }} 张（本页）
                </span>
                <span
                  v-if="Number(photoYears.find((y) => String(y.year) === group.year)?.count)"
                  class="text-caption tabular-nums text-ink-weak"
                >
                  · 全年
                  {{ formatCount(photoYears.find((y) => String(y.year) === group.year).count) }} 张
                </span>
              </h3>
              <ul class="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6 xl:grid-cols-8">
                <li v-for="photo in group.photos" :key="photo.photoCode">
                  <RouterLink
                    :to="photoDetailLink(photo.photoCode, photoSource)"
                    class="block"
                    :aria-label="`打开照片详情：${photo.relPath || photo.photoCode}`"
                  >
                    <PhotoThumb :photo="photo" thumb-size="200" />
                  </RouterLink>
                </li>
              </ul>
            </section>

            <div class="pt-2 text-center">
              <el-button v-if="hasMorePhotos" :loading="photosLoading" @click="loadMore">
                加载更多（已显示 {{ photos.length }} / {{ formatCount(photoTotal) }}）
              </el-button>
              <p v-else class="pb-hint">
                已到末尾，共 {{ formatCount(photoTotal) }} 张
                （点开某张仍走照片详情页，那里有左右翻页）
              </p>
            </div>
          </template>
        </div>
      </el-tab-pane>

      <!-- ============ Tab2 在场的人 ============ -->
      <!-- ⚠️ 列表本体在 `components/common/PlacePersonList.vue`（本文件超 300 行，
           按硬约束把这一整块有独立语义的东西拆了出去；三态也在那边自己管） -->
      <el-tab-pane :label="`在场的人（${formatCount(persons.length)}）`" name="persons">
        <PlacePersonList
          :items="persons"
          :loading="personsLoading"
          :photo-total="photoTotalAtPlace"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- 缓存值 vs 实时匹配值的安全阀：不一致时**说出来**，不让用户对着两个数字发懵 -->
    <p v-if="countDrift" class="pb-hint">
      ⚠ 地点字典里记的是 {{ formatCount(detail.photoCount) }} 张，实际按地点匹配到
      {{ formatCount(photoTotal) }} 张。字典那张数是**缓存值**（扫描新照片或改目录名之后
      不会自动更新）—— 到「设置」里点一次<b>重建地点字典</b>，两个数字就会一致。
    </p>

    <!-- 页脚引导：把「为什么这里是空的」说清楚（指向纠错队列） -->
    <p
      v-if="detail && !persons.length && !personsLoading"
      class="flex items-start gap-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
    >
      <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        该地点还没有关联到人物（这处共 {{ formatCount(photoTotalAtPlace) }} 张照片，
        其中 0 张关联到人）。去
        <RouterLink to="/review" class="underline">「待确认」</RouterLink>
        或<b>我不同意</b>队列确认人脸后，这里会显示当时在场的人 ——
        现在全库 3647 张脸里只有很小一部分有归属，所以这是数据现状，不是本页没做。
      </span>
    </p>
  </div>
</template>
