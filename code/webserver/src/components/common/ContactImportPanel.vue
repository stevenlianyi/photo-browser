<!--
  联系人导入面板（P-08 设置页 · 数据维护）
  ------------------------------------------------------------
  为什么需要它：导入此前只有命令行通道（`main/cli.py import-csv / import-vcard`
  与 `tools/import_contacts.py`），而人物库的空状态里写着「先导入联系人」——
  用户照着做却找不到入口。这里把**已有的两个导入端点**接到界面上，
  不新增任何写入逻辑：后端 `POST /api/contacts/import/{csv,vcard}` 就是
  CLI 那套 `plan/apply` 的 HTTP 包装（同一个 `contactCommon`）。

  三条纪律（每一条都写在界面上，别只写在代码里）
  ----------------------------------------------
  ① **先预览、再导入**：`?dryRun=1` 是后端原生能力（只算不写），而导入会
     **覆盖**同名档案里被界面改过的字段（DR-17 里记的 Q-14 取舍）⇒
     计划必须先摆到用户面前，尤其要看见「跳过」与「被自动改名」的条目。
  ② **只建档案，不关联照片**：导入只写 pb_person / pb_person_category /
     pb_family（vCard 内嵌的头像还会落一张图）。照片归属永远是人脸识别的
     事 —— 界面上不能给人「导完就自动认人」的错觉。
  ③ **不提供导出**（DR-17）：这一块只有「进」，没有「出」；备份/迁移走同页的
     整库拷贝（`backup.py`）。

  两个实现上的取舍
  ----------------
  · **多文件逐个调接口**，不并发：服务端一次只收一个文件（浏览器的
    `<input type=file>` 给不了「一个联系人一个 .vcf」那种目录形态），
    而导入会写同一个库 —— 并发只会让失败归因变难，快不了多少。
  · **导入成功后清掉计划**：重新导入是幂等的，但「刚导完又点一次」这种误操作
    没有意义，清掉计划会让「确认导入」自动回到禁用态，要看计划就再预览一次。
-->
<script setup>
import { computed, onMounted, ref } from 'vue'
import { FileUp, ListChecks, TriangleAlert, CircleCheck } from 'lucide-vue-next'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import { importCsv, importVcard } from '@/api/contacts'
import { formatFileSize } from '@/utils/format'

const props = defineProps({
  /** 目标锚点：人物库空状态会跳到 `#contact-import`（本组件的根 id） */
  anchor: { type: String, default: 'contact-import' },
})

/** 已选文件（保持用户选择的顺序；导入也按这个顺序） */
const files = ref([])
const previewing = ref(false)
const importing = ref(false)
/** 每个文件的 dryRun 结果：[{ file, data } | { file, error }] */
const plans = ref([])
/** 每个文件的实跑结果：同上 */
const results = ref([])
const fileInput = ref(null)

const VCARD_EXTS = ['.vcf', '.vcard']

const busy = computed(() => previewing.value || importing.value)
/** 有通过预览的文件才允许导入 —— 没计划就导，等于闭着眼睛覆盖界面上的改动 */
const canImport = computed(() => !busy.value && plans.value.length > 0)
const hasErrors = computed(() => plans.value.some((one) => one.error))

function kindOf(name) {
  const lower = String(name || '').toLowerCase()
  return VCARD_EXTS.some((ext) => lower.endsWith(ext)) ? 'vcard' : 'csv'
}

function kindLabel(name) {
  return kindOf(name) === 'vcard' ? 'vCard' : 'CSV'
}

/** 按扩展名分派：CSV 走主力通道，其余一律当 vCard（前端只做分派，不做格式判断） */
function send(file, dryRun) {
  return kindOf(file.name) === 'vcard' ? importVcard(file, dryRun) : importCsv(file, dryRun)
}

/**
 * 人物库空状态会跳到 `/settings#contact-import`。
 * 本项目没配 router 的 `scrollBehavior`（见 `router/index.js`），所以由本组件
 * 自己滚 —— 等一帧再滚：挂载那一瞬间下面的区块还没定高，滚不准。
 */
onMounted(() => {
  if (window.location.hash !== `#${props.anchor}`) return
  requestAnimationFrame(() => {
    document.getElementById(props.anchor)?.scrollIntoView({ block: 'start' })
  })
})

function browse() {
  fileInput.value?.click()
}

function pick(event) {
  files.value = Array.from(event.target.files || [])
  // 换文件 = 计划作废（旧计划对应的是旧文件，留着最容易被当成新文件的计划）
  plans.value = []
  results.value = []
}

function reset() {
  files.value = []
  plans.value = []
  results.value = []
  if (fileInput.value) fileInput.value.value = ''
}

/** 逐个文件预览（dryRun=1：**一个字节都不写**） */
async function preview() {
  if (!files.value.length) return
  previewing.value = true
  plans.value = []
  results.value = []
  try {
    for (const file of files.value) {
      try {
        plans.value.push({ file, data: await send(file, true), error: '' })
      } catch (e) {
        plans.value.push({ file, data: null, error: e?.message || '预览失败' })
      }
    }
    if (hasErrors.value) {
      ElMessage.warning('有文件没能读出计划，详见下方红色条目')
    } else {
      ElMessage.success('计划已生成 —— 确认无误后再点「确认导入」')
    }
  } finally {
    previewing.value = false
  }
}

/** 逐个文件实跑（dryRun=0）。一个文件失败不影响其余文件 */
async function run() {
  if (!canImport.value) return
  importing.value = true
  results.value = []
  try {
    for (const file of files.value) {
      try {
        results.value.push({ file, data: await send(file, false), error: '' })
      } catch (e) {
        results.value.push({ file, data: null, error: e?.message || '导入失败' })
      }
    }
    const ok = results.value.filter((one) => one.data && !one.error)
    const created = ok.reduce((sum, one) => sum + (one.data.createCount || 0), 0)
    const updated = ok.reduce((sum, one) => sum + (one.data.updateCount || 0), 0)
    const failed = ok.reduce((sum, one) => sum + (one.data.failedCount || 0), 0)
    if (!ok.length) {
      ElMessage.error('一个文件都没导进去，详见下方列表')
    } else if (failed) {
      ElMessage.warning(`导入完成：新建 ${created}、更新 ${updated}，但有 ${failed} 条写库失败`)
    } else {
      ElMessage.success(`导入完成：新建 ${created} 人、更新 ${updated} 人`)
    }
    // 计划作废：避免「刚导完又点一次」（要再看计划就重新预览）
    plans.value = []
  } finally {
    importing.value = false
  }
}

function opLabel(op) {
  return { create: '新建', update: '更新', skip: '跳过' }[op] || String(op || '')
}

/** 命中方式：告诉用户「这条是靠什么认出是同一个人的」 */
function hitLabel(hit) {
  return { vcardUid: 'UID 命中', displayName: '同名命中' }[hit] || '新档案'
}

function warningText(one) {
  return String(one || '').replace(/\*\*/g, '')
}

const PREVIEW_ROWS = 10
const WARN_ROWS = 6
</script>

<template>
  <div :id="props.anchor" class="space-y-4">
    <p class="text-body text-ink-sub">
      把通讯录（Outlook 导出的 <b>CSV</b>、手机导出的 <b>vCard .vcf</b>）导进来<b>建人员档案</b>，
      作为后续人脸归类的依据。只建档案、<b>不关联照片</b>；
      识别与确认是后面两步的事。
    </p>

    <!-- ============ 选文件 ============ -->
    <div class="rounded-btn border border-dashed border-line bg-surface px-4 py-3">
      <div class="flex flex-wrap items-center gap-3">
        <input
          ref="fileInput"
          class="sr-only"
          type="file"
          multiple
          accept=".csv,.vcf,.vcard,text/csv,text/vcard"
          aria-label="选择通讯录文件（CSV 或 vCard）"
          @change="pick"
        />
        <el-button :disabled="busy" @click="browse">
          <FileUp class="mr-1 h-4 w-4" aria-hidden="true" />选择文件
        </el-button>
        <p class="pb-hint">
          支持多选：CSV 一份通常是一个文件；vCard 若是「一个联系人一个 .vcf」，请把要导的一起选中
        </p>
        <el-button v-if="files.length" text :disabled="busy" @click="reset">清空</el-button>
      </div>

      <ul v-if="files.length" class="mt-3 space-y-1">
        <li
          v-for="file in files"
          :key="file.name + file.size"
          class="flex flex-wrap items-center gap-2 text-caption text-ink-sub"
        >
          <span class="rounded-btn bg-card px-1.5 py-0.5 text-ink-weak">{{ kindLabel(file.name) }}</span>
          <span class="font-mono text-ink">{{ file.name }}</span>
          <span class="text-ink-weak">{{ formatFileSize(file.size) }}</span>
        </li>
      </ul>
      <p v-else class="pb-hint mt-2">还没选文件。</p>
    </div>

    <!-- ============ 动作 ============ -->
    <div class="flex flex-wrap items-center gap-3">
      <el-button :loading="previewing" :disabled="!files.length || importing" @click="preview">
        <ListChecks class="mr-1 h-4 w-4" aria-hidden="true" />预览导入计划
      </el-button>
      <el-button type="primary" :loading="importing" :disabled="!canImport" @click="run">
        确认导入{{ files.length > 1 ? `（${files.length} 个文件）` : '' }}
      </el-button>
      <span class="pb-hint">
        <TriangleAlert class="mr-1 inline h-3 w-3" aria-hidden="true" />
        预览是<b>只算不写</b>；导入会<b>覆盖</b>同名档案里被界面改过的字段（改不回来的那种）
      </span>
    </div>

    <!-- ============ 导入计划（dryRun） ============ -->
    <div v-if="plans.length" class="space-y-3">
      <h4 class="text-body text-ink">计划（还没写库）</h4>

      <div
        v-for="(one, index) in plans"
        :key="'plan-' + index"
        class="rounded-btn bg-surface px-3 py-2"
      >
        <p class="flex flex-wrap items-center gap-2 text-caption">
          <span class="font-mono text-ink">{{ one.file.name }}</span>
          <span class="text-ink-weak">{{ kindLabel(one.file.name) }}</span>
          <template v-if="one.data">
            <span v-if="one.data.encoding" class="text-ink-weak">编码 {{ one.data.encoding }}</span>
            <span v-if="one.data.parser" class="text-ink-weak">解析器 {{ one.data.parser }}</span>
          </template>
        </p>

        <p v-if="one.error" class="mt-1 rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink">
          {{ one.error }}
        </p>

        <template v-else>
          <p class="mt-1 flex flex-wrap gap-3 text-caption text-ink-sub">
            <span>新建 <b class="tabular-nums text-ink">{{ one.data.createCount }}</b></span>
            <span>更新 <b class="tabular-nums text-ink">{{ one.data.updateCount }}</b></span>
            <span>跳过 <b class="tabular-nums text-ink">{{ one.data.skipCount }}</b></span>
            <span v-if="one.data.kind === 'vcard'">
              家庭组 <b class="tabular-nums text-ink">{{ one.data.groupCount || 0 }}</b>
            </span>
            <span v-else>
              数据行 <b class="tabular-nums text-ink">{{ one.data.totalRows }}</b>
            </span>
          </p>

          <div v-if="(one.data.preview || []).length" class="mt-2 overflow-x-auto">
            <table class="w-full text-left text-caption">
              <thead class="text-ink-weak">
                <tr>
                  <th class="py-1 pr-3 font-normal">动作</th>
                  <th class="py-1 pr-3 font-normal">姓名</th>
                  <th class="py-1 pr-3 font-normal">凭据</th>
                  <th class="py-1 font-normal">分类</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in one.data.preview.slice(0, PREVIEW_ROWS)" :key="row.personCode">
                  <td class="py-0.5 pr-3">{{ opLabel(row.op) }}</td>
                  <td class="py-0.5 pr-3 text-ink">{{ row.displayName }}</td>
                  <td class="py-0.5 pr-3 text-ink-weak">{{ hitLabel(row.hit) }}</td>
                  <td class="py-0.5 text-ink-weak">{{ (row.categories || []).join('、') || '—' }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="one.data.preview.length > PREVIEW_ROWS" class="pb-hint mt-1">
              只列了前 {{ PREVIEW_ROWS }} 条，计划里共检出 {{ one.data.cardCount || one.data.totalRows }} 条
            </p>
          </div>

          <div v-if="(one.data.groups || []).length" class="mt-2 space-y-0.5">
            <p
              v-for="group in one.data.groups"
              :key="group.familyCode"
              class="text-caption text-ink-sub"
            >
              家庭组「{{ group.familyName }}」成员 {{ group.memberCount }} 人<template
                v-if="group.unresolvedCount"
              >
                ，其中 {{ group.unresolvedCount }} 人认不出（<b class="text-warning-ink">不会挂接</b>）</template
              >
            </p>
          </div>
        </template>
      </div>

      <div v-if="plans.some((one) => (one.data?.warnings || []).length)" class="space-y-2">
        <p class="pb-hint">
          <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
          警告（生日解析不出、重名被改名、成员认不出等都在这里）
        </p>
        <div v-for="one in plans" :key="'warn-' + one.file.name" class="space-y-0.5">
          <ul v-if="(one.data?.warnings || []).length" class="space-y-0.5">
            <li
              v-for="(text, index) in one.data.warnings.slice(0, WARN_ROWS)"
              :key="index"
              class="text-caption text-ink-sub"
            >
              · {{ warningText(text) }}
            </li>
          </ul>
          <p v-if="(one.data?.warnings || []).length > WARN_ROWS" class="pb-hint">
            还有 {{ one.data.warnings.length - WARN_ROWS }} 条类似的警告
          </p>
        </div>
      </div>
    </div>

    <!-- ============ 导入结果 ============ -->
    <div v-if="results.length" class="space-y-3">
      <h4 class="text-body text-ink">结果（已写库）</h4>

      <div
        v-for="(one, index) in results"
        :key="'result-' + index"
        class="rounded-btn bg-surface px-3 py-2"
      >
        <p class="flex flex-wrap items-center gap-2 text-caption">
          <CircleCheck v-if="one.data && !one.error" class="h-3.5 w-3.5 text-success-ink" aria-hidden="true" />
          <span class="font-mono text-ink">{{ one.file.name }}</span>
        </p>

        <p v-if="one.error" class="mt-1 rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink">
          {{ one.error }}
        </p>

        <template v-else>
          <p class="mt-1 flex flex-wrap gap-3 text-caption text-ink-sub">
            <span>新建 <b class="tabular-nums text-ink">{{ one.data.createCount }}</b></span>
            <span>更新 <b class="tabular-nums text-ink">{{ one.data.updateCount }}</b></span>
            <span>跳过 <b class="tabular-nums text-ink">{{ one.data.skipCount }}</b></span>
            <span v-if="one.data.failedCount">
              失败 <b class="tabular-nums text-danger-ink">{{ one.data.failedCount }}</b>
            </span>
            <span>分类行 <b class="tabular-nums text-ink">{{ one.data.categoryRows }}</b></span>
            <span v-if="one.data.kind === 'vcard'">
              家庭组 <b class="tabular-nums text-ink">{{ one.data.groupCount || 0 }}</b>
              （成员 <b class="tabular-nums text-ink">{{ one.data.familyMembers || 0 }}</b> 人）
            </span>
            <span v-if="one.data.kind === 'vcard'">
              通讯录头像 <b class="tabular-nums text-ink">{{ one.data.avatarCount || 0 }}</b> 张
            </span>
          </p>

          <ul v-if="(one.data.failed || []).length" class="mt-1 space-y-0.5">
            <li v-for="code in one.data.failed.slice(0, WARN_ROWS)" :key="code" class="text-caption text-danger-ink">
              · {{ code }}
            </li>
          </ul>
          <p v-if="(one.data.familyUnresolved || []).length" class="mt-1 text-caption text-warning-ink">
            <TriangleAlert class="mr-1 inline h-3 w-3" aria-hidden="true" />
            有 {{ one.data.familyUnresolved.length }} 个组成员在通讯录里找不到对应的人 ——
            未挂接，请人工核对
          </p>
          <p v-if="one.data.archive?.path" class="pb-hint mt-1 truncate font-mono" :title="one.data.archive.path">
            原件已归档：{{ one.data.archive.path }}
          </p>

          <ul v-if="(one.data.warnings || []).length" class="mt-1 space-y-0.5">
            <li
              v-for="(text, index) in one.data.warnings.slice(0, WARN_ROWS)"
              :key="index"
              class="text-caption text-ink-sub"
            >
              · {{ warningText(text) }}
            </li>
          </ul>
          <p v-if="(one.data.warnings || []).length > WARN_ROWS" class="pb-hint">
            还有 {{ one.data.warnings.length - WARN_ROWS }} 条类似的提示
          </p>
        </template>
      </div>

      <p class="pb-hint">
        档案已经建好了。下一步：去
        <RouterLink class="text-brand-ink underline" to="/people">人物库</RouterLink>
        核对名单，然后扫描照片、在待确认队列里把脸认到人。
      </p>
    </div>

    <!-- ============ 纪律（与其它区块同款口径） ============ -->
    <ul class="space-y-1 border-t border-line pt-3 text-caption text-ink-sub">
      <li>· 只建档案，<b>不关联照片</b>；原图目录始终只读，导入不碰 photo。</li>
      <li>· 重新导入会<b>覆盖</b>这里的编辑（生日、分类等）—— 界面上的修改先确认，再导新通讯录。</li>
      <li>· 导入原件会<b>复制</b>一份到 <code class="font-mono">db\imports\</code>（不改动你的原文件）。</li>
      <li>· 本工具<b>不提供导出</b>（CSV / vCard）；迁移与备份用同页的整库拷贝。</li>
    </ul>
  </div>
</template>
