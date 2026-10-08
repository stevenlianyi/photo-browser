<!--
  PhotoPager —— 照片左右翻页（DR-31，A 档：纯前端，后端零改动）
  ------------------------------------------------------------
  单一职责：**只画两个箭头 + 位置提示**，不取数据、不翻页、不认识路由。
  数据与动作全在调用方（P-03 的 `PhotoDetailView`），
  将来 `PersonDetailView` 的时间轴详情可以原样复用。

  四条边界纪律落在 prop 的语义里（DR-31）
  ----------------------------------------
  ① `index = 0`（这张照片不在已加载列表里）⇒ 两个箭头都禁用，说明走 `loadedHint`
  ② **不循环**：到头就是禁用，不做「到底跳回第一张」——
     循环会让人失去「我在哪」的位置感，而位置感正是这个控件存在的理由
  ③ 分母 `total` 传的是**已加载条数 `items.length`**，不是后端 `total`：
     只加载 60 张而 total=2137 时写「12 / 2137」，会让人以为后面还能翻 2125 张
  ④ `disabled` 由调用方给（改判请求进行中）—— 见下面「与改判的顺序」

  为什么箭头 hover 才显形，而禁用态是「淡」而不是「消失」
  ------------------------------------------------------
  这个页面的主任务是「看照片」。两个常驻的大圆按钮会压在画面两侧，
  所以显隐放在**外层的 `.pager-slot`**（`opacity-0` → hover / focus-within 时 1）；
  按钮自己只管「可不可用」（`disabled:opacity-40`）。
  ⚠️ 这两层**不能合并**（实测踩过）：写在同一个元素上时
     `opacity-0` + `group-hover/photo:opacity-100` + `disabled:opacity-40`
     里后两条特异度相同 (0,2,0)，Tailwind 把 `group-hover` 排在后面 ——
     结果是「禁用的箭头不 hover 时半可见、hover 后与可用的完全一样亮」，两头都错。
     而叠成 `disabled:group-hover/photo:opacity-40` 也不行：Tailwind 3 会**静默丢掉**
     里层的 group-hover，产物只剩 `:disabled{opacity:.4}`。
  ⇒ 显隐与可用性分成两层，各管一件事。
  **位置提示常驻**（不遮画面、也不跟着 hover）：它是「我在哪」的唯一线索，
  而且禁用的按钮拿不到焦点，键盘用户只能靠它知道「到头了」。

  无障碍
  ------
  箭头是真 `<button>`（Tab 可达、Enter/Space 可触发），带 `aria-label`；
  焦点环由 `styles/main.css` 的全局 `:focus-visible` 提供，
  ⚠️ 这里**不要**写 `focus:outline-none` —— 特异度更高会把焦点环覆盖掉。
  `disabled` 用 `disabled:` 变体表达视觉态，不在 JS 里改 opacity。
-->
<script setup>
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'

defineProps({
  /** 当前在已加载列表里的下标 + 1（1 基）；不在列表里为 0 */
  index: { type: Number, default: 0 },
  /** 分母 = **已加载条数 items.length**（不是后端 total，见 DR-31 纪律③） */
  total: { type: Number, default: 0 },
  /** 补充提示：只加载了一部分时说明还能继续加载；不在列表里时说明为何不能翻 */
  loadedHint: { type: String, default: '' },
  canPrev: { type: Boolean, default: false },
  canNext: { type: Boolean, default: false },
  /**
   * 外部忙碌（改判请求进行中 / 正在翻页）。
   * ⚠️ 这一条是 DR-31 与 DR-16 的**顺序纪律**：改判会改掉这张脸的 personCode /
   *    isStranger 与 pb_photo_person ⇒ 人脸框描边与侧栏「出现的人」都必须重画。
   *    改判未完成就跳走，回来时状态是陈旧的，而且**不报错**。
   */
  disabled: { type: Boolean, default: false },
})

defineEmits(['nav'])
</script>

<template>
  <!-- ⚠️ 整层 pointer-events-none：这一层覆盖在照片上，不能挡住人脸框的
       「✗ 不是他」按钮（它悬停时是溢出到照片框外的）。只有两个 slot 自己接事件。
       ⚠️ slot 必须在**照片容器的 relative 层内**（absolute 定位的参照系）。 -->
  <div class="photo-pager pointer-events-none absolute inset-0 z-10">
    <!-- 上一张 -->
    <div
      class="pager-slot pointer-events-auto absolute left-1 top-1/2 -translate-y-1/2 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover/photo:opacity-100 md:left-3"
    >
      <button
        type="button"
        class="pager-btn pager-prev inline-flex h-9 w-9 items-center justify-center rounded-full border border-line bg-card text-ink shadow-pop transition-opacity duration-150 hover:bg-surface disabled:cursor-not-allowed disabled:opacity-40 md:h-11 md:w-11"
        :disabled="disabled || !canPrev"
        aria-label="上一张"
        @click="$emit('nav', -1)"
      >
        <ChevronLeft class="h-5 w-5" aria-hidden="true" />
      </button>
    </div>

    <!-- 位置提示常驻（不遮画面）。index 为 0 = 不在列表里，显示「—」而不是 0：
         「0 / 60」会被读成「你在第一张」，那是一句假话。 -->
    <div
      class="pager-info pointer-events-none absolute bottom-2 left-1/2 flex -translate-x-1/2 flex-col items-center gap-0.5 text-center"
    >
      <p class="rounded-full border border-line bg-card px-2 py-0.5 shadow-pop">
        <span class="pager-index text-caption tabular-nums text-ink">{{ index || '—' }}</span>
        <span class="pager-total text-caption tabular-nums text-ink-weak">/ {{ total }}</span>
      </p>
      <p
        v-if="loadedHint"
        class="pager-hint max-w-[80vw] rounded-full border border-line bg-card px-2 py-0.5 text-caption text-ink-weak"
      >
        {{ loadedHint }}
      </p>
    </div>

    <!-- 下一张 -->
    <div
      class="pager-slot pointer-events-auto absolute right-1 top-1/2 -translate-y-1/2 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover/photo:opacity-100 md:right-3"
    >
      <button
        type="button"
        class="pager-btn pager-next inline-flex h-9 w-9 items-center justify-center rounded-full border border-line bg-card text-ink shadow-pop transition-opacity duration-150 hover:bg-surface disabled:cursor-not-allowed disabled:opacity-40 md:h-11 md:w-11"
        :disabled="disabled || !canNext"
        aria-label="下一张"
        @click="$emit('nav', 1)"
      >
        <ChevronRight class="h-5 w-5" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>
