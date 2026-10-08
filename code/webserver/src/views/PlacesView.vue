<!--
  P-06 地点列表（`/places`）—— R5 新建
  ------------------------------------------------------------
  卡片网格：封面缩略图 + **中文地点名** + 张数 + 年份 + 在场人数。
  筛选：搜索（中文名/英文键都匹配）/ 年份 / 仅有人物的。

  ⚠️ 三条 R5 的硬约束直接体现在这一页上
  -------------------------------------
  ① **不画地图**（DR-37②）：28 个地点里 11 个目录名地点
     （占 663 张里的 598 张）`centerLat/Lon` 全是 NULL。
     只有 17 个 GPS 点可画，而照片最多的那 11 个会在地图上**消失** ——
     那不是"少画了几个点"，是把 90% 的照片从地图上抹掉。
  ② **幽灵行不出现**：走 `groupByNameZh=1`，归并路径自己会滤掉
     `photoCount=0` 的行（DR-33 的清空后残留）。
  ③ **封面只走 `/api/thumb`**（红线）：卡片拿到的只有 `coverPhotoCode`，
     本文件**不 import originalUrl** —— import 了就等于把红线交给记性。

  两个刻意的取舍
  -------------
  · 卡片是 `<RouterLink>`（渲染成 `<a href>`）而不是 div + @click：
    键盘可达、可中键新开、读屏能识别成链接。
  · 封面用原生 `loading="lazy"` 而不是 PhotoThumb 的 IntersectionObserver：
    这里是**一屏 24 张**的卡片（不是网格页的几百张），原生懒加载已经够，
    而 PhotoThumb 的形状（1:1 定尺 + 角标）与封面卡不一致，
    复用它得先给它加一堆 props —— 那是把两个用途塞进一个组件。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { MapPin, RotateCw, Search, Users } from 'lucide-vue-next'
import { getTimeline } from '@/api/browse'
import { listPlaces, placeDisplayName, placeRawName } from '@/api/place'
import { thumbUrl } from '@/api/static'
import { EMPTY, formatCount } from '@/utils/format'

const items = ref([])
const total = ref(0)
const loading = ref(false)
const mergedGroups = ref(0)
const ungroupedTotal = ref(0)
const filteredZero = ref(0)

const keyword = ref('')
const year = ref('')
const onlyWithPerson = ref(false)
const years = ref([])

/** 一页给全（地点是几十~几百量级；上限由后端 clampPage 的 200 兜住） */
const SIZE = 200

const hasFilter = computed(
  () => Boolean(keyword.value) || Boolean(year.value) || onlyWithPerson.value,
)

async function reload() {
  loading.value = true
  try {
    const body = await listPlaces({
      page: 1,
      size: SIZE,
      // ⚠️ 必须走归并路径：幽灵行过滤、`year` / `hasPerson` 都只在归并路径上实现
      groupByNameZh: 1,
      keyword: keyword.value || undefined,
      year: year.value ? Number(year.value) : undefined,
      hasPerson: onlyWithPerson.value ? 1 : undefined,
      orderBy: 'photoCount',
      desc: 1,
    })
    items.value = body?.items ?? []
    total.value = Number(body?.total) || 0
    mergedGroups.value = Number(body?.mergedGroups) || 0
    ungroupedTotal.value = Number(body?.ungroupedTotal) || 0
    filteredZero.value = Number(body?.filteredZero) || 0
  } finally {
    loading.value = false
  }
}

/** 年份下拉：从时间轴年表反推（库里有哪些年就有哪些选项） */
async function loadYears() {
  try {
    const data = await getTimeline()
    years.value = (data?.items ?? []).map((one) => one.year).filter(Boolean)
      .sort((a, b) => b - a)
  } catch (e) {
    years.value = []
  }
}

let debounce = null
watch([keyword, year, onlyWithPerson], () => {
  clearTimeout(debounce)
  debounce = setTimeout(reload, 250)
})

onMounted(async () => {
  await Promise.all([reload(), loadYears()])
})

onBeforeUnmount(() => clearTimeout(debounce))

function reset() {
  keyword.value = ''
  year.value = ''
  onlyWithPerson.value = false
}

/** 年份跨度：只有一年时不要写成 `2013–2013` */
function yearSpanOf(place) {
  const low = place?.firstShotYear
  const high = place?.lastShotYear
  if (!low && !high) return EMPTY
  if (!low || !high || String(low) === String(high)) return String(low || high)
  return `${low}–${high}`
}

/**
 * 下钻入口：归并项带上**组内全部 placeCode**。
 * ⚠️ 不带的话详情页会按主编码自己再查一遍同名行 —— 结果通常一样，
 *    但「用户点的是哪一组」这件事就丢了（比如同名组里将来多出第三个点）。
 */
function targetOf(place) {
  const codes = (place?.placeCodes || []).filter(Boolean)
  return {
    path: `/places/${encodeURIComponent(place.placeCode || '')}`,
    query: codes.length > 1 ? { placeCodes: codes.join(',') } : {},
  }
}

function ariaLabelOf(place) {
  const raw = placeRawName(place)
  const people = Number(place?.personCount) || 0
  return `打开地点：${placeDisplayName(place)}${raw ? `（${raw}）` : ''}，`
    + `${Number(place?.photoCount) || 0} 张照片，${yearSpanOf(place)}，`
    + (people ? `在场 ${people} 人` : '暂无人物关联')
    + (Number(place?.placeCodes?.length) > 1 ? `，归并 ${place.placeCodes.length} 处` : '')
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">地点</h2>
        <p class="pb-hint mt-1">
          共 {{ formatCount(total) }} 处 ·
          <template v-if="ungroupedTotal">
            原始 {{ formatCount(ungroupedTotal) }} 行，
            归并 {{ formatCount(mergedGroups) }} 组同名地点<template
              v-if="filteredZero"
            >、隐藏 {{ formatCount(filteredZero) }} 行 0 张照片的空壳</template>
          </template>
          <span>（名称优先显示中文；有 GPS 的点才有坐标）</span>
        </p>
      </div>
      <el-button :loading="loading" aria-label="重新加载地点" @click="reload">
        <RotateCw class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />刷新
      </el-button>
    </div>

    <!-- 筛选条 -->
    <section class="pb-card p-3" aria-label="筛选条件">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-2">
          <!-- ⚠️ 标签加 whitespace-nowrap：筛选条是 flex-wrap 的，
               窄屏下中文标签会被折成两行（「搜索地/点」），看起来像渲染坏了 -->
          <label class="pb-hint whitespace-nowrap" for="pl-keyword">搜索地点</label>
          <el-input id="pl-keyword" v-model="keyword" class="w-44"
                    placeholder="中文名 / 英文键" clearable>
            <template #prefix>
              <Search class="h-4 w-4 text-ink-weak" aria-hidden="true" />
            </template>
          </el-input>
        </div>

        <div class="flex items-center gap-2">
          <label class="pb-hint whitespace-nowrap" for="pl-year">年份</label>
          <el-select id="pl-year" v-model="year" class="w-28" placeholder="全部"
                     clearable aria-label="只看该年有照片的地点">
            <el-option v-for="y in years" :key="`y-${y}`" :label="String(y)" :value="y" />
          </el-select>
        </div>

        <el-checkbox v-model="onlyWithPerson">仅有人物的</el-checkbox>

        <el-button v-if="hasFilter" class="ml-auto" size="small" @click="reset">
          清除筛选
        </el-button>
      </div>
    </section>

    <!-- 骨架 -->
    <!-- ⚠️ 断点：`<768` 固定 **2 列**（设计稿 §4 的响应式约定）；
         用 sm: 会在 640~767 之间变成 3 列，与约定不符 -->
    <ul v-if="loading && !items.length"
        class="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-5"
        aria-hidden="true">
      <li v-for="n in 10" :key="n" class="pb-card p-3">
        <div class="pb-photo-frame aspect-[4/3] animate-pulse bg-skeleton" />
      </li>
    </ul>

    <!-- 空状态：把「筛没了」与「库里真没有」分开说 -->
    <p v-else-if="!items.length" class="pb-card px-4 py-12 text-center text-body text-ink-weak">
      <template v-if="hasFilter">
        没有符合条件的地点。
        <el-button class="ml-2" size="small" @click="reset">清除筛选</el-button>
      </template>
      <template v-else>
        还没有任何地点。<br />
        <span class="text-caption">
          地点来自两条线索：照片的 GPS 逆地理，以及<b>目录名</b>里的日期+地名
          （如 <code>2013.07.26 华盛顿</code>）。扫描完之后到「设置」里点一次
          <b>重建地点字典</b>，这个列表才会有内容。
        </span>
      </template>
    </p>

    <ul v-else class="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-5">
      <li v-for="place in items" :key="place.placeCode || place.placeName">
        <RouterLink
          :to="targetOf(place)"
          class="pb-card group block overflow-hidden p-3 transition-transform duration-150 ease-out hover:-translate-y-0.5 focus-within:-translate-y-0.5"
          :aria-label="ariaLabelOf(place)"
        >
          <!-- 封面：只可能是 /api/thumb（红线） -->
          <span class="pb-photo-frame relative block aspect-[4/3] overflow-hidden">
            <img
              v-if="place.coverPhotoCode"
              :src="thumbUrl(place.coverPhotoCode, 400)"
              :alt="`${placeDisplayName(place)} 的封面照片`"
              class="h-full w-full object-cover"
              loading="lazy"
              decoding="async"
              draggable="false"
            />
            <span
              v-else
              class="flex h-full w-full items-center justify-center text-caption text-ink-weak"
            >
              没有可作封面的照片
            </span>

            <!-- 归并角标：**必须让人看出这是 2 处并成的 1 行** -->
            <span
              v-if="Number(place.placeCodes?.length) > 1"
              class="absolute right-1 top-1 rounded-btn bg-card px-1.5 py-0.5 text-caption text-ink"
              :title="`由 ${place.placeCodes.length} 处同名地点归并；点开可分别下钻`"
            >
              {{ place.placeCodes.length }} 处
            </span>
          </span>

          <span class="mt-2 block">
            <span class="block truncate text-body text-ink"
                  :title="placeRawName(place) || undefined">
              {{ placeDisplayName(place) }}
            </span>
            <span class="mt-0.5 flex items-baseline gap-2 text-caption text-ink-weak">
              <span class="tabular-nums">{{ formatCount(place.photoCount) }} 张</span>
              <span class="tabular-nums">{{ yearSpanOf(place) }}</span>
            </span>
            <span class="mt-1 flex items-center gap-1 text-caption text-ink-sub">
              <Users class="h-3 w-3" aria-hidden="true" />
              <!--
                空状态要**友好**：现在绝大多数地点是 0 人 —— 显示一个
                「0」加问号会让人以为坏了，这里给「暂无人物」并说明为什么。
              -->
              <span v-if="Number(place.personCount) > 0" class="tabular-nums">
                {{ formatCount(place.personCount) }} 人
              </span>
              <span v-else :title="'这张照片还没关联到人 —— 去「待确认」队列确认人脸后这里会有数字'">
                暂无人物
              </span>
              <!-- 只有**真有坐标**的地点才显示这个图标（11/28 目录名地点没有） -->
              <span
                v-if="place.centerLat !== null && place.centerLat !== undefined"
                class="ml-auto inline-flex"
                title="有 GPS 坐标"
              >
                <MapPin class="h-3 w-3 text-ink-weak" aria-hidden="true" />
                <span class="sr-only">有 GPS 坐标</span>
              </span>
            </span>
          </span>
        </RouterLink>
      </li>
    </ul>
  </div>
</template>
