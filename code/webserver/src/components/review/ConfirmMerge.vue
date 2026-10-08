<!--
  ConfirmMerge —— 合并两个人物档案的二次确认（设计稿 §五 + §6.2 流程三）
  ------------------------------------------------------------
  合并是**不可逆**的（虽然有撤销入口，但质心一旦被污染靠重算回不到原值，
  DR-16③）。所以这个弹窗的全部职责就是**复述参数**：

    把「张三」（照片 412 张 / 人脸 486 张 / 人工确认 412）
    合并进「李四」（照片 38 张 / 人脸 40 张 / 人工确认 40）

  为什么必须复述**双方姓名 + 照片数**
  --------------------------------
  「合并」这个词本身有歧义：合并后保的是哪一个？用户如果以为是"两个都保留"，
  点下去就发现张三这个档案不见了 —— 而那正是他最常出现的名字。
  复述照片数是第二道保险：412 vs 38 这个对比会让"保谁"变得没有疑问。

  动词按钮「确认合并」（设计稿硬要求）：主按钮写清楚**将要发生什么**，
  不是「确定」/「OK」。
-->
<script setup>
import { computed } from 'vue'
import { TriangleAlert } from 'lucide-vue-next'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 被合并方（合并后这个档案不再单独存在） */
  from: { type: Object, default: null },
  /** 保留方 */
  to: { type: Object, default: null },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const fromName = computed(
  () => props.from?.displayName || props.from?.personCode || '（未命名）',
)
const toName = computed(() => props.to?.displayName || props.to?.personCode || '（未命名）')

const mergedFaces = computed(
  () => (Number(props.from?.faceCount) || 0) + (Number(props.to?.faceCount) || 0),
)

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : '0'
}

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="确认合并两个人物档案？"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <!-- 参数复述：双方姓名 + 照片数（P0-3 参数复述） -->
    <div class="rounded-btn border border-line bg-card p-3">
      <p class="text-body text-ink">
        把 <b class="text-danger-ink">「{{ fromName }}」</b> 的全部人脸，归到
        <b class="text-success-ink">「{{ toName }}」</b> 名下。
      </p>
      <dl class="mt-3 grid grid-cols-[1fr_auto_1fr] items-center gap-2 text-center">
        <div class="rounded-btn bg-danger-soft p-2">
          <dt class="text-caption text-ink-weak">被合并方</dt>
          <dd class="mt-1 truncate text-body font-medium text-ink">{{ fromName }}</dd>
          <dd class="mt-1 text-caption tabular-nums text-ink-sub">
            照片 {{ num(from?.photoCount) }} 张<br />
            人脸 {{ num(from?.faceCount) }} 张<br />
            已确认 {{ num(from?.confirmedFaceCount) }} 张
          </dd>
        </div>
        <span class="text-caption text-ink-weak" aria-hidden="true">→</span>
        <div class="rounded-btn bg-success-soft p-2">
          <dt class="text-caption text-ink-weak">保留方</dt>
          <dd class="mt-1 truncate text-body font-medium text-ink">{{ toName }}</dd>
          <dd class="mt-1 text-caption tabular-nums text-ink-sub">
            照片 {{ num(to?.photoCount) }} 张<br />
            人脸 {{ num(to?.faceCount) }} 张<br />
            已确认 {{ num(to?.confirmedFaceCount) }} 张
          </dd>
        </div>
      </dl>
      <p class="mt-2 text-caption text-ink-sub">
        合并后共
        <b class="tabular-nums text-ink">{{ num(mergedFaces) }}</b> 张人脸，
        年代档质心会按新归属<b>全部重算</b>。
      </p>
    </div>

    <p
      class="mt-3 flex items-start gap-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
    >
      <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        合并后「{{ fromName }}」这个档案将不再单独存在（它的照片数并入「{{ toName }}」）。
        合并<b>可以撤销</b>（人物详情页有「撤销上次合并」），但撤销只能回到合并前的归属，
        <b>质心的原值回不来</b>。
      </span>
    </p>

    <template #footer>
      <div class="flex justify-end gap-2">
        <!-- 取消必须无任何副作用：不发请求、不改任何东西，只关自己 -->
        <el-button :disabled="loading" @click="close">取消</el-button>
        <el-button type="danger" :loading="loading" @click="emit('confirm')">
          确认合并
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>
