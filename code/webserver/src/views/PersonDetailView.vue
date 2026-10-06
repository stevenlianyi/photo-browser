<!--
  P-05 人物详情（设计稿 §4.5）
  ------------------------------------------------------------
  头部：头像 + 姓名 + 家庭关系 + 分类 + 照片数 / 年代跨度
  操作：合并到… ｜ 撤销上次合并 ｜ 编辑资料（抽屉）｜ 操作历史
  Tab1 时间轴：**按年代桶分组**（对齐 S0 结论：分桶是刚需），空桶不显示
  Tab2 人脸样本：**必须分两段** —— 人工确认（实线）/ 自动归属（虚线）。
        混在一起用户无法判断哪些值得复核；自动段每张带「✗ 移除」
  质心健康度：自动 ≫ 确认时主动提示，用数据引导用户去纠错
-->
<script setup>
import { computed, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  ArrowLeft,
  CircleCheck,
  History,
  Merge,
  Pencil,
  TriangleAlert,
  Undo2,
  UserRound,
  X,
} from 'lucide-vue-next'
import PersonForm from '@/components/common/PersonForm.vue'
import { useReviewStore } from '@/store/review'

const route = useRoute()
const review = useReviewStore()

const activeTab = ref('timeline')

/** 结构示例：步骤 12 接 /api/persons/{personCode} 后由真实数据替换 */
const PERSON = {
  personCode: 'P-zhongwen',
  displayName: 'zhongwen lian',
  familyName: 'lian',
  relation: '兄',
  birthday: '1985-03-07',
  categories: ['家人'],
  familyName_label: '陈家',
  photoCount: 486,
  faceCount: 612,
  confirmedCount: 412,
  autoCount: 74,
  yearFrom: 1998,
  yearTo: 2021,
}

/** Tab1：按年代桶分组的照片（桶自适应：童年 3 年 / 成年 10 年） */
const BUCKETS = [
  { bucketKey: '1998–2007', count: 96, confirmed: 88 },
  { bucketKey: '2008–2017', count: 244, confirmed: 212 },
  { bucketKey: '2018–2021', count: 72, confirmed: 68 },
]

/** Tab2：人脸样本分两段 —— 混在一起就分不清哪些值得复核 */
const CONFIRMED_SAMPLES = [
  { faceCode: 'F-0001', photoCode: 'P-000231', similarity: 0.86, year: '2006' },
  { faceCode: 'F-0014', photoCode: 'P-000455', similarity: 0.79, year: '2009' },
  { faceCode: 'F-0031', photoCode: 'P-001120', similarity: 0.91, year: '2014' },
]
const AUTO_SAMPLES = [
  { faceCode: 'F-0102', photoCode: 'P-002015', similarity: 0.58, year: '2018' },
  { faceCode: 'F-0139', photoCode: 'P-002480', similarity: 0.52, year: '2020' },
  { faceCode: 'F-0177', photoCode: 'P-003101', similarity: 0.61, year: '2021' },
]

/** 健康度：自动 ≫ 人工确认 ⇒ 质心可能被自动样本拉偏 */
const healthHint = computed(() => {
  if (PERSON.confirmedCount === 0) return '此人的质心还没有人工确认样本，不参与自动匹配。'
  if (PERSON.autoCount > PERSON.confirmedCount) {
    return `自动归属 ${PERSON.autoCount} 张远多于人工确认 ${PERSON.confirmedCount} 张，识别质量偏低，建议先人工确认 ${PERSON.autoCount} 张。`
  }
  return ''
})

const editVisible = ref(false)
const personForForm = computed(() =>
  editVisible.value
    ? {
        personCode: PERSON.personCode,
        displayName: PERSON.displayName,
        familyName: PERSON.familyName,
        relation: PERSON.relation,
        birthday: PERSON.birthday,
        categories: PERSON.categories,
      }
    : null,
)
const FAMILY_OPTIONS = [{ familyCode: 'F-chen', familyName: '陈家' }]
</script>

<template>
  <div class="pb-page space-y-4">
    <RouterLink
      to="/people"
      class="inline-flex items-center gap-1 text-caption text-ink-sub hover:text-ink"
    >
      <ArrowLeft class="h-3.5 w-3.5" aria-hidden="true" />人物库
    </RouterLink>

    <!-- 头部 -->
    <section class="pb-card p-4">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="flex min-w-0 items-center gap-4">
          <span
            class="flex h-16 w-16 shrink-0 items-center justify-center rounded-full border border-line bg-surface text-ink-weak"
            aria-hidden="true"
          >
            <UserRound class="h-7 w-7" />
          </span>
          <div class="min-w-0">
            <h2 class="truncate text-title text-ink">{{ PERSON.displayName }}</h2>
            <p class="mt-1 text-body text-ink-sub">
              {{ PERSON.relation ? `${PERSON.relation} · ` : '' }}{{ PERSON.familyName_label }}
              <span class="mx-1 text-ink-weak">|</span>
              {{ PERSON.birthday ? `${PERSON.birthday} · ` : '' }}婴儿 → 成年
            </p>
            <ul class="mt-2 flex flex-wrap gap-1.5">
              <li
                v-for="name in PERSON.categories"
                :key="name"
                class="rounded-btn bg-accent-soft px-2 py-0.5 text-caption text-ink-sub"
              >
                {{ name }}
              </li>
              <li class="rounded-btn bg-surface px-2 py-0.5 text-caption text-ink-sub">
                {{ PERSON.yearFrom }}–{{ PERSON.yearTo }}
              </li>
            </ul>
          </div>
        </div>

        <!-- 操作都在手边，不藏三级菜单 -->
        <div class="flex flex-wrap gap-2">
          <el-button>
            <Merge class="mr-1 h-4 w-4" aria-hidden="true" />合并到…
          </el-button>
          <el-button>
            <Undo2 class="mr-1 h-4 w-4" aria-hidden="true" />撤销上次合并
          </el-button>
          <el-button @click="editVisible = true">
            <Pencil class="mr-1 h-4 w-4" aria-hidden="true" />编辑资料
          </el-button>
          <el-button>
            <History class="mr-1 h-4 w-4" aria-hidden="true" />操作历史
          </el-button>
        </div>
      </div>

      <!-- 质心健康度 -->
      <p
        v-if="healthHint"
        class="mt-4 flex items-start gap-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>{{ healthHint }}</span>
      </p>
    </section>

    <el-tabs v-model="activeTab" class="pb-card px-4 pb-4">
      <!-- Tab1 时间轴 -->
      <el-tab-pane label="时间轴" name="timeline">
        <div class="space-y-4 pt-2">
          <div v-for="bucket in BUCKETS" :key="bucket.bucketKey" class="flex gap-4">
            <p class="w-28 shrink-0 pt-1 text-caption tabular-nums text-ink-weak">
              {{ bucket.bucketKey }}
            </p>
            <div class="flex-1">
              <p class="mb-2 text-caption text-ink-sub">
                {{ bucket.count }} 张 · 其中 {{ bucket.confirmed }} 张已人工确认
              </p>
              <ul class="flex flex-wrap gap-2">
                <li v-for="index in Math.min(bucket.count, 8)" :key="index">
                  <RouterLink
                    :to="`/photos/P-${bucket.bucketKey.slice(0, 4)}-${index}`"
                    class="block"
                    :aria-label="`${bucket.bucketKey} 的第 ${index} 张照片`"
                  >
                    <span class="pb-photo-frame block h-16 w-16" />
                  </RouterLink>
                </li>
                <li
                  v-if="bucket.count > 8"
                  class="flex h-16 w-16 items-center justify-center rounded-thumb border border-dashed border-line text-caption text-ink-weak"
                >
                  +{{ bucket.count - 8 }}
                </li>
              </ul>
            </div>
          </div>
          <p class="pb-hint">时间轴按年代桶分组（童年 3 年 / 成年 10 年），空桶不显示。</p>
        </div>
      </el-tab-pane>

      <!-- Tab2 人脸样本：人工确认 / 自动归属两段 -->
      <el-tab-pane label="人脸样本" name="faces">
        <div class="space-y-6 pt-2">
          <section aria-labelledby="pd-confirmed-title">
            <h3 id="pd-confirmed-title" class="flex items-center gap-2 text-body text-ink">
              <CircleCheck class="h-4 w-4 text-success" aria-hidden="true" />
              人工确认
              <span class="tabular-nums text-ink-weak">{{ PERSON.confirmedCount }}</span>
            </h3>
            <ul class="mt-3 flex flex-wrap gap-3">
              <li v-for="sample in CONFIRMED_SAMPLES" :key="sample.faceCode">
                <RouterLink
                  :to="`/photos/${sample.photoCode}`"
                  class="block"
                  :aria-label="`${sample.year} 年照片，已人工确认`"
                >
                  <span class="pb-photo-frame block h-16 w-16 border-2 border-solid border-success" />
                  <span class="mt-1 block text-center text-caption tabular-nums text-ink-weak">
                    {{ sample.similarity.toFixed(2) }}
                  </span>
                </RouterLink>
              </li>
              <li class="pb-hint self-end">实线框 = 已经你确认过，不会再出现在任何队列里</li>
            </ul>
          </section>

          <section aria-labelledby="pd-auto-title">
            <h3 id="pd-auto-title" class="flex items-center gap-2 text-body text-ink">
              <span
                class="inline-block h-3 w-4 border-t-2 border-dashed border-success"
                aria-hidden="true"
              />
              自动归属
              <span class="tabular-nums text-ink-weak">{{ PERSON.autoCount }}</span>
            </h3>
            <ul class="mt-3 flex flex-wrap gap-3">
              <li v-for="sample in AUTO_SAMPLES" :key="sample.faceCode" class="w-16">
                <RouterLink
                  :to="`/photos/${sample.photoCode}`"
                  class="block"
                  :aria-label="`${sample.year} 年照片，机器自动认的`"
                >
                  <span class="pb-photo-frame block h-16 w-16 border-2 border-dashed border-success" />
                  <span class="mt-1 block text-center text-caption tabular-nums text-ink-weak">
                    {{ sample.similarity.toFixed(2) }}
                  </span>
                </RouterLink>
                <el-button
                  size="small"
                  text
                  type="danger"
                  class="mt-1 w-full"
                  :aria-label="`把这张脸的归属移除（${sample.year} 年）`"
                >
                  <X class="mr-1 h-3 w-3" aria-hidden="true" />移除
                </el-button>
              </li>
            </ul>
            <p class="pb-hint mt-3">
              虚线框 = 机器认的，还没经过你确认。它们同时出现在「待确认 › 我不同意」里，
              发现认错可以就地移除。
            </p>
          </section>

          <p class="pb-hint">
            当前待确认队列剩余 {{ review.pendingCount }} 条 · 我不同意
            {{ review.disputedCount }} 条。
          </p>
        </div>
      </el-tab-pane>
    </el-tabs>

    <p class="pb-hint">本页为结构示例，接入 /api/persons/{personCode} 后由真实数据替换。</p>

    <!-- 编辑资料抽屉：与 P-04 新建对话框共用 PersonForm -->
    <el-drawer v-model="editVisible" title="编辑资料" size="480px">
      <PersonForm
        :person="personForForm"
        :families="FAMILY_OPTIONS"
        @cancel="editVisible = false"
        @submit="editVisible = false"
      />
    </el-drawer>
  </div>
</template>