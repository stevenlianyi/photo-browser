<!--
  P-08 设置（设计稿 §3.2）
  ------------------------------------------------------------
  照片根目录（只读展示）｜ 识别参数（T_high / T_low / 分年代档策略）｜ 数据维护 ｜ 外观 ｜ 关于

  三条必须写在界面上的纪律
  ----------------------
  ① **不提供任何导出入口**（DR-17）：只有「备份 / 恢复」（整库拷贝 db\ + thumb\）。
     而且备份/恢复**故意不给按钮**：备份的定义是「停服务 → 拷贝」，
     在服务自己开着的时候拷走的是 WAL 中间态 —— 恢复后少几百条记录且不报错。
     所以这里给的是只读清单 + 可照抄的命令行。
  ② 改识别阈值只是**引导**，不自动重算质心 —— 并且明确区分两件事：
     「改参数（瞬时）」与「重算质心（分钟级写库）」。
  ③ 参数改动是**进程内**的：不写回配置文件，重启服务即回到 basicSettings。
     这句话必须挂在参数区，不然用户重启后发现阈值变了回去会当成 bug。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Archive,
  CircleCheck,
  Database,
  HardDrive,
  Info,
  Palette,
  RefreshCw,
  TriangleAlert,
  Wrench,
} from 'lucide-vue-next'
import { ElMessage } from 'element-plus/es/components/message/index'
import 'element-plus/theme-chalk/el-message.css'
import { BUCKET_STRATEGIES, useSettingsStore } from '@/store/settings'
import { formatCount } from '@/utils/format'

const settings = useSettingsStore()

const busy = ref(false)

const match = computed(() => settings.effective)
const paths = computed(() => settings.paths)
const centroids = computed(() => settings.centroidOverview)

const presetOptions = computed(() =>
  Object.entries(match.value?.presets || {}).map(([value, one]) => ({
    value,
    label: `${value}（T_low ${Number(one.tLow).toFixed(2)} / T_high ${Number(one.tHigh).toFixed(2)}）`,
  })),
)

/** 点预设 = 把两个滑块拨到那一档（预设本身不单独提交，由「保存参数」统一发） */
function applyPreset(one) {
  if (!one) return
  settings.matchThresholdLow = Number(one.tLow)
  settings.matchThresholdHigh = Number(one.tHigh)
}

const backupItems = computed(() => settings.server?.backups?.items || [])
const backupCommands = computed(() => settings.server?.backups?.commands || {})
const backupNotes = computed(() => settings.server?.backups?.notes || [])

onMounted(async () => {
  try {
    await settings.fetchSettings()
  } catch (e) {
    /* store 已记录 serverError，页面顶部会显示 */
  }
})

onBeforeUnmount(() => settings.stopRebuildPolling())

async function save() {
  busy.value = true
  try {
    const data = await settings.saveMatch()
    if (data?.needCentroidRebuild) {
      ElMessage.warning('参数已保存。识别结果要按新参数走，还需要重新生成质心。')
    } else if (data?.changed?.length) {
      ElMessage.success('参数已保存（不影响已有质心）')
    } else {
      ElMessage.info('没有改动')
    }
  } finally {
    busy.value = false
  }
}

async function rebuild() {
  busy.value = true
  try {
    await settings.startRebuild()
    ElMessage.info('已开始重新生成质心（先刷新年代档，再重算质心）')
    settings.setRebuildPolling(true)
  } finally {
    busy.value = false
  }
}

/** 复制命令：备份这一节的价值全在「可照抄的命令」上 */
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch (e) {
    ElMessage.warning('浏览器不允许自动复制，请手动选中复制')
  }
}
</script>

<template>
  <div class="pb-page space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-title text-ink">设置</h2>
        <p class="pb-hint mt-1">识别参数、数据维护与外观偏好。改参数不会自动生效，需要重算质心。</p>
      </div>
      <el-button :loading="settings.serverLoading" @click="settings.fetchSettings()">
        <RefreshCw class="mr-1 h-4 w-4" aria-hidden="true" />重新读取
      </el-button>
    </div>

    <p
      v-if="settings.serverError"
      class="flex items-start gap-2 rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink"
      role="alert"
    >
      <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{{ settings.serverError }}</span>
    </p>

    <!-- ============ 照片库路径（只读） ============ -->
    <section class="pb-card p-4" aria-labelledby="st-paths-title">
      <h3 id="st-paths-title" class="flex items-center gap-2 pb-section-title">
        <HardDrive class="h-4 w-4 text-ink-weak" aria-hidden="true" />照片库路径（只读）
      </h3>
      <dl class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="item in [
            { label: '原图目录（只读）', value: paths?.photo },
            { label: '缩略图目录（生成物）', value: paths?.thumb },
            { label: '数据库', value: paths?.database },
            { label: '备份根目录', value: paths?.backupRoot },
          ]" :key="item.label">
          <dt class="pb-hint">{{ item.label }}</dt>
          <dd class="truncate font-mono text-body text-ink" :title="item.value">
            {{ item.value || '—' }}
          </dd>
        </div>
      </dl>
      <p class="pb-hint mt-3">
        目录由后端配置解析，界面上不提供修改入口 ——
        <b>原图始终只读，本工具不提供编辑或覆盖原图的能力</b>。
      </p>
    </section>

    <!-- ============ 识别参数 ============ -->
    <section class="pb-card p-4" aria-labelledby="st-match-title">
      <h3 id="st-match-title" class="pb-section-title">识别参数</h3>

      <!-- 当前生效值：改之前先告诉用户「现在是多少」 -->
      <p v-if="match" class="pb-hint mt-2">
        当前生效：相似度 ≥ <b class="tabular-nums">{{ Number(match.tHigh).toFixed(2) }}</b>
        自动归属；<b class="tabular-nums">{{ Number(match.tLow).toFixed(2) }}</b> 以下进聚类；
        分年代档策略 <b>{{ match.bucketStrategy }}</b>；
        质心{{ match.centroidConfirmedOnly ? '只' : '不只' }}用人工确认样本。
      </p>

      <div class="mt-4 grid grid-cols-1 gap-6 md:grid-cols-2">
        <div>
          <label class="pb-hint block" for="st-high">
            自动归属阈值 T_high（相似度 ≥ 此值直接归属，不进待确认）
          </label>
          <el-slider
            id="st-high"
            v-model="settings.matchThresholdHigh"
            class="mt-2"
            :min="0.3"
            :max="0.9"
            :step="0.01"
            :format-tooltip="(value) => Number(value).toFixed(2)"
          />
        </div>
        <div>
          <label class="pb-hint block" for="st-low">
            置为未知阈值 T_low（相似度 &lt; 此值直接进聚类，不进待确认队列）
          </label>
          <el-slider
            id="st-low"
            v-model="settings.matchThresholdLow"
            class="mt-2"
            :min="0.1"
            :max="0.5"
            :step="0.01"
            :format-tooltip="(value) => Number(value).toFixed(2)"
          />
        </div>

        <div>
          <label class="pb-hint block" for="st-preset">阈值预设（按 S0 实测背书的三档）</label>
          <el-select
            id="st-preset"
            class="mt-1 w-full"
            :model-value="match?.preset || undefined"
            placeholder="自定义"
            @change="(value) => applyPreset(match?.presets?.[value])"
          >
            <el-option
              v-for="item in presetOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </div>

        <div>
          <label class="pb-hint block" for="st-bucket">分年代档策略</label>
          <el-select id="st-bucket" v-model="settings.bucketStrategy" class="mt-1 w-full">
            <el-option
              v-for="item in BUCKET_STRATEGIES"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </div>

        <div class="md:col-span-2">
          <el-checkbox v-model="settings.centroidConfirmedOnly">
            质心只用<b>人工确认</b>的样本（强烈建议保持勾选）
          </el-checkbox>
          <p class="pb-hint mt-1">
            质心若把自动归属的样本也算进去，一张误认的脸就会把质心拉偏 →
            更多脸被误认 → <b>越错越错</b>，而且没有任何迹象提示它坏了。
            关掉它只会让「我不同意」列表变空，不会让认得更准。
          </p>
        </div>
      </div>

      <!-- 需重算质心提示 -->
      <p
        class="mt-4 flex items-start gap-2 rounded-btn px-3 py-2 text-caption"
        :class="settings.needCentroidRebuild ? 'bg-warning-soft text-warning-ink' : 'bg-surface text-ink-sub'"
      >
        <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>
          <template v-if="settings.needCentroidRebuild">
            参数已改动。<b>改阈值与年代档划分都需要重新生成质心</b>，否则新旧口径不一致会出现静默失配 ——
            质心表里留着旧年代档键的行、新年代档键又没有质心，那个人的匹配率会归零且库里看不出异常。
            <template v-if="settings.afterSave?.steps?.length">
              建议顺序：{{ settings.afterSave.steps.join(' → ') }}。
            </template>
          </template>
          <template v-else>
            阈值与分年代档策略只影响后续判定，不会自动重算质心；改动后请到下面的「数据维护」重算一次。
          </template>
        </span>
      </p>

      <div class="mt-4 flex flex-wrap gap-2">
        <el-button type="primary" :loading="busy" :disabled="!settings.draftDirty" @click="save">
          保存参数
        </el-button>
        <el-button :disabled="!settings.draftDirty" @click="settings.discardDraft()">
          放弃改动
        </el-button>
        <el-button @click="settings.resetMatchParams()">恢复默认值</el-button>
      </div>

      <p class="pb-hint mt-3">
        <TriangleAlert class="mr-1 inline h-3 w-3" aria-hidden="true" />
        参数改动<b>只在本进程内生效</b>：不写回配置文件，重启服务后回到
        <code class="font-mono">config/basicSettings.py</code> 的值。
        要固化请改那个文件（那是代码，不是设置）。
      </p>
    </section>

    <!-- ============ 数据维护 ============ -->
    <section class="pb-card p-4" aria-labelledby="st-data-title">
      <h3 id="st-data-title" class="flex items-center gap-2 pb-section-title">
        <Database class="h-4 w-4 text-ink-weak" aria-hidden="true" />数据维护
      </h3>

      <dl v-if="centroids" class="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div>
          <dt class="pb-hint">人脸总数</dt>
          <dd class="text-body tabular-nums text-ink">{{ formatCount(centroids.faceCount) }}</dd>
        </div>
        <div>
          <dt class="pb-hint">质心行数</dt>
          <dd class="text-body tabular-nums text-ink">{{ formatCount(centroids.centroidRows) }}</dd>
        </div>
        <div>
          <dt class="pb-hint">已启用年代档（样本 ≥ {{ match?.minCentroidSamples ?? 3 }}）</dt>
          <dd class="text-body tabular-nums text-ink">{{ formatCount(centroids.enabledBuckets) }}</dd>
        </div>
        <div>
          <dt class="pb-hint">脸表出现的年代档键</dt>
          <dd class="text-body tabular-nums text-ink">{{ formatCount(centroids.faceBucketKeys) }}</dd>
        </div>
      </dl>

      <p
        v-if="centroids?.bucketsWithoutCentroidTotal"
        class="mt-3 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
      >
        <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
        有 <b>{{ centroids.bucketsWithoutCentroidTotal }}</b> 个年代档里有人脸、却没有质心
        （{{ (centroids.bucketsWithoutCentroid || []).join('、') }}）——
        这些年代档取不到任何质心，属于 DR-22 说的静默失配。跑一次「重新生成质心」即可。
      </p>

      <div class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
        <p class="max-w-xl text-body text-ink-sub">
          <b>重新生成质心</b>：默认先<b>重刷年代档</b>（按当前分年代档策略与生日重算
          <code class="font-mono">pb_face.shotBucket</code>），<b>再</b>重建质心。
          顺序反了会留下「质心是旧年代档键、脸是新年代档键」的静默失配（DR-22），
          所以这里不给「跳过刷新年代档」的开关。
        </p>
        <el-button type="primary" :loading="busy || settings.rebuild.running" @click="rebuild">
          <Wrench class="mr-1 h-4 w-4" aria-hidden="true" />重新生成质心
        </el-button>
      </div>

      <!-- 重算进度：真实计数 -->
      <div
        v-if="settings.rebuild.total"
        class="mt-3 rounded-btn bg-surface px-3 py-2 text-caption text-ink-sub"
      >
        <div class="flex flex-wrap items-center gap-3">
          <span class="tabular-nums">{{ settings.rebuild.done }} / {{ settings.rebuild.total }} 人</span>
          <span
            class="h-1.5 w-40 overflow-hidden rounded-btn bg-card"
            role="progressbar"
            :aria-valuenow="settings.rebuild.done"
            :aria-valuemax="settings.rebuild.total"
            aria-label="质心重算进度"
          >
            <span
              class="block h-full bg-brand"
              :style="{ width: `${settings.rebuild.percent || 0}%` }"
            />
          </span>
          <span class="tabular-nums text-success-ink">成功 {{ settings.rebuild.ok }}</span>
          <span class="tabular-nums text-danger-ink">失败 {{ settings.rebuild.failed }}</span>
          <span v-if="!settings.rebuild.running" class="inline-flex items-center gap-1">
            <CircleCheck class="h-3.5 w-3.5" aria-hidden="true" />已结束
          </span>
        </div>
        <ul v-if="(settings.rebuild.log || []).length" class="mt-2 space-y-1">
          <li v-for="(line, index) in settings.rebuild.log" :key="index" class="text-ink-weak">
            {{ line }}
          </li>
        </ul>
      </div>

      <!-- 备份 / 恢复 -->
      <div class="mt-4 space-y-3 border-t border-line pt-4">
        <h4 class="flex items-center gap-2 text-body text-ink">
          <Archive class="h-4 w-4 text-ink-weak" aria-hidden="true" />备份 / 恢复
        </h4>
        <p class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink">
          <TriangleAlert class="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
          这里<b>故意不给按钮</b>：备份的定义是「<b>停服务</b> → 拷贝
          <code class="font-mono">db\</code> 与 <code class="font-mono">thumb\</code>」。
          服务还开着的时候拷走的是 SQLite 的 <b>WAL 中间态</b> ——
          最近几分钟的确认与重算还躺在 <code class="font-mono">-wal</code> 里，
          结果是<b>备份成功、零报错、恢复后少几百条记录</b>。
        </p>

        <ul class="space-y-1 text-caption text-ink-sub">
          <li v-for="(note, index) in backupNotes" :key="index">· {{ note }}</li>
        </ul>

        <div class="space-y-2">
          <div
            v-for="(cmd, key) in backupCommands"
            :key="key"
            class="flex flex-wrap items-center gap-2"
          >
            <code class="rounded-btn bg-surface px-2 py-1 font-mono text-caption text-ink">{{ cmd }}</code>
            <el-button size="small" text @click="copy(cmd)">复制</el-button>
          </div>
        </div>

        <div>
          <p class="pb-hint">已有备份（{{ backupItems.length }} 份）：</p>
          <p v-if="!backupItems.length" class="pb-hint mt-1">
            还没有备份。停掉服务后跑上面第一条命令即可。
          </p>
          <ul v-else class="mt-2 space-y-1">
            <li v-for="one in backupItems" :key="one.backupCode" class="text-caption text-ink-sub">
              <code class="font-mono">{{ one.backupCode }}</code>
              <span class="ml-2 text-ink-weak">
                db {{ one.db?.sizeText || '?' }} · thumb {{ one.thumb?.sizeText || '?' }}
                <template v-if="one.createdYMDHMS"> · {{ one.createdYMDHMS }}</template>
                <template v-if="one.label"> · {{ one.label }}</template>
              </span>
              <span v-if="!one.hasDb || !one.hasThumb" class="ml-2 text-danger-ink">
                （不完整，恢复会被拒绝）
              </span>
            </li>
          </ul>
        </div>
      </div>

      <p class="pb-hint mt-4">
        本工具<b>不提供导出入口</b>（CSV / vCard 导出）：家庭规模逐条编辑够用，
        导出侧零代码而导入侧要做幂等与去残留，两头不对称反而更容易出错。
      </p>
    </section>

    <!-- ============ 外观 ============ -->
    <section class="pb-card p-4" aria-labelledby="st-look-title">
      <h3 id="st-look-title" class="flex items-center gap-2 pb-section-title">
        <Palette class="h-4 w-4 text-ink-weak" aria-hidden="true" />外观
      </h3>
      <div class="mt-3 space-y-3">
        <p class="pb-hint">主题在顶栏右上角切换：浅色 / 深色 / 跟随系统，选择会记住。</p>
        <div class="flex flex-wrap items-center gap-6">
          <div>
            <span class="pb-hint block">缩略图密度</span>
            <el-radio-group
              :model-value="settings.density"
              class="mt-1"
              @update:model-value="settings.setDensity"
            >
              <el-radio-button value="comfortable">舒适</el-radio-button>
              <el-radio-button value="compact">紧凑</el-radio-button>
            </el-radio-group>
          </div>
          <div>
            <span class="pb-hint block">照片流默认视图</span>
            <el-radio-group
              :model-value="settings.photoViewMode"
              class="mt-1"
              @update:model-value="settings.setViewMode"
            >
              <el-radio-button value="grid">网格</el-radio-button>
              <el-radio-button value="timeline">时间轴</el-radio-button>
            </el-radio-group>
          </div>
        </div>
      </div>
    </section>

    <!-- ============ 关于 ============ -->
    <section class="pb-card p-4" aria-labelledby="st-about-title">
      <h3 id="st-about-title" class="flex items-center gap-2 pb-section-title">
        <Info class="h-4 w-4 text-ink-weak" aria-hidden="true" />关于
      </h3>
      <p class="mt-3 text-body text-ink-sub">
        photo-browser · 本地个人 / 家庭照片浏览与人脸归类工具。照片不上传、不出本机，
        单用户使用，没有登录与多用户概念。
      </p>
      <p class="pb-hint mt-2">
        界面：Vue 3 + Vite + Tailwind CSS + Element Plus ｜ 后端：FastAPI + SQLite（WAL）
      </p>
      <dl v-if="match" class="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div>
          <dt class="pb-hint">年代档划分（童年 / 成年）</dt>
          <dd class="text-body tabular-nums text-ink">
            ≤{{ match.childMaxAge }} 岁每 {{ match.childWidth }} 年 /
            &gt;{{ match.childMaxAge }} 岁每 {{ match.adultWidth }} 年
          </dd>
        </div>
        <div>
          <dt class="pb-hint">无生日降级年代档跨度</dt>
          <dd class="text-body tabular-nums text-ink">{{ match.equalWidth }} 年</dd>
        </div>
        <div>
          <dt class="pb-hint">默认批大小</dt>
          <dd class="text-body tabular-nums text-ink">{{ match.batchSize }} 张</dd>
        </div>
        <div>
          <dt class="pb-hint">服务端口</dt>
          <dd class="text-body tabular-nums text-ink">127.0.0.1:{{ match.serverPort }}</dd>
        </div>
      </dl>
      <p class="pb-hint mt-3">
        原图目录<strong>始终只读</strong>；本工具不提供编辑、覆盖、删除原图的任何入口。
        缩略图与人脸裁剪图是生成物，可随时重建。
      </p>
    </section>
  </div>
</template>