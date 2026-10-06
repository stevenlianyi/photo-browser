<!--
  PersonForm —— **唯一**的人物表单组件（设计稿 §4.8，DR-17/18/19）
  ------------------------------------------------------------
  P-04「+ 新建人物」对话框与 P-05「编辑资料」抽屉共用这一个组件：
  两处只是数据源与提交目标不同，字段与校验必须一致 ——
  写成两份的话，撞名提示、停用入口、生日影响说明一定会只落在一处。

  三组布局（刻意不合并）：
    ① 基本信息 ② 生日（单独一组 + 影响说明） ③ 联系方式与分类
  生日单独成组的原因：它是**唯一会触发质心重算**的字段，
  和其它字段混在一排里，用户不会意识到改它的代价。

  ⚠️ 步骤 10 只出结构与文案，提交逻辑（PATCH / POST、409 撞名、centroidRebuilt
     提示）在步骤 12 接上；对外接口（props / emits）先定好，步骤 12 只填实现。
-->
<script setup>
import { computed, reactive, watch } from 'vue'

const props = defineProps({
  /** 传入人员对象 = 编辑模式；不传 = 新建模式 */
  person: { type: Object, default: null },
  /** 家庭组下拉选项：[{ familyCode, familyName }] */
  families: { type: Array, default: () => [] },
})

const emit = defineEmits(['cancel', 'submit'])

const RELATIONS = [
  { value: 'parent', label: '父亲 / 母亲' },
  { value: 'spouse', label: '配偶' },
  { value: 'child', label: '子女' },
  { value: 'sibling', label: '兄弟姐妹' },
  { value: 'colleague', label: '同事' },
]

const CATEGORY_OPTIONS = ['家人', '朋友', '同事']

function emptyForm() {
  return {
    displayName: '',
    familyName: '',
    relation: '',
    familyGroupCode: '',
    birthday: '',
    email: '',
    phone: '',
    categories: [],
  }
}

const form = reactive(emptyForm())

// 编辑模式带入已有值；新建模式清空。watch 而不是 onMounted ——
// 抽屉/对话框可能复用同一个组件实例，切换对象时要重新灌值。
watch(
  () => props.person,
  (person) => {
    Object.assign(form, emptyForm())
    if (!person) return
    form.displayName = person.displayName ?? ''
    form.familyName = person.familyName ?? ''
    form.relation = person.relation ?? ''
    form.familyGroupCode = person.familyGroupCode ?? ''
    form.birthday = person.birthday ?? ''
    form.email = person.email ?? ''
    form.phone = person.phone ?? ''
    form.categories = Array.isArray(person.categories) ? [...person.categories] : []
  },
  { immediate: true },
)

const isEdit = computed(() => Boolean(props.person))
/** 显示名是必填项 */
const canSubmit = computed(() => form.displayName.trim().length > 0)

function toggleCategory(name) {
  const index = form.categories.indexOf(name)
  if (index >= 0) form.categories.splice(index, 1)
  else form.categories.push(name)
}

function submit() {
  if (!canSubmit.value) return
  emit('submit', { ...form })
}
</script>

<template>
  <form class="space-y-6" novalidate @submit.prevent="submit">
    <!-- ① 基本信息 -->
    <fieldset class="space-y-3">
      <legend class="pb-section-title">基本信息</legend>

      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div class="sm:col-span-2">
          <label class="pb-hint block" for="pf-display-name">显示名 *</label>
          <el-input
            id="pf-display-name"
            v-model="form.displayName"
            placeholder="例：zhongwen lian"
            maxlength="64"
            show-word-limit
          />
          <p class="pb-hint mt-1">重名极可能就是同一个人，保存时若撞名会提示「去合并」。</p>
        </div>

        <div>
          <label class="pb-hint block" for="pf-family-name">姓氏</label>
          <el-input id="pf-family-name" v-model="form.familyName" placeholder="例：lian" />
        </div>

        <div>
          <label class="pb-hint block" for="pf-relation">家庭关系</label>
          <el-select
            id="pf-relation"
            v-model="form.relation"
            class="w-full"
            placeholder="请选择"
            clearable
          >
            <el-option
              v-for="item in RELATIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </div>

        <div class="sm:col-span-2">
          <span class="pb-hint block">家庭组</span>
          <div class="flex items-center gap-2">
            <el-select
              v-model="form.familyGroupCode"
              class="flex-1"
              placeholder="未归入任何家庭组"
              clearable
            >
              <el-option
                v-for="group in families"
                :key="group.familyCode"
                :label="group.familyName"
                :value="group.familyCode"
              />
            </el-select>
            <el-button>新建家庭组</el-button>
          </div>
        </div>
      </div>
    </fieldset>

    <!-- ② 生日（单独一组：唯一会触发质心重算的字段） -->
    <fieldset class="space-y-3 border-t border-line pt-4">
      <legend class="pb-section-title">生日</legend>
      <div>
        <label class="pb-hint block" for="pf-birthday">出生日期</label>
        <el-date-picker
          id="pf-birthday"
          v-model="form.birthday"
          type="date"
          value-format="YYYY-MM-DD"
          class="w-full"
          placeholder="未填写"
        />
        <p class="pb-hint mt-1">
          生日决定年代分桶（0–18 岁 3 年 / 18 岁以上 10 年）。改动会使该人的质心全部重算、
          匹配结果随之变化；其它字段的改动不会有这个影响。
        </p>
      </div>
    </fieldset>

    <!-- ③ 联系方式与分类 -->
    <fieldset class="space-y-3 border-t border-line pt-4">
      <legend class="pb-section-title">联系方式与分类</legend>

      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label class="pb-hint block" for="pf-email">邮箱</label>
          <el-input id="pf-email" v-model="form.email" placeholder="例：lian@example.com" />
        </div>
        <div>
          <label class="pb-hint block" for="pf-phone">电话</label>
          <el-input id="pf-phone" v-model="form.phone" placeholder="例：138…" />
        </div>
      </div>

      <div>
        <span class="pb-hint block">分类（可多选）</span>
        <div class="mt-1 flex flex-wrap items-center gap-2">
          <button
            v-for="name in CATEGORY_OPTIONS"
            :key="name"
            type="button"
            class="rounded-btn border px-3 py-1 text-caption transition-colors duration-150"
            :class="
              form.categories.includes(name)
                ? 'border-brand bg-brand-soft text-brand-ink'
                : 'border-line bg-card text-ink-sub hover:bg-surface'
            "
            :aria-pressed="form.categories.includes(name)"
            @click="toggleCategory(name)"
          >
            {{ name }}
          </button>
          <el-button size="small">添加分类</el-button>
        </div>
      </div>
    </fieldset>

    <!-- 底部：来源提示 + 操作 -->
    <div class="space-y-3 border-t border-line pt-4">
      <p v-if="isEdit" class="pb-hint">
        该联系人来自 Outlook 导入，重新导入会覆盖这里的修改。本工具不提供导出入口，
        迁移与备份请使用设置页的「备份 / 恢复」。
      </p>
      <div class="flex justify-end gap-2">
        <el-button @click="emit('cancel')">取消</el-button>
        <el-button type="primary" native-type="submit" :disabled="!canSubmit">
          {{ isEdit ? '保存' : '创建' }}
        </el-button>
      </div>
    </div>
  </form>
</template>