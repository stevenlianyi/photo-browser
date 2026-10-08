<!--
  P-04 人物库（设计稿 §4.8 顶栏 / §3.3）
  ------------------------------------------------------------
  顶栏：关键词搜索 ｜ 分类 chips ｜ 家庭组 ｜ 显示已停用 ｜ + 新建人物
  卡片：PersonCard（头像 + 姓名 + 照片数 + 年代跨度 + **识别质量徽标**）
  · **不提供删除**（DR-19）：只停用。一个误点不该把几小时的确认工作清零
  · 「还没有脸」= 还没有任何脸归属给他，多半是刚导入的联系人
  · 新建人物对话框复用 PersonForm —— 与 P-05 的编辑抽屉是同一个组件
    （**不得有第二份表单**）
  · 空状态引导：先导入联系人 → 再扫描 → 再确认，而不是一句「暂无数据」
-->
<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  CircleSlash,
  Contact,
  FolderSearch,
  Plus,
  Search,
  TriangleAlert,
  UserRound,
} from 'lucide-vue-next'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import PersonCard from '@/components/common/PersonCard.vue'
import PersonForm from '@/components/common/PersonForm.vue'
import { createContact, disableContact, getImpact, patchContact } from '@/api/contacts'
import { PERSON_CATEGORIES, usePersonsStore } from '@/store/persons'
import { useReviewStore } from '@/store/review'

const persons = usePersonsStore()
const review = useReviewStore()
const router = useRouter()
const filters = persons.filters

/** 编辑抽屉 / 停用对话框作用于谁 */
const editTarget = ref(null)
const disableTarget = ref(null)
const disableImpact = ref(null)
const disableSubmitting = ref(false)

const createVisible = ref(false)
const submitting = ref(false)

const families = computed(() => persons.families)
const hasAny = computed(() => persons.total > 0 || persons.hasFilter)

async function reload() {
  await persons.fetchPersons()
}

/** 筛选变化 -> 回到第 1 页重取（带旧页码去新条件里翻是错的） */
let debounce = null
watch(
  // ⚠️ 这里**不能**写 `filters.value.keyword`：`filters` 是 store 里那个 ref
  //    **解包后**的响应式对象（模板里 `filters.keyword` 能直接 v-model 就是证据），
  //    `filters.value` 是 undefined —— getter 每次取值都抛 TypeError，
  //    Vue 的 callWithErrorHandling 把它吞掉（只在控制台留一条告警），
  //    **依赖收集就此中断**：搜索框打字、分类 chips、家庭组全都不会触发重取，
  //    界面上看起来就是「搜索没反应」。
  () => [
    filters.keyword,
    filters.category,
    filters.familyGroupCode,
    persons.includeDisabled,
  ],
  () => {
    persons.page = 1
    clearTimeout(debounce)
    debounce = setTimeout(() => reload(), 250)
  },
)

watch(
  () => persons.page,
  () => {
    reload()
  },
)

onMounted(async () => {
  await Promise.all([reload(), persons.fetchFamilies()])
})

// ============================================================
// 新建 / 编辑（**同一个 PersonForm**，只是提交目标不同）
// ============================================================

function openCreate() {
  createVisible.value = true
}

async function submitCreate(form) {
  submitting.value = true
  try {
    const created = await createContact({
      displayName: String(form.displayName || '').trim(),
      familyName: form.familyName || undefined,
      relation: form.relation || undefined,
      familyGroupCode: form.familyGroupCode || undefined,
      birthday: form.birthday || undefined,
      email: form.email || undefined,
      phone: form.phone || undefined,
      categories: Array.isArray(form.categories) ? form.categories : [],
    })
    createVisible.value = false
    const code = created?.personCode || created?.person?.personCode
    ElMessage.success(`已建档「${form.displayName}」`)
    await reload()
    // 建完直接进详情：那一步的「质心健康度」提示会立刻告诉用户
    // 「他还没有确认样本」，不用他自己发现。
    if (code) router.push(`/people/${code}`)
  } finally {
    submitting.value = false
  }
}

/**
 * 保存编辑。
 *
 * ⚠️ 只在 `centroidRebuilt=true` 时提示「年代档与质心已重算」——
 *    改了姓名/邮箱也弹「质心已重算」是对无影响改动的噪声，
 *    弹多了用户就学会无视所有 Toast（包括真正重要的那条）。
 */
async function submitEdit(form) {
  submitting.value = true
  try {
    const data = await patchContact(editTarget.value.personCode, {
      displayName: String(form.displayName || '').trim(),
      familyName: form.familyName || undefined,
      relation: form.relation || undefined,
      familyGroupCode: form.familyGroupCode || undefined,
      birthday: form.birthday || undefined,
      email: form.email || undefined,
      phone: form.phone || undefined,
      categories: Array.isArray(form.categories) ? form.categories : [],
    })
    editTarget.value = null
    if (data?.centroidRebuilt) {
      ElMessage.success('生日已改，该人的年代档与质心已重算')
    } else {
      ElMessage.success('已保存')
    }
    await reload()
  } catch (e) {
    // DR-18：撞 displayName（UNIQUE）不是「保存失败」，而是**多半是同一个人**。
    // axios 拦截器已经弹过后端 message，这里再补一条可执行的下一步。
    if (e?.code === 'DUPLICATE_DISPLAY_NAME') {
      const existing = e?.payload?.existing || {}
      ElMessage.warning(
        `已存在「${existing.displayName || ''}」，你可能是想合并到 TA？`
        + `${existing.photoCount ?? 0} 张照片 / ${existing.faceCount ?? 0} 张脸 —— 请到详情页用「合并到…」`,
      )
    }
  } finally {
    submitting.value = false
  }
}

// ============================================================
// 停用（DR-19）：**先看影响面**，二次确认才执行
// ============================================================

async function openDisable(person) {
  disableTarget.value = person
  disableImpact.value = null
  try {
    // GET /impact 是**纯读**端点：一行都不写，所以可以在点按钮时就先拉一次
    disableImpact.value = await getImpact(person.personCode)
  } catch (e) {
    disableImpact.value = null
  }
}

async function confirmDisable() {
  if (!disableTarget.value) return
  disableSubmitting.value = true
  try {
    await disableContact(disableTarget.value.personCode, true)
    ElMessage.success(`已停用「${disableTarget.value.displayName}」，他的人脸已退回待确认队列`)
    disableTarget.value = null
    await reload()
    await review.fetchBadge()
  } finally {
    disableSubmitting.value = false
  }
}
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 顶栏 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">人物库</h2>
        <p class="pb-hint mt-1">
          共 {{ persons.total.toLocaleString('zh-CN') }} 人 · 停用的人默认不显示
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <div class="flex items-center gap-2">
          <label class="pb-hint" for="pp-search">搜索</label>
          <el-input
            id="pp-search"
            v-model="filters.keyword"
            class="w-56"
            placeholder="姓名 / 拼音 / 邮箱 / 电话"
            clearable
          >
            <template #prefix>
              <Search class="h-4 w-4 text-ink-weak" aria-hidden="true" />
            </template>
          </el-input>
        </div>

        <div class="flex items-center gap-2">
          <label class="pb-hint" for="pp-family">家庭组</label>
          <el-select
            id="pp-family"
            v-model="filters.familyGroupCode"
            class="w-32"
            placeholder="全部"
            clearable
          >
            <el-option
              v-for="group in families"
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

    <!-- 分类 chips（多值，筛选是单选；再点一次取消） -->
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
        @click="filters.category = filters.category === item.value ? '' : item.value"
      >
        {{ item.label }}
      </button>
      <el-button v-if="persons.hasFilter" size="small" text @click="persons.resetFilters()">
        清除筛选
      </el-button>
    </div>

    <!-- 错误提示：宁可显式报错，也不要一个空列表让人猜 -->
    <p
      v-if="persons.error"
      class="flex items-start gap-2 rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink"
      role="alert"
    >
      <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        {{ persons.error }}
        <el-button size="small" text @click="reload">重试</el-button>
      </span>
    </p>

    <!-- 骨架：卡片形状先占位，避免数据到达时整页往下跳一格 -->
    <ul
      v-if="persons.loading && !persons.items.length"
      class="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5"
      aria-hidden="true"
    >
      <li v-for="n in 10" :key="n">
        <div class="pb-card animate-pulse p-4">
          <div class="mx-auto h-24 w-24 rounded-full bg-skeleton" />
          <div class="mx-auto mt-3 h-4 w-24 rounded-btn bg-skeleton" />
          <div class="mx-auto mt-2 h-3 w-16 rounded-btn bg-skeleton" />
        </div>
      </li>
    </ul>

    <!-- 空状态：名册为空 -> 给三步引导，而不是一句「暂无数据」 -->
    <section
      v-else-if="!hasAny"
      class="pb-card space-y-4 px-4 py-12 text-center"
      aria-labelledby="pp-empty-title"
    >
      <UserRound class="mx-auto h-8 w-8 text-ink-weak" aria-hidden="true" />
      <h3 id="pp-empty-title" class="text-body text-ink">名册还是空的</h3>
      <p class="mx-auto max-w-xl text-caption text-ink-sub">
        人物库来自<b>联系人导入</b>：先把通讯录 CSV / vCard 导进来建档，再扫描照片提取人脸，
        最后在待确认队列里把脸认到人 —— 三步之后这里才会有内容。
      </p>
      <ol class="mx-auto max-w-xl space-y-1.5 text-left text-caption text-ink-sub">
        <li class="flex items-start gap-2">
          <span class="tabular-nums text-ink-weak">1</span>
          <span><b>导入联系人</b> —— 只建档案，不关联照片</span>
        </li>
        <li class="flex items-start gap-2">
          <span class="tabular-nums text-ink-weak">2</span>
          <span><b>扫描照片</b> —— 原图只读，扫描不改任何文件</span>
        </li>
        <li class="flex items-start gap-2">
          <span class="tabular-nums text-ink-weak">3</span>
          <span><b>确认人脸</b> —— 这一步的质量直接决定后续自动归属的准确度</span>
        </li>
      </ol>
      <el-button type="primary" @click="openCreate">
        <Contact class="mr-1 h-4 w-4" aria-hidden="true" />先建一个人物
      </el-button>
    </section>

    <section
      v-else-if="!persons.items.length"
      class="pb-card px-4 py-12 text-center text-body text-ink-weak"
    >
      <FolderSearch class="mx-auto mb-2 h-6 w-6" aria-hidden="true" />
      没有符合筛选条件的人。搜索支持汉字、全拼（wangxiaoming）与拼音首字母（wxm）。
      <el-button class="ml-2" size="small" @click="persons.resetFilters()">清除筛选</el-button>
    </section>

    <!-- 人物卡片网格 -->
    <ul v-else class="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      <li v-for="person in persons.items" :key="person.personCode">
        <PersonCard
          :person="person"
          @edit="editTarget = $event"
          @disable="openDisable"
        />
      </li>
    </ul>

    <!-- 分页：名册是小表，但正式库实测有 2000+ 人，分页仍然必要 -->
    <footer v-if="persons.total > persons.size" class="flex flex-wrap items-center justify-between gap-3">
      <p class="text-caption text-ink-sub">
        第 {{ persons.page }} 页 · 共 {{ persons.total.toLocaleString('zh-CN') }} 人
      </p>
      <el-pagination
        layout="prev, pager, next"
        background
        :total="persons.total"
        :page-size="persons.size"
        :current-page="persons.page"
        @current-change="(value) => (persons.page = value)"
      />
    </footer>

    <p class="pb-hint">
      本工具不提供导出入口，迁移与备份请用设置页的「备份 / 恢复」。
      人物只能<b>停用</b>，不提供删除 —— 一个误点不该把几小时的确认工作清零。
    </p>

    <!-- 新建人物：与 P-05 编辑抽屉共用 PersonForm（不得有第二份表单） -->
    <el-dialog v-model="createVisible" title="新建人物" width="560px">
      <PersonForm
        :families="families"
        :submitting="submitting"
        @cancel="createVisible = false"
        @submit="submitCreate"
      />
    </el-dialog>

    <!-- 编辑抽屉：同一个 PersonForm，只是数据源与提交目标不同 -->
    <el-drawer
      :model-value="Boolean(editTarget)"
      title="编辑资料"
      size="480px"
      @update:model-value="(value) => !value && (editTarget = null)"
    >
      <PersonForm
        v-if="editTarget"
        :person="editTarget"
        :families="families"
        :submitting="submitting"
        @cancel="editTarget = null"
        @submit="submitEdit"
      />
    </el-drawer>

    <!-- 停用：先复述影响面（设计稿 §4.8） -->
    <el-dialog
      :model-value="Boolean(disableTarget)"
      title="停用这个人？"
      width="460px"
      @update:model-value="(value) => !value && (disableTarget = null)"
    >
      <div class="space-y-3">
        <p class="text-body text-ink-sub">
          停用「<b>{{ disableTarget?.displayName }}</b
          >」会发生：
        </p>
        <ul v-if="disableImpact" class="space-y-1 text-caption text-ink-sub">
          <li>
            · 该人的 <b>{{ disableImpact.centroidCount ?? 0 }}</b> 个年代档质心将被删除
            （不再参与人脸匹配）
          </li>
          <li>· 该人的 <b>{{ disableImpact.faceCount ?? 0 }}</b> 张人脸将退回<b>待确认队列</b></li>
          <li>
            · 照片-人员关联将按剩余人脸情况重新整理（共
            {{ disableImpact.photoCount ?? 0 }} 张照片）
          </li>
        </ul>
        <p v-else class="pb-hint">正在读取影响面…</p>
        <p
          v-if="(disableImpact?.pendingAfter ?? 0) > (disableImpact?.pendingCount ?? 0)"
          class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
        >
          <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
          待确认队列将从 {{ disableImpact.pendingCount }} 涨到
          {{ disableImpact.pendingAfter }}，队列会明显变长。
        </p>
        <p class="pb-hint">
          停用是<b>可恢复</b>的（不是删除）；但恢复后需重新确认人脸才能自动匹配。
        </p>
      </div>
      <template #footer>
        <el-button @click="disableTarget = null">取消</el-button>
        <el-button
          type="danger"
          :loading="disableSubmitting"
          :disabled="!disableImpact"
          @click="confirmDisable"
        >
          <CircleSlash class="mr-1 h-4 w-4" aria-hidden="true" />确认停用
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>