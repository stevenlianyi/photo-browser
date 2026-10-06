<!--
  P-02 照片流（设计稿 §4.3）
  ------------------------------------------------------------
  筛选条（人物 / 年份区间 / 地点 / 仅含人脸 / 重复 / 排序）＋ 网格 ⇄ 时间轴双视图 ＋ 分页。

  红线：**网格里只加载缩略图**，绝不加载原图（点击才进详情页取原图）。
  这一条在本文件里是**结构性**的：网格的每格是 PhotoThumb，而 PhotoThumb 的
  src 只能由 photoCode 推导成 /api/thumb —— 组件根本没有接收原图 URL 的入口；
  本文件也**不 import originalUrl**（import 了就等于把红线交给记性）。

  「10 万张滚动不卡」怎么做到的
  ---------------------------
  ① **页码分页 + 滚动追加**（不是虚拟滚动，理由见 store/photos.js 的注释）
  ② PhotoThumb 用 **IntersectionObserver 自己管 src**：没进视口的格子不请求
  ③ 每格 `content-visibility: auto` + `contain-intrinsic-size`：屏外的格子
     **跳过布局与绘制**，只占位。滚到深处时 DOM 节点多，但每帧要处理的
     始终只有视口那几十个。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { LayoutGrid, RotateCw, Rows3, Search } from 'lucide-vue-next'
import PhotoThumb from '@/components/photo/PhotoThumb.vue'
import { usePhotosStore } from '@/store/photos'
import { useSettingsStore } from '@/store/settings'
import { formatCount } from '@/utils/format'

const photos = usePhotosStore()
const settings = useSettingsStore()

const viewMode = computed(() => settings.photoViewMode)
const filters = photos.filters

/** 年份下拉的数据源：从时间轴年表反推（库里有哪些年就有哪些选项） */
const yearOptions = computed(() =>
  photos.timelineYears
    .map((year) => year.year)
    .filter(Boolean)
    .sort((a, b) => b - a),
)

async function reload() {
  if (viewMode.value === 'timeline') {
    await photos.fetchTimelineYears()
    return
  }
  await photos.fetchPhotos({ replace: true })
}

/** 筛选变化 -> 回到第 1 页重取（带旧页码去新条件里翻是错的） */
let debounce = null
watch(
  () => [
    filters.keyword,
    filters.personCode,
    filters.placeName,
    filters.shotYearFrom,
    filters.shotYearTo,
    photos.onlyWithFace,
    photos.onlyDuplicate,
    photos.orderBy,
  ],
  () => {
    photos.applyFilters()
    clearTimeout(debounce)
    debounce = setTimeout(() => reload(), 250)
  },
)

watch(viewMode, (mode) => {
  if (mode === 'grid' && !photos.loaded) reload()
  if (mode === 'timeline' && !photos.timelineYears.length) photos.fetchTimelineYears()
})

/** 滚动追加的哨兵：进视口就取下一页 */
const sentinel = ref(null)
let observer = null

function setupObserver() {
  if (observer || typeof IntersectionObserver === 'undefined') return
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) photos.appendMore()
    },
    // 提前 800px 开始取，用户滚到底时下一页已经在路上
    { rootMargin: '800px 0px' },
  )
  if (sentinel.value) observer.observe(sentinel.value)
}

onMounted(async () => {
  photos.loadFilterOptions()
  // 年份下拉的数据源来自时间轴年表，所以**首屏就要取**
  // （只在切到时间轴视图时才取的话，网格视图的「年份」下拉会永远是空的 ——
  //  看着像坏了，而它其实是筛选条件之一，必须任何视图下都能用）
  await Promise.all([reload(), photos.fetchTimelineYears()])
  setupObserver()
})

onBeforeUnmount(() => {
  clearTimeout(debounce)
  if (observer) {
    observer.disconnect()
    observer = null
  }
})

function onPageChange(value) {
  photos.setPage(value)
  photos.fetchPhotos({ replace: true })
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

/** 首屏前 12 张直接加载（其余走 IO），避免首屏出现「等一下才出图」的空窗 */
function isEager(index) {
  return index < 12
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">照片流</h2>
        <p class="pb-hint mt-1">
          共 {{ formatCount(photos.total) }} 张照片 ·
          <span v-if="viewMode === 'grid'">网格只加载缩略图，点击才取原图</span>
          <span v-else>时间轴按年月分组，点开某年再点某月</span>
        </p>
      </div>
      <el-radio-group
        :model-value="viewMode"
        aria-label="切换视图"
        @update:model-value="settings.setViewMode"
      >
        <el-radio-button value="grid">
          <LayoutGrid class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />网格
        </el-radio-button>
        <el-radio-button value="timeline">
          <Rows3 class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />时间轴
        </el-radio-button>
      </el-radio-group>
    </div>

    <!-- 筛选条 -->
    <section class="pb-card p-3" aria-label="筛选条件">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-2">
          <label class="pb-hint" for="ph-keyword">关键词</label>
          <el-input
            id="ph-keyword"
            v-model="filters.keyword"
            class="w-44"
            placeholder="路径 / 地点 / 机型"
            clearable
          >
            <template #prefix>
              <Search class="h-4 w-4 text-ink-weak" aria-hidden="true" />
            </template>
          </el-input>
        </div>

        <div class="flex items-center gap-2">
          <label class="pb-hint" for="ph-person">人物</label>
          <el-select
            id="ph-person"
            v-model="filters.personCode"
            class="w-40"
            placeholder="全部"
            clearable
            filterable
            :loading="photos.optionsLoading"
          >
            <el-option
              v-for="person in photos.personOptions"
              :key="person.personCode"
              :label="person.displayName"
              :value="person.personCode"
            >
              <span class="flex items-center justify-between gap-2">
                <span class="truncate">{{ person.displayName }}</span>
                <span class="shrink-0 text-caption tabular-nums text-ink-weak">{{
                  person.photoCount
                }}</span>
              </span>
            </el-option>
          </el-select>
        </div>

        <!-- 年份区间：两个下拉而不是区间滑块 —— 手机上滑块难点中，精度也低 -->
        <div class="flex items-center gap-2">
          <span class="pb-hint">年份</span>
          <el-select
            v-model="filters.shotYearFrom"
            class="w-24"
            placeholder="起"
            clearable
            aria-label="起始年份"
          >
            <el-option v-for="y in yearOptions" :key="`from-${y}`" :label="String(y)" :value="y" />
          </el-select>
          <span class="pb-hint" aria-hidden="true">–</span>
          <el-select
            v-model="filters.shotYearTo"
            class="w-24"
            placeholder="止"
            clearable
            aria-label="结束年份"
          >
            <el-option v-for="y in yearOptions" :key="`to-${y}`" :label="String(y)" :value="y" />
          </el-select>
        </div>

        <div class="flex items-center gap-2">
          <label class="pb-hint" for="ph-place">地点</label>
          <el-select
            id="ph-place"
            v-model="filters.placeName"
            class="w-36"
            placeholder="全部"
            clearable
            filterable
          >
            <el-option
              v-for="place in photos.placeOptions"
              :key="place.placeCode || place.placeName"
              :label="place.placeName"
              :value="place.placeName"
            />
          </el-select>
        </div>

        <el-checkbox v-model="photos.onlyWithFace">仅含人脸</el-checkbox>
        <el-checkbox v-model="photos.onlyDuplicate">重复照片</el-checkbox>

        <div class="ml-auto flex items-center gap-2">
          <label class="pb-hint" for="ph-sort">排序</label>
          <el-select id="ph-sort" v-model="photos.orderBy" class="w-32">
            <el-option
              v-for="item in photos.SORT_OPTIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <el-button :disabled="!photos.hasFilter" @click="photos.resetFilters()">
            清除筛选
          </el-button>
          <el-button :loading="photos.loading" aria-label="重新加载" @click="reload">
            <RotateCw class="h-4 w-4" aria-hidden="true" />
          </el-button>
        </div>
      </div>
    </section>

    <!-- ============ 网格视图 ============ -->
    <section v-if="viewMode === 'grid'" aria-label="照片网格">
      <!-- 首屏骨架：格子尺寸已由 aspect-ratio 预留，图片到位时不回流 -->
      <ul
        v-if="photos.loading && !photos.items.length"
        class="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
        aria-hidden="true"
      >
        <li v-for="n in 24" :key="n">
          <div class="pb-photo-frame aspect-square animate-pulse bg-skeleton" />
        </li>
      </ul>

      <p
        v-else-if="!photos.items.length"
        class="pb-card px-4 py-12 text-center text-body text-ink-weak"
      >
        没有符合条件的照片。
        <el-button
          v-if="photos.hasFilter"
          class="ml-2"
          size="small"
          @click="photos.resetFilters()"
          >清除筛选</el-button
        >
      </p>

      <ul
        v-else
        class="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
      >
        <li
          v-for="(photo, index) in photos.items"
          :key="photo.photoCode"
          class="[content-visibility:auto]"
          style="contain-intrinsic-size: auto 190px"
        >
          <RouterLink
            :to="`/photos/${photo.photoCode}`"
            class="block"
            :aria-label="`打开照片详情：${photo.relPath || photo.photoCode}`"
          >
            <PhotoThumb :photo="photo" :eager="isEager(index)" thumb-size="200" />
            <span class="mt-1 block truncate text-caption text-ink-weak">
              {{ photo.placeName || photo.cameraModel || '' }}
            </span>
          </RouterLink>
        </li>
      </ul>

      <!-- 滚动追加哨兵 -->
      <div ref="sentinel" class="h-8" aria-hidden="true" />

      <p v-if="photos.loadingMore" class="py-3 text-center text-caption text-ink-weak">
        正在加载更多…（已显示 {{ photos.items.length }} / {{ formatCount(photos.total) }}）
      </p>
      <p
        v-else-if="photos.appendCapped"
        class="rounded-btn bg-info-soft px-3 py-2 text-center text-caption text-info-ink"
      >
        已连续加载 {{ photos.items.length }} 张（DOM 节点到此为止，再堆下去切页会变慢）。
        想看更后面请用
        <b>年份筛选</b> 或底部的<b>分页器</b>跳转 —— 那是按页取，节点数会回到一页的量。
      </p>
      <p
        v-else-if="!photos.hasMore && photos.items.length"
        class="py-3 text-center text-caption text-ink-weak"
      >
        已到末尾，共 {{ formatCount(photos.total) }} 张
      </p>
    </section>

    <!-- ============ 时间轴视图 ============ -->
    <section v-else aria-label="按年月分组的时间轴" class="space-y-2">
      <p v-if="photos.timelineLoading" class="pb-hint">正在加载时间轴…</p>
      <p
        v-else-if="!photos.timelineYears.length"
        class="pb-card px-4 py-12 text-center text-body text-ink-weak"
      >
        库里还没有能读出拍摄时间的照片。
      </p>

      <ul v-else class="space-y-2">
        <li v-for="year in photos.timelineYears" :key="year.year" class="pb-card p-3">
          <button
            type="button"
            class="flex w-full items-center gap-3 text-left"
            :aria-expanded="photos.expandedYear === year.year"
            @click="photos.toggleYear(year.year)"
          >
            <span class="w-20 shrink-0 text-body font-medium tabular-nums text-ink">{{
              year.year
            }}</span>
            <span class="text-caption tabular-nums text-ink-weak">{{ year.count }} 张</span>
            <span class="ml-auto text-caption text-brand-ink">{{
              photos.expandedYear === year.year ? '收起' : '展开'
            }}</span>
          </button>

          <ul
            v-if="photos.expandedYear === year.year"
            class="mt-3 space-y-3 border-t border-line pt-3"
          >
            <li v-for="month in year.months" :key="month.ym">
              <button
                type="button"
                class="flex w-full items-center gap-2 text-left text-caption"
                :aria-expanded="photos.expandedMonth === month.ym"
                @click="photos.toggleMonth(year.year, month.ym)"
              >
                <span class="tabular-nums text-ink-sub">{{ month.ym }}</span>
                <span class="tabular-nums text-ink-weak">{{ month.count }} 张</span>
              </button>
              <ul v-if="photos.expandedMonth === month.ym" class="mt-2 flex flex-wrap gap-2">
                <li
                  v-for="photo in photos.monthPhotos[month.ym] || []"
                  :key="photo.photoCode"
                  class="w-20"
                >
                  <RouterLink
                    :to="`/photos/${photo.photoCode}`"
                    class="block"
                    :aria-label="`打开照片详情：${photo.relPath || photo.photoCode}`"
                  >
                    <PhotoThumb :photo="photo" show-name thumb-size="200" />
                  </RouterLink>
                </li>
              </ul>
            </li>
          </ul>
        </li>
      </ul>
    </section>

    <!-- ============ 分页信息 ============ -->
    <footer class="flex flex-wrap items-center justify-between gap-3">
      <p class="text-caption text-ink-sub">{{ photos.rangeText }}</p>
      <el-pagination
        v-if="viewMode === 'grid' && photos.total > photos.size"
        layout="prev, pager, next"
        background
        :total="photos.total"
        :page-size="photos.size"
        :current-page="photos.page"
        @current-change="onPageChange"
      />
    </footer>
  </div>
</template>
