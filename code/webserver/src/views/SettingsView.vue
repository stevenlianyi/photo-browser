<!--
  P-08 设置（设计稿 §3.2）
  ------------------------------------------------------------
  照片根目录（只读展示）｜ 识别参数（T_high / T_low / 分桶策略）｜ 数据维护 ｜ 外观 ｜ 关于
  两条纪律：
   · **不提供任何导出入口**（DR-17）：只有「备份 / 恢复」（整库拷贝 db\ + thumb\）
   · 改识别阈值只是引导，**不自动重算质心** —— 界面要提示「需重新生成质心」
-->
<script setup>
import { ref } from 'vue'
import { Archive, Database, HardDrive, Info, Palette, TriangleAlert } from 'lucide-vue-next'
import { useSettingsStore } from '@/store/settings'

const settings = useSettingsStore()

/** 只读展示：改目录等于换库，只应由后端配置决定 */
const PATHS = {
  photoRoot: 'd:\\PhotoLib\\photo',
  thumbRoot: 'd:\\PhotoLib\\thumb',
  dbFile: 'd:\\PhotoLib\\db\\photolib.db',
}

const BUCKET_STRATEGIES = [
  { value: 'adaptive', label: '自适应（按出生年定桶宽：0–18 岁 3 年 / 18+ 10 年）' },
  { value: 'fixed5', label: '等宽 5 年（无生日时的降级方案）' },
  { value: 'none', label: '不分桶（最慢，只用于对比排查）' },
]

const backupBusy = ref(false)
</script>

<template>
  <div class="pb-page space-y-4">
    <div>
      <h2 class="text-title text-ink">设置</h2>
      <p class="pb-hint mt-1">识别参数、数据维护与外观偏好。改参数不会自动生效，需要重算质心。</p>
    </div>

    <!-- 照片根目录（只读） -->
    <section class="pb-card p-4" aria-labelledby="st-paths-title">
      <h3 id="st-paths-title" class="flex items-center gap-2 pb-section-title">
        <HardDrive class="h-4 w-4 text-ink-weak" aria-hidden="true" />照片库路径（只读）
      </h3>
      <dl class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div v-for="item in [
            { label: '原图目录', value: PATHS.photoRoot },
            { label: '缩略图目录', value: PATHS.thumbRoot },
            { label: '数据库', value: PATHS.dbFile },
          ]" :key="item.label">
          <dt class="pb-hint">{{ item.label }}</dt>
          <dd class="truncate font-mono text-body text-ink">{{ item.value }}</dd>
        </div>
      </dl>
      <p class="pb-hint mt-3">
        三个目录由后端配置解析，界面上不提供修改入口 —— 原图始终只读，本工具不提供编辑或覆盖原图的能力。
      </p>
    </section>

    <!-- 识别参数 -->
    <section class="pb-card p-4" aria-labelledby="st-match-title">
      <h3 id="st-match-title" class="pb-section-title">识别参数</h3>
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
        <div class="md:col-span-2">
          <label class="pb-hint block" for="st-bucket">分桶策略</label>
          <el-select id="st-bucket" v-model="settings.bucketStrategy" class="mt-1 w-full max-w-md">
            <el-option
              v-for="item in BUCKET_STRATEGIES"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </div>
      </div>

      <p
        class="mt-4 flex items-start gap-2 rounded-btn px-3 py-2 text-caption"
        :class="settings.centroidDirty ? 'bg-warning-soft text-warning-ink' : 'bg-surface text-ink-sub'"
      >
        <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>
          <template v-if="settings.centroidDirty">
            参数已改动。<b>改阈值与分桶都需要重新生成质心</b>，否则新旧口径不一致会出现静默失配 ——
            质心表里留着旧桶键的行、新桶键又没有质心，那个人的匹配率会归零且库里看不出异常。
          </template>
          <template v-else>
            阈值与分桶策略只影响判定，不会自动重算质心；改动后请到下面的「数据维护」重算一次。
          </template>
        </span>
      </p>

      <div class="mt-4 flex flex-wrap gap-2">
        <el-button type="primary">保存并重新生成质心</el-button>
        <el-button @click="settings.resetMatchParams()">恢复默认值</el-button>
      </div>
    </section>

    <!-- 数据维护 -->
    <section class="pb-card p-4" aria-labelledby="st-data-title">
      <h3 id="st-data-title" class="flex items-center gap-2 pb-section-title">
        <Database class="h-4 w-4 text-ink-weak" aria-hidden="true" />数据维护
      </h3>
      <div class="mt-3 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <p class="max-w-xl text-body text-ink-sub">
            重新生成质心：只使用<b>人工确认</b>的样本，避免一张误认样本把质心拉偏、越错越错。
          </p>
          <el-button>重新生成质心</el-button>
        </div>
        <div class="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
          <p class="max-w-xl text-body text-ink-sub">
            备份：停服务后整库拷贝 <code class="font-mono">db\</code> 与
            <code class="font-mono">thumb\</code>。这是本工具唯一的迁移与备份途径。
          </p>
          <el-button :loading="backupBusy" @click="backupBusy = false">
            <Archive class="mr-1 h-4 w-4" aria-hidden="true" />备份 / 恢复
          </el-button>
        </div>
      </div>
      <p class="pb-hint mt-4">
        本工具<b>不提供导出入口</b>（CSV / vCard 导出）：家庭规模逐条编辑够用，
        导出侧零代码而导入侧要做幂等与去残留，两头不对称反而更容易出错。
      </p>
    </section>

    <!-- 外观 -->
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

    <!-- 关于 -->
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
    </section>

    <p class="pb-hint">
      参数改动与重算质心在步骤 12 接上后端接口；本页当前只出结构与文案。
    </p>
  </div>
</template>