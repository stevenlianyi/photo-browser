<!--
  P-07 扫描任务（设计稿 §4.7）
  ------------------------------------------------------------
  新建扫描（根目录只读 + 批大小，默认 100）＋ 任务列表（状态 / 本批 / 累计 / 操作）
  两条语义必须显式表达在界面上：
   · **批次限流**：每处理 batchSize 张就 PAUSED，等人工点「继续下一批」——
     所以要能看到「本批 62 / 100」和「已暂停等待指示」两个信息，不能只给一个总进度
   · **进度诚实**：用真实计数，不做假进度条
-->
<script setup>
import { computed, ref } from 'vue'
import { CircleCheck, CircleX, HardDrive, Pause, Play, Plus, TriangleAlert } from 'lucide-vue-next'
import { JOB_STATUS_META, useScanStore } from '@/store/scan'

const scan = useScanStore()
const createVisible = ref(false)
const batchSizeInput = ref(scan.batchSize)

/** 根目录只读：改目录等于换库，只应由后端配置决定 */
const ROOT_PATH = 'd:\\PhotoLib\\photo'

/** 结构示例：步骤 11 接 /api/scan/jobs 后由真实数据替换 */
const JOBS = [
  { jobCode: 'SJ-0007', status: 'RUNNING', batchProcessed: 62, batchSize: 100, total: 1240 },
  { jobCode: 'SJ-0006', status: 'PAUSED', batchProcessed: 100, batchSize: 100, total: 3800 },
  { jobCode: 'SJ-0005', status: 'DONE', batchProcessed: null, batchSize: 100, total: 9204 },
  { jobCode: 'SJ-0004', status: 'FAILED', batchProcessed: null, batchSize: 100, total: 120 },
]

/** 状态 → 色/图标/文字三重编码（不单靠颜色） */
const STATUS_TONE = {
  neutral: 'text-ink-sub',
  info: 'text-info-ink',
  warning: 'text-warning-ink',
  success: 'text-success-ink',
  danger: 'text-danger-ink',
}
const STATUS_ICON = {
  IDLE: Play,
  RUNNING: Play,
  PAUSED: Pause,
  DONE: CircleCheck,
  FAILED: CircleX,
}
const STATUS_SOFT = {
  neutral: 'bg-surface',
  info: 'bg-info-soft',
  warning: 'bg-warning-soft',
  success: 'bg-success-soft',
  danger: 'bg-danger-soft',
}

function openCreate() {
  batchSizeInput.value = scan.batchSize
  createVisible.value = true
}

function confirmCreate() {
  scan.setBatchSize(batchSizeInput.value)
  createVisible.value = false
}

const canResume = computed(() => JOBS.some((job) => job.status === 'PAUSED'))
</script>

<template>
  <div class="pb-page space-y-4">
    <!-- 页头 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">扫描任务</h2>
        <p class="pb-hint mt-1">
          扫描按批次限流：每处理 {{ scan.batchSize }} 张就停下来等你确认，不会一口气跑完整个照片库。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">
        <Plus class="mr-1 h-4 w-4" aria-hidden="true" />新建扫描
      </el-button>
    </div>

    <!-- 根目录（只读） -->
    <section class="pb-card flex flex-wrap items-center gap-3 p-4">
      <HardDrive class="h-4 w-4 shrink-0 text-ink-weak" aria-hidden="true" />
      <div class="min-w-0 flex-1">
        <p class="pb-hint">照片根目录（只读）</p>
        <p class="truncate font-mono text-body text-ink">{{ ROOT_PATH }}</p>
      </div>
      <p class="pb-hint">
        目录由后端配置决定，界面上不提供修改入口 —— 原图只读，改目录等于换库。
      </p>
    </section>

    <!-- 任务列表 -->
    <section class="pb-card p-4">
      <h3 class="pb-section-title">任务列表</h3>
      <el-table :data="JOBS" class="mt-3" style="width: 100%">
        <el-table-column prop="jobCode" label="任务" width="120" />
        <el-table-column label="状态" width="140">
          <template #default="{ row }">
            <span
              class="inline-flex items-center gap-1.5 rounded-btn px-2 py-0.5 text-caption"
              :class="[STATUS_SOFT[JOB_STATUS_META[row.status].tone], STATUS_TONE[JOB_STATUS_META[row.status].tone]]"
            >
              <component :is="STATUS_ICON[row.status]" class="h-3 w-3" aria-hidden="true" />
              {{ JOB_STATUS_META[row.status].label }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="本批" width="160">
          <template #default="{ row }">
            <span class="tabular-nums">
              {{ row.batchProcessed === null ? '—' : `${row.batchProcessed} / ${row.batchSize}` }}
            </span>
            <div
              v-if="row.batchProcessed !== null"
              class="mt-1 h-1.5 w-full overflow-hidden rounded-btn bg-surface"
              role="progressbar"
              :aria-valuenow="row.batchProcessed"
              :aria-valuemax="row.batchSize"
              aria-label="本批进度"
            >
              <div
                class="h-full"
                :class="row.status === 'RUNNING' ? 'bg-info' : 'bg-[var(--pb-warning-line)]'"
                :style="{ width: `${Math.round((row.batchProcessed / row.batchSize) * 100)}%` }"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="累计" width="120">
          <template #default="{ row }">
            <span class="tabular-nums">{{ row.total.toLocaleString('zh-CN') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="220">
          <template #default="{ row }">
            <el-button v-if="row.status === 'RUNNING'" size="small">
              <Pause class="mr-1 h-3.5 w-3.5" aria-hidden="true" />暂停
            </el-button>
            <el-button v-else-if="row.status === 'PAUSED'" size="small" type="primary">
              <Play class="mr-1 h-3.5 w-3.5" aria-hidden="true" />继续下一批
            </el-button>
            <el-button v-else-if="row.status === 'DONE'" size="small">查看</el-button>
            <el-button v-else size="small" type="danger" plain>
              <TriangleAlert class="mr-1 h-3.5 w-3.5" aria-hidden="true" />查看错误
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <p class="pb-hint mt-3">
        <template v-if="canResume">
          有任务处于「已暂停」，点「继续下一批」从上次游标接着扫 —— 不会从头再来。
        </template>
        <template v-else>所有任务都已跑完。进度显示的是真实计数，不做假进度条。</template>
      </p>
    </section>

    <p class="pb-hint">本页为结构示例，接入 /api/scan/jobs 后由真实数据替换。</p>

    <!-- 新建扫描对话框 -->
    <el-dialog v-model="createVisible" title="新建扫描任务" width="460px">
      <div class="space-y-4">
        <div>
          <p class="pb-hint">照片根目录</p>
          <p class="mt-1 rounded-btn border border-line bg-surface px-3 py-2 font-mono text-body text-ink">
            {{ ROOT_PATH }}
          </p>
          <p class="pb-hint mt-1">
            只能扫描照片根目录本身：库外目录与子目录都会被后端拒绝（会导致重复入库或写脏 relPath）。
          </p>
        </div>

        <div>
          <label class="pb-hint block" for="sj-batch">每批张数</label>
          <el-input-number
            id="sj-batch"
            v-model="batchSizeInput"
            :min="1"
            :max="5000"
            :step="50"
            class="w-40"
          />
          <p class="pb-hint mt-1">
            默认 100。到这个数就自动暂停，等你点「继续下一批」—— 10 万张的库一口气跑完不合适。
          </p>
        </div>
      </div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmCreate">开始扫描</el-button>
      </template>
    </el-dialog>
  </div>
</template>