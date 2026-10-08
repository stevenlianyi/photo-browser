<!--
  PersonPlaces —— 人物详情 Tab1「时间轴」**下方**的「去过的地方」区块（R5 · DR-39）
  --------------------------------------------------------------------------
  位置是**用户点名的**：就在年代档时间轴的下一块，**不新开 Tab、不独立成页**
  （空 Tab 比空区块更难看，而实测绝大多数人这个区块就是空的）。

  四条不能省的语义
  --------------
  ① **每条走 placeDisplayName()**（`api/place.js` 的唯一入口）。
     直接渲染 `placeName` 的后果：17 个 GPS 地点全是
     `CN, Beijing, Datun` 这种英文串 —— 正是 R4a 要解决的问题。
  ② **整块为空时连标题一起隐藏**，只留一行「该人 N 张照片中，M 张有地点信息」。
     只写「暂无数据」是不合格的：用户分不清「功能没做」与「地点线索没覆盖到」。
     带上 N/M 才说明白「是数据里没有，不是这里没做」。
  ③ **点击 = `/photos?personCode=X&placeName=Y`**（人物 + 地点**双条件**）。
     比跳 `/places/{code}` 更贴合「**这个人**去过的地方」的语义 ——
     跳地点详情会丢掉「谁」，用户还得再从在场的人里找回去。
     `/api/photos` 同时支持这两个参数（已核实），R4a 又做了「中文名/英文键
     返回同一批照片」的翻译，所以两种写法都能命中。
  ④ **加载态不复用空状态文案**：`places` 初值是空对象，加载中就渲染
     「该人 0 张照片中，0 张有地点信息」的话，每次进页面都会先闪一句假话。
-->
<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { MapPin } from 'lucide-vue-next'
import { placeDisplayName, placeRawName } from '@/api/place'
import { EMPTY, formatCount } from '@/utils/format'

const props = defineProps({
  /** 谁的详情页（点击跳转要带上，才能保留人物上下文） */
  personCode: { type: String, required: true },
  /** `GET /persons/{code}/places` 的 places[] */
  places: { type: Array, default: () => [] },
  /** 该人的照片总数（分组文案的分母） */
  photoTotal: { type: Number, default: 0 },
  /** 其中有地点信息的张数（分子） */
  locatedPhotoTotal: { type: Number, default: 0 },
  loading: { type: Boolean, default: false },
})

/**
 * 排序：`lastShotYear` 倒序（最近去过的在前）。
 * ⚠️ 后端已经排过一遍，这里**再排一次是刻意的**：排序是这个区块的
 *    展示语义（「最近去过的」），而不是某个接口的实现细节 ——
 *    哪天接口换了实现，页面不该跟着变成另一个顺序。
 *    没有年份的排最后（`?? -1`，与后端 ORDER BY 的 NULL 语义一致）。
 */
const orderedPlaces = computed(() =>
  [...(props.places || [])]
    .sort((a, b) => (Number(b?.lastShotYear ?? -1) - Number(a?.lastShotYear ?? -1))),
)

const hasPlaces = computed(() => orderedPlaces.value.length > 0)

/** 年份范围：只有一年时不要写成 `2007–2007`（那看起来像渲染坏了） */
function yearSpanOf(place) {
  const low = place?.firstShotYear
  const high = place?.lastShotYear
  if (!low && !high) return EMPTY
  if (!low || !high || String(low) === String(high)) return String(low || high)
  return `${low}–${high}`
}

function targetOf(place) {
  // ⚠️ 传的是**聚合键那一套值**，不是 placeCode：
  //    `/api/photos?placeName=` 吃的是聚合键（中文名与英文键都收，
  //    `placeStore.resolvePlaceFilter`），而 placeCode 是 `pb_place` 的概念，
  //    `/photos` 根本不认识它。
  // ⚠️ 优先传**显示名**（nameZh ?? placeName）而不是英文键：
  //    照片流的地点筛选下拉，选项值就是显示名 —— 传英文键（`CN, Beijing, Longtan`）
  //    会让筛选框显示一串英文原值、且选不中任何选项（它不在选项列表里），
  //    用户看到的是「我明明点了地点，筛选框里却是一串看不懂的东西」。
  //    两套写法在后端都命中同一批照片（DR-29③），所以这里选可读的那套。
  const value = String(place?.nameZh || place?.placeName || '').trim()
  return {
    path: '/photos',
    query: { personCode: props.personCode, placeName: value },
  }
}

function ariaLabelOf(place) {
  const name = placeDisplayName(place)
  const raw = placeRawName(place)
  return `查看 ${name}${raw ? `（${raw}）` : ''} 的照片：`
    + `${Number(place?.photoCount) || 0} 张，${yearSpanOf(place)}`
}
</script>

<template>
  <!-- 加载中：不显示标题也不显示 N/M（都要等数据），只给一句话 -->
  <p v-if="loading" class="pb-hint mt-6 border-t border-line pt-4">
    正在读取去过的地方…
  </p>

  <section
    v-else-if="hasPlaces"
    class="mt-6 border-t border-line pt-4"
    aria-labelledby="pd-places-title"
  >
    <h3 id="pd-places-title" class="text-body text-ink">去过的地方</h3>

    <ul class="mt-3 space-y-1">
      <li v-for="place in orderedPlaces" :key="place.placeCode || place.placeName">
        <!-- 整条可点：<RouterLink> 渲染成 <a href>，键盘可达、可中键新开 -->
        <RouterLink
          :to="targetOf(place)"
          class="flex items-baseline gap-2 rounded-btn px-2 py-1.5 text-body transition-colors duration-150 hover:bg-surface focus-visible:bg-surface"
          :aria-label="ariaLabelOf(place)"
        >
          <MapPin class="h-3.5 w-3.5 shrink-0 self-center text-ink-weak" aria-hidden="true" />
          <span class="truncate text-ink" :title="placeRawName(place) || undefined">
            {{ placeDisplayName(place) }}
          </span>
          <span class="shrink-0 text-caption tabular-nums text-ink-weak">
            {{ formatCount(place.photoCount) }} 张
          </span>
          <span class="ml-auto shrink-0 text-caption tabular-nums text-ink-sub">
            {{ yearSpanOf(place) }}
          </span>
        </RouterLink>
      </li>
    </ul>

    <p class="pb-hint mt-2">
      点击任一条 = 只看<b>这个人在这处</b>的照片（人物 + 地点双条件）。
      该人共 {{ formatCount(photoTotal) }} 张照片，其中
      {{ formatCount(locatedPhotoTotal) }} 张有地点信息。
    </p>
  </section>

  <!--
    空状态：**整块隐藏**，只留这一行。
    ⚠️ 文案必须带 N/M：实测全库「照片带地点」的人物关联只有个位数，
       绝大多数人看到的都是这一行 —— 它得让人明白「是地点线索没覆盖到」，
       而不是「这个功能没做」。
  -->
  <p v-else class="pb-hint mt-6 border-t border-line pt-4">
    该人 {{ formatCount(photoTotal) }} 张照片中，
    {{ formatCount(locatedPhotoTotal) }} 张有地点信息。
    <template v-if="Number(photoTotal) > 0">
      在「待确认」队列里给有地点的照片确认人脸之后，这里会显示他去过的地方。
    </template>
  </p>
</template>
