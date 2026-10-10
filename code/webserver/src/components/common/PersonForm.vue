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
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import { PERSON_CATEGORIES, PERSON_RELATIONS, usePersonsStore } from '@/store/persons'

const props = defineProps({
  /** 传入人员对象 = 编辑模式；不传 = 新建模式 */
  person: { type: Object, default: null },
  /** 家庭组下拉选项：[{ familyCode, familyName }] */
  families: { type: Array, default: () => [] },
  /**
   * 提交中。**由调用方持有**：表单自己不调接口 ——
   * P-04 建档后要跳详情、P-05 改完生日要按 centroidRebuilt 给不同 Toast，
   * 把提交塞进表单就得靠一堆回调把这两件事绕回来。
   * 不给这个 prop 时行为与之前完全一致（只是按钮不禁用）。
   */
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits(['cancel', 'submit'])

/**
 * 唯一一处「表单自己碰数据」的地方：**新建家庭组**。
 *
 * 提交这个人的动作仍然由调用方持有（见 submitting 的说明，P-04 与 P-05
 * 的提交后动作不同）。但「新建家庭组」在四处调用方里要做的事**完全一样**
 * （建组 → 刷新下拉 → 归入新组），写成 emit 再让四个页面各抄一遍，
 * 迟早有一处漏掉刷新，表现成「建了但下拉里没有」。
 */
const persons = usePersonsStore()

/**
 * 家庭关系下拉的选项来自 store 的 `PERSON_RELATIONS`，这里不再自己列一份：
 * 卡片 / 详情头部 / 改判候选都用 store 里的 `relationLabelOf()` 翻译同一个值，
 * 表单里再抄一份的话，改了这里、忘了那里，就会出现
 * 「下拉里选的是「配偶」，卡片上显示 spouse」。
 */
const RELATIONS = PERSON_RELATIONS

/**
 * 分类：**值是英文码，显示是中文**（选项同样来自 store，理由同 relations）。
 *
 * ⚠️ 这里存的一律是 `family/friend/colleague`，不是「家人/朋友/同事」。
 * 因为 `/api/contacts?category=family` 与 `pb_person_category.category`
 * 都是按英文码比对的（`c.category = 'family'`）；存中文的话，
 * 人物库的分类筛选会**永远筛不出东西**，而且不报错 ——
 * 导入（步骤 8 走 `Categories` 列）与界面编辑会各存一套值，
 * 同一个人的分类在两处对不上。
 */
const CATEGORY_OPTIONS = PERSON_CATEGORIES

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

/**
 * 库里已有的、不属于三档标准值的分类。
 *
 * ⚠️ 必须把它们**画出来**：导入来的联系人常常带自定义类别
 *    （Outlook 的 Categories 列原样落库，`normalizeCategory` 认不出来就存原文），
 *    只画三档标准值的话，用户在这里看到的分类比库里少 ——
 *    一保存就把看不见的那几个**抹掉**了（PATCH 的 categories 是全量覆盖）。
 */
const customCategories = computed(() =>
  form.categories.filter((name) => !CATEGORY_OPTIONS.some((option) => option.value === name)),
)

/* ---- 「添加分类」：内联输入，不弹二级对话框 ---- */
const addingCategory = ref(false)
const newCategoryName = ref('')
const newCategoryInput = ref(null)

function startAddCategory() {
  addingCategory.value = true
  newCategoryName.value = ''
  // 点开即可输入：不自动聚焦的话这里会多出一次「再点一下输入框」的动作
  nextTick(() => newCategoryInput.value?.focus?.())
}

/**
 * 输入的内容先按「显示标签」归一一次：用户敲「家人 / 同事 / friend」时应落到
 * `family / colleague / friend` 上 —— 否则会和上面那三个标准 chip 撞成两张一模一样的标签，
 * 而它们在库里是两个值（`/api/contacts?category=family` 只认得前者）。
 * 认不出来的一律**原样保留**（后端 `normalizeCategory` 同款口径，截到 32 字）：
 * 联系人里本来就有「同事:|同事:同事」这类自己写的分类，不该被吞掉。
 */
function resolveCategory(text) {
  const raw = String(text || '').trim()
  if (!raw) return ''
  const hit = CATEGORY_OPTIONS.find(
    (option) => option.value === raw.toLowerCase() || option.label === raw,
  )
  return hit ? hit.value : raw.slice(0, 32)
}

function commitNewCategory() {
  const name = resolveCategory(newCategoryName.value)
  newCategoryName.value = ''
  addingCategory.value = false
  if (!name) return
  if (!form.categories.includes(name)) form.categories.push(name)
}

/** 取消：先把输入清空再收起 —— 收起会触发 blur，blur 又调 commit，不清空就白取消了 */
function cancelNewCategory() {
  newCategoryName.value = ''
  addingCategory.value = false
}

/* ---- 「新建家庭组」：与「添加分类」同款内联输入 ---- */
const addingFamily = ref(false)
const newFamilyName = ref('')
const newFamilyInput = ref(null)
const creatingFamily = ref(false)

function startAddFamily() {
  addingFamily.value = true
  newFamilyName.value = ''
  nextTick(() => newFamilyInput.value?.focus?.())
}

/**
 * 建新组并**当场归入**：建完还要用户回下拉里再选一次，等于白建。
 *
 * 同名组直接复用而不是再建一个：`pb_person.familyGroupCode` 是弱关联，
 * 组重名的后果是人物库筛选下拉里出现两条一模一样的「陈家」，谁也没法选。
 */
async function commitNewFamily() {
  const name = newFamilyName.value.trim()
  newFamilyName.value = ''
  addingFamily.value = false
  if (!name) return
  const existed = (props.families || []).find(
    (group) => String(group.familyName || '').trim() === name,
  )
  if (existed) {
    form.familyGroupCode = existed.familyCode
    return
  }
  creatingFamily.value = true
  try {
    const created = await persons.createFamily(name)
    if (created.familyCode) form.familyGroupCode = created.familyCode
    ElMessage.success(`已新建家庭组「${created.familyName}」并归入`)
  } catch {
    // 失败提示由 api/request.js 的统一拦截器弹出（这里再弹一次就是两条一样的 Toast）
  } finally {
    creatingFamily.value = false
  }
}

function cancelNewFamily() {
  newFamilyName.value = ''
  addingFamily.value = false
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
          <!-- 这三个字段（家庭关系 / 家庭组 / 分类）最容易被当成一件事，
               各给一句话说明它们各管什么，比在别处解释便宜得多 -->
          <p class="pb-hint mt-1">TA 与你（这个档案的主人）是什么关系，单选。</p>
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

            <el-button v-if="!addingFamily" :loading="creatingFamily" @click="startAddFamily">
              新建家庭组
            </el-button>
          </div>

          <!-- 内联新增，建完**当场归入**新组。
               ⚠️ 这里与「添加分类」有一处刻意的不同：**失焦不提交**。
                  分类写错只是本地的几个字，而这里一提交就是一条 pb_family
                  （家庭组没有删除接口，建错了只能改名），
                  点一下旁边就悄悄多出一个组是不可接受的。 -->
          <div v-if="addingFamily" class="mt-2 flex items-center gap-2">
            <el-input
              ref="newFamilyInput"
              v-model="newFamilyName"
              class="flex-1"
              size="small"
              maxlength="64"
              placeholder="家庭组名（例：陈家），回车确定"
              aria-label="新家庭组名"
              @keydown.enter.prevent="commitNewFamily"
              @keydown.esc="cancelNewFamily"
            />
            <el-button size="small" type="primary" :loading="creatingFamily" @click="commitNewFamily">
              新建并归入
            </el-button>
            <el-button size="small" :disabled="creatingFamily" @click="cancelNewFamily">
              取消
            </el-button>
          </div>

          <p class="pb-hint mt-1">
            TA 属于哪个「家」，单选 —— 一个人只有一个家庭组（娘家 / 夫家二选一）。
            下拉里没有就现建一个，建完自动归入。
          </p>
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
          生日决定年代档划分（0–18 岁 3 年 / 18 岁以上 10 年）。改动会使该人的质心全部重算、
          匹配结果随之变化；其它字段的改动不会有这个影响。
          填一个大概的生日（例如：2000-01-01）也有助于提高人脸识别精度。
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
            v-for="option in CATEGORY_OPTIONS"
            :key="option.value"
            type="button"
            class="rounded-btn border px-3 py-1 text-caption transition-colors duration-150"
            :class="
              form.categories.includes(option.value)
                ? 'border-brand bg-brand-soft text-brand-ink'
                : 'border-line bg-card text-ink-sub hover:bg-surface'
            "
            :aria-pressed="form.categories.includes(option.value)"
            @click="toggleCategory(option.value)"
          >
            {{ option.label }}
          </button>

          <!-- 自定义分类（导入带来的、以及这里新加的）：点一下即取消 -->
          <button
            v-for="name in customCategories"
            :key="name"
            type="button"
            class="inline-flex items-center gap-1 rounded-btn border border-brand bg-brand-soft px-3 py-1 text-caption text-brand-ink transition-colors duration-150 hover:bg-surface"
            aria-pressed="true"
            @click="toggleCategory(name)"
          >
            {{ name }}
            <X class="h-3 w-3" aria-hidden="true" />
          </button>

          <!-- 内联新增：分类是个小字段，不值得再弹一层对话框 -->
          <el-input
            v-if="addingCategory"
            ref="newCategoryInput"
            v-model="newCategoryName"
            class="w-40"
            size="small"
            maxlength="32"
            placeholder="新分类名，回车确定"
            aria-label="新分类名"
            @keydown.enter.prevent="commitNewCategory"
            @keydown.esc="cancelNewCategory"
            @blur="commitNewCategory"
          />
          <el-button v-else size="small" @click="startAddCategory">添加分类</el-button>
        </div>
        <p class="pb-hint mt-1">
          标签，可同时属于多类（与上面的家庭关系 / 家庭组互不影响，它们各管一件事）。
          新增的分类按自定义标签保存（回车确定，Esc 取消）；库里已有的自定义分类会一并列出，
          点一下即取消。
        </p>
      </div>
    </fieldset>

    <!-- 底部：来源提示 + 操作 -->
    <div class="space-y-3 border-t border-line pt-4">
      <p v-if="isEdit" class="pb-hint">
        该联系人来自 Outlook 导入，重新导入会覆盖这里的修改。本工具不提供导出入口，
        迁移与备份请使用设置页的「备份 / 恢复」。
      </p>
      <div class="flex justify-end gap-2">
        <el-button :disabled="props.submitting" @click="emit('cancel')">取消</el-button>
        <el-button
          type="primary"
          native-type="submit"
          :loading="props.submitting"
          :disabled="!canSubmit"
        >
          {{ isEdit ? '保存' : '创建' }}
        </el-button>
      </div>
    </div>
  </form>
</template>