/* ============================================================
 * 人物 store —— 名册 / 筛选 / 单人档案
 * ============================================================
 * 筛选与后端 /api/persons 的 Query 对应（api/browse.js）。
 * 分类取值：family 家人 / friend 朋友 / colleague 同事；
 * delFlag 是「停用」不是「删除」（DR-19：只停用不删除）。
 * ⚠️ 步骤 10 只建结构，页面不自动取数（步骤 12 接入）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getPerson, listPersons } from '@/api/browse'

export const PERSON_CATEGORIES = [
  { value: 'family', label: '家人' },
  { value: 'friend', label: '朋友' },
  { value: 'colleague', label: '同事' },
]

export const usePersonsStore = defineStore('persons', () => {
  const items = ref([])
  const total = ref(0)
  const page = ref(1)
  const size = ref(24)
  const loading = ref(false)

  /** 单人档案（P-05：含各年代桶分组统计与人脸样本两段） */
  const current = ref(null)
  const currentLoading = ref(false)

  const filters = ref({
    keyword: '',
    category: '',
    familyGroupCode: '',
    delFlag: '0',
  })

  /** 「显示已停用」是独立开关，默认关 —— 已停用的人不该出现在默认名册里 */
  const includeDisabled = ref(false)

  const hasFilter = computed(
    () =>
      Boolean(filters.value.keyword) ||
      Boolean(filters.value.category) ||
      Boolean(filters.value.familyGroupCode) ||
      includeDisabled.value,
  )

  function toQuery() {
    return {
      page: page.value,
      size: size.value,
      keyword: filters.value.keyword || undefined,
      category: filters.value.category || undefined,
      familyGroupCode: filters.value.familyGroupCode || undefined,
      // 勾了「显示已停用」才放开 delFlag，否则后端默认仍会带出停用的人
      delFlag: includeDisabled.value ? undefined : '0',
      orderBy: 'photoCount',
      desc: 1,
    }
  }

  async function fetchPersons() {
    loading.value = true
    try {
      const data = await listPersons(toQuery())
      items.value = data?.items ?? []
      total.value = data?.total ?? 0
      return data
    } finally {
      loading.value = false
    }
  }

  async function fetchPerson(personCode) {
    currentLoading.value = true
    try {
      current.value = await getPerson(personCode)
      return current.value
    } finally {
      currentLoading.value = false
    }
  }

  function resetFilters() {
    filters.value = { keyword: '', category: '', familyGroupCode: '', delFlag: '0' }
    includeDisabled.value = false
    page.value = 1
  }

  return {
    items,
    total,
    page,
    size,
    loading,
    current,
    currentLoading,
    filters,
    includeDisabled,
    hasFilter,
    toQuery,
    fetchPersons,
    fetchPerson,
    resetFilters,
  }
})