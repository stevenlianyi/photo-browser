<!--
  PlaceNamePicker —— 「选一个地名」（R13 · DR-54）
  ============================================================
  三段候选，**已有地点排在最前**：
    ① 已有地点（按张数倒序，带张数）—— 库里已经有照片的那些（华盛顿 / 纽约 / 外婆家…）
    ② 行政区（省 · 区县）—— 离线地理数据集的**规范名**，约 2832 条 + 34 个省级
    ③ 自由输入 —— 下拉里没有的名字直接打（`allow-create`）

  为什么值得一个共用组件
  --------------------
    两个入口都要它：照片详情「修正地点」（写 `pb_photo.placeNameOverride`）与
    地点详情「重命名」（写 `pb_place.nameZh`）。两处**候选词表必须一致** ——
    各写一份的下场是「同一个区县，在 A 里选得到、在 B 里选不到」。

  ⚠️ 两个组选出来的**值不是一回事**，但结局相同（这是刻意的，别"优化"掉）
  ------------------------------------------------------------------
    · 已有地点 -> 值是**英文聚合键**（`CN, Beijing, Datum`）。选它就等于把照片
      挂到那个键上（列表里它属于「北京市 · 朝阳区」那一项）。
    · 行政区   -> 值是**规范中文名**（`北京市 · 朝阳区`）。选它会新建一个中文键的行，
      并把 `nameZh` 也写成同一个规范名 ⇒ 靠 `groupByNameZh` **归并进同一项**。
      （为什么不能直接写那 8 个街道键之一：一次只能写一个字符串，
        而"整个朝阳区"在库里是 8 个键 —— 写哪个都表达不了"朝阳区"。）
    ⇒ 两条路都会让「北京市 · 朝阳区」那一项 +1 张，因此保留两组不冲突。

  ⚠️ 为什么用 `remote`（服务端过滤）而不是一次拉全量本地过滤
  ------------------------------------------------------
    行政区 2866 条 + 已有地点几百条，全部渲染成 option 会让下拉明显卡顿；
    而"输入即查询"本来就是用户找区县的方式（没人会去翻 2800 项列表）。
-->
<script setup>
import { computed, ref } from 'vue'
import { listDistricts, listPlaces, placeDisplayName } from '@/api/place'
import { formatCount } from '@/utils/format'

const props = defineProps({
  modelValue: { type: String, default: '' },
  /** `both` = 已有地点 + 行政区（默认）｜`districts` = 只给行政区（改显示名时用） */
  groups: { type: String, default: 'both' },
  placeholder: { type: String, default: '选一个地点，或直接输入新名字' },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const places = ref([])
const districts = ref([])
const snapshot = ref({})
const loading = ref(false)
/** 词表不可用（数据源缺失）—— 此时只剩"自由输入"，界面要如实说一句 */
const vocabReady = ref(true)

const withPlaces = computed(() => props.groups !== 'districts')
const noDataText = computed(() => (loading.value
  ? '正在查询…'
  : '没有匹配项 —— 直接回车就用你输入的这几个字'))

/** 逐次查询：两个组并发，互不阻塞（一组失败不该让另一组消失） */
async function search(word) {
  const keyword = String(word ?? '').trim()
  loading.value = true
  try {
    const jobs = [listDistricts({
      keyword: keyword || undefined,
      // 空关键字时给一份"浏览用"的短名单（省 + 前几个省），打关键字时才放大
      limit: keyword ? 60 : 120,
    })]
    if (withPlaces.value) {
      jobs.push(listPlaces({
        keyword: keyword || undefined,
        groupByNameZh: 1, page: 1, size: 50, orderBy: 'photoCount', desc: 1,
      }))
    }
    const [districtBody, placeBody] = await Promise.all(jobs)
    districts.value = districtBody?.items ?? []
    snapshot.value = districtBody?.snapshot ?? {}
    vocabReady.value = districtBody?.ready !== false
    if (withPlaces.value) places.value = placeBody?.items ?? []
  } catch {
    // 查询失败不弹错：用户在输入过程中本来就会敲出空结果，弹错会把输入打断
    districts.value = []
  } finally {
    loading.value = false
  }
}

/** 首次展开时拉一次"默认名单"（`remote` 模式下 el-select 不会自己触发） */
function onVisible(opened) {
  if (opened && !districts.value.length && !loading.value) search('')
}
</script>

<template>
  <div class="space-y-1">
    <el-select
      :model-value="modelValue"
      class="w-full"
      filterable
      remote
      allow-create
      default-first-option
      clearable
      :remote-method="search"
      :loading="loading"
      :disabled="disabled"
      :placeholder="placeholder"
      :no-data-text="noDataText"
      @visible-change="onVisible"
      @update:model-value="(value) => emit('update:modelValue', value ?? '')"
    >
      <el-option-group v-if="withPlaces && places.length" label="已有地点（按张数）">
        <el-option
          v-for="one in places"
          :key="'p-' + one.placeCode"
          :value="one.placeName"
          :label="`${placeDisplayName(one)} · ${formatCount(one.photoCount)} 张`"
        />
      </el-option-group>
      <el-option-group v-if="districts.length" label="行政区（省 · 区县）">
        <el-option
          v-for="one in districts"
          :key="'d-' + one.label"
          :value="one.label"
          :label="one.ambiguous ? `${one.label}（省内同名，无法区分）` : one.label"
        />
      </el-option-group>
    </el-select>
    <p v-if="!vocabReady" class="text-caption text-warning-ink">
      行政区词表不可用（地理数据缺失）—— 只能直接输入地点名。
    </p>
    <p v-else-if="snapshot.labelNum" class="text-caption text-ink-weak">
      行政区词表来自离线地理数据（{{ snapshot.labelNum }} 项 · 快照
      {{ String(snapshot.fetchedAt || '').slice(0, 8) || '—' }}）；
      选中的是<b>规范名</b>，会与 GPS 算出来的同名地点归并成一项。
    </p>
  </div>
</template>
