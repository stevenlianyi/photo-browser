<!--
  P-04 人物库（设计稿 §4.8 顶栏 / §3.3）
  ------------------------------------------------------------
  顶栏：关键词搜索 ｜ 分类 chips ｜ 家庭组 ｜ 显示已停用 ｜ + 新建人物
  卡片：头像 + 姓名 + 照片数 + 质心健康度徽标；hover 出 [编辑] [停用]
  · **不提供删除**（DR-19）：只停用。一个误点不该把几小时的确认工作清零
  · 「0 张」= 还没有任何脸归属给他，多半是刚导入的联系人
  · 新建人物对话框复用 PersonForm —— 与 P-05 的编辑抽屉是同一个组件
-->
<script setup>
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Plus, Search, TriangleAlert, UserRound, X } from 'lucide-vue-next'
import PersonForm from '@/components/common/PersonForm.vue'
import { PERSON_CATEGORIES, usePersonsStore } from '@/store/persons'

const persons = usePersonsStore()
const filters = persons.filters

const FAMILY_OPTIONS = [
  { familyCode: 'F-chen', familyName: '陈家' },
  { familyCode: 'F-lian', familyName: '连家' },
]

/** 结构示例：步骤 12 接 /api/persons 后由真实数据替换 */
const PEOPLE = [
  {
    personCode: 'P-zhongwen',
    displayName: 'zhongwen lian',
    relation: '兄',
    categories: ['家人'],
    photoCount: 486,
    faceCount: 612,
    confirmedCount: 412,
    yearSpan: '1998–2021',
  },
  {
    personCode: 'P-luwen',
    displayName: 'luwen lian',
    relation: '妹',
    categories: ['家人'],
    photoCount: 12,
    faceCount: 15,
    confirmedCount: 14,
    yearSpan: '2015–2021',
  },
  {
    personCode: 'P-zhongming',
    displayName: 'zhongming lian',
    relation: '同事',
    categories: ['同事'],
    photoCount: 74,
    faceCount: 88,
    confirmedCount: 6,
    yearSpan: '2019–2022',
  },
  {
    personCode: 'P-new01',
    displayName: '新建的联系人',
    relation: '',
    categories: ['朋友'],
    photoCount: 0,
    faceCount: 0,
    confirmedCount: 0,
    yearSpan: '—',
  },
]

/**
 * 质心健康度（DR-16）：自动归属数 ≫ 人工确认数 ⇒ 识别质量偏低，
 * 主动提示用户去复核，而不是等他自己发现。
 */
function healthOf(person) {
  if (person.confirmedCount === 0 && person.photoCount > 0) {
    return { tone: 'warning', text: '确认样本为 0', hint: '此人的质心只由自动样本构成，容易越认越错' }
  }
  if (person.confirmedCount > 0 && person.photoCount > person.confirmedCount * 3) {
    return {
      tone: 'warning',
      text: '识别质量偏低',
      hint: `自动归属 ${person.faceCount - person.confirmedCount} 张 ≫ 人工确认 ${person.confirmedCount} 张，建议逐张复核`,
    }
  }
  return null
}

const cards = computed(() => PEOPLE)

const createVisible = ref(false)
function openCreate() {
  createVisible.value = true
}
function onCreateSubmit() {
  createVisible.value = false
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 顶栏 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">人物库</h2>
        <p class="pb-hint mt-1">共 {{ PEOPLE.length }} 人 · 停用的人默认不显示</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <div class="flex items-center gap-2">
          <label class="pb-hint" for="pp-search">搜索</label>
          <el-input
            id="pp-search"
            v-model="filters.keyword"
            class="w-56"
            placeholder="姓名 / 邮箱 / 电话"
            clearable
          >
            <template #prefix>
              <Search class="h-4 w-4 text-ink-weak" aria-hidden="true" />
            </template>
          </el-input>
        </div>

        <div class="flex items-center gap-2">
          <label class="pb-hint" for="pp-family">家庭组</label>
          <el-select id="pp-family" v-model="filters.familyGroupCode" class="w-32" placeholder="全部" clearable>
            <el-option
              v-for="group in FAMILY_OPTIONS"
              :key="group.familyCode"
              :label="group.familyName"
              :value="group.familyCode"
            />
          </el-select>
        </div>

        <el-checkbox v-model="persons.includeDisabled">显示已停用</el-checkbox>
        <el-button type="primary" @click="openCreate">
          <Plus class="mr-1 h-4 w-4" aria-hidden="true" />新建人物
        </el-button>
      </div>
    </div>

    <!-- 分类 chips（多值，筛选是单选） -->
    <div class="flex flex-wrap items-center gap-2" role="group" aria-label="按分类筛选">
      <span class="pb-hint">分类</span>
      <button
        type="button"
        class="rounded-btn border px-3 py-1 text-caption transition-colors duration-150"
        :class="
          filters.category === ''
            ? 'border-brand bg-brand-soft text-brand-ink'
            : 'border-line bg-card text-ink-sub hover:bg-surface'
        "
        :aria-pressed="filters.category === ''"
        @click="filters.category = ''"
      >
        全部
      </button>
      <button
        v-for="item in PERSON_CATEGORIES"
        :key="item.value"
        type="button"
        class="rounded-btn border px-3 py-1 text-caption transition-colors duration-150"
        :class="
          filters.category === item.value
            ? 'border-brand bg-brand-soft text-brand-ink'
            : 'border-line bg-card text-ink-sub hover:bg-surface'
        "
        :aria-pressed="filters.category === item.value"
        @click="filters.category = item.value"
      >
        {{ item.label }}
      </button>
      <el-button v-if="persons.hasFilter" size="small" text @click="persons.resetFilters()">
        清除筛选
      </el-button>
    </div>

    <!-- 人物卡片网格 -->
    <ul class="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      <li v-for="person in cards" :key="person.personCode">
        <article class="pb-card group relative p-4">
          <!-- hover 出 [编辑] [停用]，键盘聚焦同样可达 -->
          <div
            class="absolute right-2 top-2 flex gap-1 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover:opacity-100"
          >
            <el-button size="small" text>编辑</el-button>
            <el-button size="small" text type="danger">停用</el-button>
          </div>

          <RouterLink
            :to="`/people/${person.personCode}`"
            class="block"
            :aria-label="`${person.displayName}，${person.photoCount} 张照片`"
          >
            <span class="flex items-center gap-3">
              <span
                class="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-line bg-surface text-ink-weak"
                aria-hidden="true"
              >
                <UserRound class="h-5 w-5" />
              </span>
              <span class="min-w-0">
                <span class="block truncate text-body text-ink">{{ person.displayName }}</span>
                <span class="block truncate text-caption text-ink-weak">
                  {{ person.relation ? `${person.relation} · ` : '' }}{{ person.yearSpan }}
                </span>
              </span>
            </span>

            <span class="mt-3 flex flex-wrap items-center gap-2 text-caption">
              <span class="tabular-nums text-ink-sub">{{ person.photoCount }} 张</span>
              <span
                v-if="healthOf(person)"
                class="inline-flex items-center gap-1 rounded-btn bg-warning-soft px-1.5 py-0.5 text-warning-ink"
                :title="healthOf(person).hint"
              >
                <TriangleAlert class="h-3 w-3" aria-hidden="true" />{{ healthOf(person).text }}
              </span>
              <span
                v-else-if="person.photoCount === 0"
                class="inline-flex items-center gap-1 rounded-btn bg-surface px-1.5 py-0.5 text-ink-sub"
              >
                <X class="h-3 w-3" aria-hidden="true" />还没有任何脸归属给他
              </span>
            </span>
          </RouterLink>
        </article>
      </li>
    </ul>

    <p class="pb-hint">
      本页为结构示例，接入 /api/persons 后由真实数据替换。本工具不提供导出入口，迁移与备份请用设置页的
      「备份 / 恢复」。
    </p>

    <!-- 新建人物：与 P-05 编辑抽屉共用 PersonForm -->
    <el-dialog v-model="createVisible" title="新建人物" width="560px">
      <PersonForm :families="FAMILY_OPTIONS" @cancel="createVisible = false" @submit="onCreateSubmit" />
    </el-dialog>
  </div>
</template>